"""Small stdio MCP adapter; the initialized key/catalog live for this server process."""
import base64
import json
import sys
from pathlib import Path

from hfsy import RelayError, Session, normalize_key, prepare_media, safe_json


# MCP uses UTF-8 in both directions, regardless of the Windows code page.
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


TOOL = {
    "name": "hfsy",
    "description": "Validate a key and list models (catalog), generate (call), or poll (query). For generation, pass optional local image/video/audio paths in files: call uploads them internally only when present, then submits once. Do not call upload separately before generation or display reference URLs. Poll async tasks and show only the final generated result. Standalone upload is only for explicit upload-only requests. Preserve the prompt exactly. Never retry uploads or generation automatically.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["catalog", "call", "query", "upload"]},
            "key": {"type": "string", "minLength": 1},
            "model": {"type": "string"},
            "prompt": {"type": "string"},
            "params": {"type": "object"},
            "files": {"type": "array", "items": {"type": "string"}},
            "dry_run": {"type": "boolean"},
            "task_id": {"type": "string"},
            "task_type": {"type": "string", "enum": ["image", "video", "midjourney"]},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


_DEFAULT_SESSION = None


def invoke(args, session_factory=Session):
    global _DEFAULT_SESSION
    persistent = session_factory is Session
    if persistent:
        if _DEFAULT_SESSION is None:
            _DEFAULT_SESSION = session_factory()
        session = _DEFAULT_SESSION
    else:
        session = session_factory()
    supplied_key = args.get("key", "")
    try:
        action = args["action"]
        if action == "upload" and any(name in args for name in ("model", "prompt", "params", "dry_run", "task_id", "task_type")):
            raise RelayError("invalid_parameter", "upload accepts only action, files, and optional key")
        if supplied_key:
            if not isinstance(supplied_key, str) or not supplied_key.strip():
                raise RelayError("invalid_key", "Initialize with an API key first")
            key = normalize_key(supplied_key)
            if session.key != key:
                catalog = session.set_key(key, load_catalog=action != "upload")
            else:
                catalog = getattr(session, "_catalog", None)
        elif not session.key:
            raise RelayError("invalid_key", "Initialize with an API key first")
        else:
            catalog = getattr(session, "_catalog", None)
        if action == "catalog":
            result = catalog if catalog is not None else session.catalog()
        elif action == "call":
            result = session.call(args["model"], args["prompt"], args.get("files"), args.get("params"), args.get("dry_run", False))
        elif action == "query":
            result = session.query(args["task_id"], args["task_type"])
        elif action == "upload":
            result = session.upload(args.get("files"))
        else:
            raise RelayError("invalid_parameter", "Unknown action")
        images = []
        embedded_media = []
        media_links = []
        if action in ("call", "query"):
            # Decode provider bytes and download hosted image URLs for native MCP rendering.
            # Keep the artifact in a persistent plugin output directory so the Codex
            # client can also use the local path for a reliable thumbnail/preview.
            output_dir = Path(__file__).resolve().parent / "outputs"
            result = prepare_media(result, output_dir)
            for artifact in result["artifacts"]:
                path = Path(artifact["path"])
                mime = artifact.get("mime_type") or {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp", ".mp4": "video/mp4"}[path.suffix]
                if mime.startswith("image/"):
                    images.append({"type": "image", "mimeType": mime, "data": base64.b64encode(path.read_bytes()).decode("ascii")})
                elif mime.startswith("video/"):
                    name = artifact.get("name", "生成的视频" + path.suffix)
                    media_links.append({"type": "resource_link", "uri": path.as_uri(),
                                        "name": name, "title": name, "mimeType": mime,
                                        "size": path.stat().st_size})
                    if artifact.get("embed", True):
                        embedded_media.append({"type": "resource", "resource": {
                            "uri": path.as_uri(), "mimeType": mime,
                            "blob": base64.b64encode(path.read_bytes()).decode("ascii"),
                        }})
            # Video URLs are copyable text; only images get a remote link.
            for item in result["media_urls"]:
                url = item.get("url")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    is_video = item.get("media_type") == "video" or url.lower().split("?", 1)[0].endswith((".mp4", ".webm", ".mov"))
                    if is_video:
                        continue
                    media_links.append({
                        "type": "resource_link",
                        "uri": url,
                        "name": "播放视频" if is_video else "打开原图",
                        "mimeType": item.get("mime_type", "video/mp4" if is_video else "image/*"),
                    })
            # Return persistent artifact paths so the assistant can embed a local
            # Markdown image when a client cannot render MCP image content inline.
            result = {"media_urls": result["media_urls"], "media_warnings": result["media_warnings"],
                      "artifacts": result["artifacts"], "response": remove_paths(result["response"]),
                      "image_count": len(images), "video_count": sum(1 for item in result["media_urls"] if item.get("media_type") == "video")}
        text = safe_json(result, ensure_ascii=False).replace(session.key or "", "[REDACTED]")
        return {"content": images + embedded_media + [{"type": "text", "text": text}] + media_links, "isError": False}
    except Exception as exc:
        code = exc.code if isinstance(exc, RelayError) else "invalid_request_or_provider_error"
        message = str(exc) if isinstance(exc, RelayError) else f"{type(exc).__name__}: {exc}"
        if isinstance(supplied_key, str) and supplied_key:
            message = message.replace(supplied_key, "[REDACTED]")
        if session.key:
            message = message.replace(session.key, "[REDACTED]")
        return {"content": [{"type": "text", "text": safe_json({"code": code, "message": message})}], "isError": True}
    finally:
        if not persistent:
            session.key = None


def remove_paths(value):
    if isinstance(value, list):
        return [remove_paths(item) for item in value]
    if isinstance(value, dict):
        return {k: ({"returned_as": "MCP image content"} if k == "local_image" else remove_paths(v)) for k, v in value.items()}
    return value


def dispatch(request):
    request_id = request.get("id")
    method = request.get("method")
    if "id" not in request:
        return None
    if method == "initialize":
        # MCP clients may negotiate a newer protocol. Echo a supported client
        # version so the host does not reject the server as an invalid upstream.
        requested = (request.get("params") or {}).get("protocolVersion")
        protocol_version = requested if requested in {"2024-11-05", "2025-03-26", "2025-06-18"} else "2024-11-05"
        result = {"protocolVersion": protocol_version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "hfsy-api", "version": "0.2.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = request.get("params", {})
        if params.get("name") != "hfsy" or not isinstance(params.get("arguments"), dict):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid tool or arguments"}}
        result = invoke(params["arguments"])
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main():
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError()
            response = dispatch(request)
        except (ValueError, TypeError, AttributeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON-RPC request"}}
        if response is not None:
            print(safe_json(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
