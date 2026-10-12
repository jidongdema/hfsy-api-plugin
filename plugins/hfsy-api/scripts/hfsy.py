"""Conversation-invoked HFSY client; no plugin registration or third-party packages."""

import base64
import json
import mimetypes
import re
import socket
import sys
import uuid
import unicodedata
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, Request, build_opener

BASE = "https://www.hfsyapi.cn"
MEDIA_UPLOAD_PATHS = {
    "image": "/v1/files/image-upload",
    "video": "/v1/files/video-upload",
    "audio": "/v1/files/audio-upload",
}

def documentation_constraints(document):
    """Parse the site's request-parameter table, never executable examples.

    Only recognized wording is converted. Keep the source table visible for
    restrictions (such as decoded media duration) this client cannot measure.
    """
    result = {}
    parameters = document.get("requestParams", [])
    if not isinstance(parameters, list):
        return result
    for parameter in parameters:
        if not isinstance(parameter, dict):
            continue
        name = parameter.get("name")
        description = parameter.get("description", "")
        if not isinstance(description, str):
            continue
        description = unicodedata.normalize("NFKC", description)
        description = re.sub(r"[‐‑–—〜～~]", "-", description)
        if name == "prompt":
            match = re.search(r"(?:≤|最多|最大|上限)\s*(\d+)\s*字", description)
            if match:
                result["prompt_max_length"] = int(match[1])
        elif name == "duration":
            match = re.search(r"(\d+)\s*-\s*(\d+)\s*秒", description)
            if match and 0 < int(match[1]) <= int(match[2]) <= 3600:
                result["video_duration_options"] = list(range(int(match[1]), int(match[2]) + 1))
            else:
                match = re.search(r"仅支持\s*(\d+(?:\s*[,、]\s*\d+)+)", description)
                if match:
                    result["video_duration_options"] = [int(n) for n in re.findall(r"\d+", match[1])]
        elif name == "ratio":
            # Ignore the following default/inference clause.
            values = re.findall(r"\d+:\d+", description.split("不传")[0])
            if values:
                result["aspect_ratio_options"] = list(dict.fromkeys(values))
        elif name == "orientation":
            values = re.findall(r"\b(?:landscape|portrait)\b", description)
            if values:
                result["video_orientation_options"] = list(dict.fromkeys(values))
        elif name == "size":
            values = re.findall(r"\b[124][kK]\b", description.split("默认")[0])
            if values:
                result["image_output_resolution_options"] = list(dict.fromkeys(v.upper() for v in values))
        elif name in ("images", "videos", "audios"):
            media = name[:-1]
            if "不支持" in description:
                result[media + "_upload_limit"] = 0
                continue
            match = re.search(r"(?:数量\s*≤|最多)\s*(\d+)\s*[张条]", description)
            if match:
                result[media + "_upload_limit"] = int(match[1])
            else:
                match = re.search(r"(\d+)\s*-\s*(\d+)\s*[张条]", description)
                if match:
                    result[media + "_upload_limit"] = [int(match[1]), int(match[2])]
            # A total-byte limit is not a per-file limit.
            match = re.search(r"(?<!总)大小\s*≤\s*(\d+(?:\.\d+)?)\s*(MB|KB|GB)\b", description, re.I)
            if match:
                result[media + "_upload_size_hint"] = match[1] + match[2].upper()
            if media == "image":
                match = re.search(r"总大小\s*≤\s*(\d+(?:\.\d+)?)\s*(MB|KB|GB)\b", description, re.I)
                if match:
                    result["image_upload_total_max_size"] = size_hint(match[1] + match[2])
    return result


class RelayError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def sanitize_unicode(value):
    """Make provider strings safe for UTF-8/MCP serialization."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, list):
        return [sanitize_unicode(item) for item in value]
    if isinstance(value, dict):
        return {sanitize_unicode(key): sanitize_unicode(item) for key, item in value.items()}
    return value


def safe_json(value, **kwargs):
    return json.dumps(sanitize_unicode(value), **kwargs)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_key(value):
    """Accept a raw token or a copied `Bearer <token>` value."""
    if not isinstance(value, str):
        raise RelayError("invalid_key", "Supply a nonempty API key")
    token = value.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token or any(ch.isspace() for ch in token):
        raise RelayError("invalid_key", "Supply a nonempty API key without whitespace")
    return token


def request(path, key=None, payload=None):
    if not path.startswith("/") or path.startswith("//"):
        raise RelayError("protocol", "Invalid API path")
    headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
    if key:
        # HFSY's OpenAI-compatible gateway accepts the token in Bearer
        # authorization. Keep X-API-Key for the media endpoints, but do not
        # send both on gateway requests: some deployments reject the mixed
        # credential form even when the token is valid.
        headers["Authorization"] = "Bearer " + key
        if not path.startswith("/v1/") and not path.startswith("/v1beta/"):
            headers["X-API-Key"] = key
        # Gemini-compatible generation routes authenticate with this header.
        if path.startswith("/v1beta/models/"):
            headers["x-goog-api-key"] = key
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    req = Request(BASE + path, data=body, headers=headers)
    try:
        # Generation endpoints can take longer than ordinary API calls. This only
        # extends the wait; it never retries or duplicates a submitted request.
        if payload is not None and path.startswith(("/v1/video/", "/mj/")):
            timeout = 4800
        elif payload is not None and path.startswith(("/v1/images/", "/v1beta/models/")):
            timeout = 300
        else:
            timeout = 60
        with build_opener(NoRedirect).open(req, timeout=timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            raw = response.read()
    except HTTPError as exc:
        code = {401: "invalid_key", 403: "forbidden", 402: "insufficient_balance",
                404: "not_found", 400: "invalid_parameter", 422: "invalid_parameter",
                429: "rate_limited", 504: "timeout"}.get(exc.code, "http_error")
        message = exc.read(16384).decode(errors="replace")
        if any(word in message.lower() for word in ("quota", "balance", "余额", "额度")):
            code = "insufficient_balance"
        if key:
            message = message.replace(key, "[REDACTED]")
        raise RelayError(code, f"HTTP {exc.code}: {message}") from None
    except (TimeoutError, socket.timeout):
        raise RelayError("timeout", "Request timed out; generation may have started. Do not retry automatically.") from None
    except URLError:
        raise RelayError("network", "Cannot connect to the relay") from None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        if "text/html" in content_type:
            raise RelayError("upstream_route", "HFSY returned a web page instead of an API response; the requested endpoint is not an upstream API route") from None
        raise RelayError("protocol", "Expected a JSON response") from None
    if not isinstance(value, dict):
        raise RelayError("protocol", "Expected a JSON object")
    if value.get("error") or value.get("success") is False:
        message = safe_json(value.get("error") or value.get("message"), ensure_ascii=False)
        raise RelayError("upstream", message.replace(key, "[REDACTED]") if key else message)
    return value


def media_kind(mime):
    for kind_name in ("image", "video", "audio"):
        if mime.startswith(kind_name + "/"):
            return kind_name
    return None


def image_size_for(aspect_ratio, resolution):
    """Convert a catalog ratio/resolution pair into HFSY's WxH size string."""
    if aspect_ratio in (None, ""):
        aspect_ratio = "1:1"
    if resolution in (None, ""):
        return None
    try:
        width_part, height_part = str(aspect_ratio).split(":", 1)
        ratio_w, ratio_h = float(width_part), float(height_part)
        if ratio_w <= 0 or ratio_h <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise RelayError("invalid_parameter", f"Invalid aspect ratio: {aspect_ratio}") from None
    resolution_text = str(resolution).strip().upper()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)K", resolution_text)
    if not match:
        raise RelayError("unsupported_protocol", f"Unsupported image resolution: {resolution}")
    long_side = round(float(match.group(1)) * 1024)
    scale = long_side / max(ratio_w, ratio_h)
    width = max(64, round(ratio_w * scale / 64) * 64)
    height = max(64, round(ratio_h * scale / 64) * 64)
    return f"{width}x{height}"


def upload_file(path, key, mime=None, transport=None):
    """Upload one local media file and return the HFSY-hosted URL."""
    path = Path(path)
    mime = mime or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    kind_name = media_kind(mime)
    if kind_name is None:
        raise RelayError("unsupported_material", f"Unsupported media type: {mime}")
    try:
        data = path.read_bytes()
    except OSError:
        raise RelayError("invalid_parameter", f"Cannot read material: {path.name}") from None

    filename = path.name.replace("\r", "").replace("\n", "")
    if kind_name == "image":
        # The image upload API accepts JSON base64 and returns data.url. That
        # URL is then passed to the model-specific generation payload.
        body = json.dumps({
            "base64": base64.b64encode(data).decode("ascii"),
            "mime_type": mime,
            "filename": filename,
        }).encode("utf-8")
        headers = {"Accept": "application/json", "Cache-Control": "no-cache",
                   "Content-Type": "application/json"}
    else:
        boundary = "----hfsy-" + uuid.uuid4().hex
        body = (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        headers = {"Accept": "application/json", "Cache-Control": "no-cache",
                   "Content-Type": f"multipart/form-data; boundary={boundary}"}
    if key:
        headers["Authorization"] = "Bearer " + key
        headers["X-API-Key"] = key
    req = Request(BASE + MEDIA_UPLOAD_PATHS[kind_name], data=body, headers=headers)
    try:
        opener = transport or build_opener(NoRedirect)
        with opener.open(req, timeout=300) as response:
            raw = response.read()
    except HTTPError as exc:
        message = exc.read(16384).decode(errors="replace")
        if key:
            message = message.replace(key, "[REDACTED]")
        code = {401: "invalid_key", 403: "forbidden", 404: "not_found",
                413: "invalid_parameter", 429: "rate_limited", 504: "timeout"}.get(exc.code, "http_error")
        raise RelayError(code, f"HTTP {exc.code}: {message}") from None
    except (TimeoutError, socket.timeout):
        raise RelayError("timeout", "Media upload timed out; do not retry automatically.") from None
    except URLError:
        raise RelayError("network", "Cannot connect to the HFSY media upload service") from None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise RelayError("protocol", "Expected a JSON response from the media upload service") from None
    if not isinstance(value, dict) or value.get("success") is False:
        message = safe_json(value.get("error") or value.get("message"), ensure_ascii=False) if isinstance(value, dict) else "Malformed upload response"
        raise RelayError("upstream", message.replace(key, "[REDACTED]") if key else message)
    result = value.get("data")
    url = result.get("url") if isinstance(result, dict) else None
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise RelayError("protocol", "Media upload succeeded but returned no usable URL")
    return {"kind": kind_name, "mime": mime, "url": url, "name": path.name,
            "size": len(data), "response": value}


def constraints(row):
    value = row.get("constraints") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise RelayError("invalid_config", "Model constraints are not valid JSON") from None
    if not isinstance(value, dict):
        raise RelayError("invalid_config", "Model constraints must be an object")
    return {**row.get("documented_constraints", {}), **value}


def kind(row):
    tags = row.get("tags", "")
    tags = tags if isinstance(tags, list) else re.split(r"[,\s]+", tags)
    for result, names in (("video", ("video", "视频")), ("image", ("image", "图片", "生图")),
                          ("text", ("chat", "text", "文本"))):
        if any(name in tags for name in names):
            return result
    return "unknown"


def size_hint(value):
    if isinstance(value, bool):
        raise RelayError("invalid_config", "Invalid image size limit")
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|K|KB|KIB|M|MB|MIB|G|GB|GIB)?\s*",
                         str(value), re.I)
    if not match:
        raise RelayError("invalid_config", "Unrecognized image_upload_size_hint")
    # The platform's numeric size hints are expressed in megabytes.
    unit = (match[2] or "M").upper()[0]
    return float(match[1]) * {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[unit]


def maximum(value, limit, name):
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or limit < 0:
        raise RelayError("invalid_config", f"Invalid limit: {name}")
    if value > limit:
        raise RelayError("invalid_parameter", f"{name}: {value} exceeds {limit}")


def validate(row, prompt, paths, params):
    if not isinstance(prompt, str) or not prompt.strip():
        raise RelayError("invalid_parameter", "Prompt must be a nonempty string")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in prompt):
        raise RelayError("invalid_parameter", "Prompt contains invalid Unicode; resend it using UTF-8")
    c = constraints(row)
    known = {"prompt_max_length", "image_upload_limit", "video_upload_limit", "audio_upload_limit",
             "total_upload_limit", "image_upload_size_hint", "video_upload_size_hint",
             "audio_upload_size_hint", "image_upload_total_max_size"}
    unknown = [name for name in c if name not in known and not name.endswith(("_options", "_default"))]
    if unknown:
        raise RelayError("unsupported_constraint", "New constraint semantics require an adapter: " + ", ".join(unknown))
    missing = []
    if "prompt_max_length" in c:
        # Match JavaScript string.length used by the platform UI.
        maximum(len(prompt.encode("utf-16-le")) // 2, c["prompt_max_length"], "prompt_max_length")
    else:
        missing.append("prompt_max_length")
    files = []
    counts = {"image": 0, "video": 0, "audio": 0}
    if not isinstance(paths, list):
        raise RelayError("invalid_parameter", "files must be a list of local paths")
    for filename in paths:
        path = Path(filename)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        kind_name = media_kind(mime)
        if kind_name is None:
            raise RelayError("unsupported_material", f"Unsupported media type: {mime}")
        try:
            data = path.read_bytes()
        except OSError:
            raise RelayError("invalid_parameter", f"Cannot read material: {path.name}") from None
        hint_name = f"{kind_name}_upload_size_hint"
        if hint_name in c:
            maximum(len(data), size_hint(c[hint_name]), f"{kind_name} bytes")
        elif kind_name == "image" and "image_upload_size_hint" in c:
            maximum(len(data), size_hint(c["image_upload_size_hint"]), "image bytes")
        else:
            missing.append(hint_name)
        counts[kind_name] += 1
        files.append((mime, data, kind_name))
    for kind_name in counts:
        field = f"{kind_name}_upload_limit"
        if counts[kind_name] and field not in c:
            missing.append(field)
    for field in ("image_upload_limit", "video_upload_limit", "audio_upload_limit", "total_upload_limit"):
        if field in c:
            limit = c[field]
            if isinstance(limit, list):
                if len(limit) != 2 or any(type(v) is not int or v < 0 for v in limit) or limit[0] > limit[1]:
                    raise RelayError("invalid_config", f"Invalid range: {field}")
                count = len(files) if field == "total_upload_limit" else counts[field.removesuffix("_upload_limit")]
                if count and count < limit[0]:
                    raise RelayError("invalid_parameter", f"{field}: minimum {limit[0]} media files")
                limit = limit[1]
            count = len(files) if field == "total_upload_limit" else counts[field.removesuffix("_upload_limit")]
            maximum(count, limit, field)
    if "image_upload_total_max_size" in c:
        maximum(sum(len(data) for _, data, media_type in files if media_type == "image"),
                c["image_upload_total_max_size"], "total image bytes")
    for name, value in params.items():
        options = c.get(name + "_options")
        if options is None:
            raise RelayError("unpublished_limit", f"No published options for {name}")
        if not isinstance(options, list) or value not in options:
            raise RelayError("invalid_parameter", f"{name} must be one of {options}")
    if missing:
        raise RelayError("unpublished_limit", "Required limits were not found in the model catalog or recognized API documentation: " + ", ".join(sorted(set(missing))))
    # Enforce documented restrictions too when the catalog disagrees. Neither
    # source may silently relax a bound published by the other source.
    documented = row.get("documented_constraints", {})
    if documented and any(c.get(k) != v for k, v in documented.items()):
        validate({"constraints": {**c, **documented}}, prompt, paths, params)
    return files


class Session:
    def __init__(self, transport=request, uploader=upload_file):
        self.key = None
        self.transport = transport
        self.uploader = uploader
        self._catalog = None

    def set_key(self, key, load_catalog=True):
        self.key = None
        self._catalog = None
        self.key = normalize_key(key)
        if not load_catalog:
            return None
        try:
            self._catalog = self.catalog()
            return self._catalog
        except Exception:
            self.key = None
            self._catalog = None
            raise

    def catalog(self):
        self._catalog = None
        # These are independent live reads. Fetch them concurrently so a slow
        # catalog endpoint does not add its latency to the other one.
        with ThreadPoolExecutor(max_workers=3) as pool:
            market_future = pool.submit(self.transport, "/api/pricing", self.key)
            models_future = pool.submit(self.transport, "/v1/models", self.key) if self.key else None
            docs_future = pool.submit(self.transport, "/api/docs", None)
            market = market_future.result()
            models = models_future.result() if models_future else None
            docs = docs_future.result()
        document_models = docs.get("data", {}).get("models") if isinstance(docs.get("data"), dict) else None
        if not isinstance(document_models, dict):
            raise RelayError("protocol", "Malformed API documentation response: expected data.models")
        rows = market.get("data")
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get("model_name") for r in rows):
            raise RelayError("protocol", "Malformed marketplace response")
        enriched = []
        for row in rows:
            document = document_models.get(row["model_name"], {})
            if not isinstance(document, dict):
                raise RelayError("protocol", "Malformed model API documentation")
            enriched.append({**row, "documented_constraints": documentation_constraints(document),
                             "documentation": {"url": BASE + "/docs/api?model=" + quote(row["model_name"], safe=""),
                                               "requestParams": document.get("requestParams", []),
                                               "errorTips": document.get("errorTips", []),
                                               "validation_scope": "Recognized prompt length, file bytes/counts, output duration and enum limits. Reference media duration, dimensions and codec rules require separate review."}})
        rows = enriched
        available = None
        if self.key:
            data = models.get("data")
            if not isinstance(data, list) or any(not isinstance(r, dict) or not r.get("id") for r in data):
                raise RelayError("protocol", "Malformed authorized model list")
            available = {r["id"] for r in data}
        result = [{"name": r["model_name"], "type": kind(r),
                 "context_tokens": r.get("context_length"),
                 "available_to_key": None if available is None else r["model_name"] in available,
                 "constraints": constraints(r), "raw": r} for r in rows]
        self._catalog = result
        return result

    def upload(self, files):
        """Upload one reference without submitting a generation request."""
        if not self.key:
            raise RelayError("invalid_key", "Initialize the session first")
        if (not isinstance(files, list) or len(files) != 1
                or not isinstance(files[0], str) or not files[0].strip()):
            raise RelayError("invalid_parameter", "upload requires exactly one local path in files")
        return self.uploader(files[0], self.key)

    def call(self, model, prompt, files=None, params=None, dry_run=False):
        if not self.key:
            raise RelayError("invalid_key", "Initialize the session first")
        catalog = self._catalog
        if catalog is None:
            catalog = self.catalog()
        entry = next((r for r in catalog if r["name"] == model), None)
        if not entry or not entry["available_to_key"]:
            raise RelayError("model_unavailable", "Model was removed or is unavailable to this key")
        params = params or {}
        if not isinstance(params, dict):
            raise RelayError("invalid_parameter", "params must be an object")
        material = validate(entry["raw"], prompt, files or [], params)
        if dry_run:
            preview = [{"kind": kind_name, "mime": mime, "url": "https://upload.invalid/preview"}
                       for mime, _data, kind_name in material]
            path, _payload = build_payload(entry, prompt, preview, params)
            return {"validated": True, "path": path, "model": model, "files": len(material), "params": params}
        uploaded = []
        if material:
            with ThreadPoolExecutor(max_workers=min(8, len(material))) as pool:
                uploaded = list(pool.map(lambda item: self.uploader(item[1], self.key, item[0]),
                                         [(mime, files[index]) for index, (mime, _data, _kind) in enumerate(material)]))
        path, payload = build_payload(entry, prompt, uploaded, params)
        return self.transport(path, self.key, payload)

    def query(self, task_id, task_type):
        if not self.key:
            raise RelayError("invalid_key", "Initialize the session first")
        task_id = quote(str(task_id), safe="")
        paths = {"video": f"/v1/video/query?id={task_id}",
                 "midjourney": f"/mj/task/{task_id}/fetch",
                 "image": f"/v1/image/query/{task_id}"}
        if task_type not in paths:
            raise RelayError("unsupported_protocol", "Unknown async task protocol")
        return self.transport(paths[task_type], self.key)


def build_payload(entry, prompt, material, params):
    model, modality = entry["name"], entry["type"]
    endpoints = entry["raw"].get("supported_endpoint_types", [])
    c = entry["constraints"]
    def media_item(item):
        if isinstance(item, dict):
            return item
        mime, data, *rest = item
        return {"kind": rest[0] if rest else media_kind(mime), "mime": mime,
                "data": data, "url": None}

    media = [media_item(item) for item in material]
    values = dict(params)
    for name in ("aspect_ratio", "image_output_resolution", "video_duration", "video_orientation"):
        default = c.get(name + "_default")
        if name not in values and default not in (None, ""):
            if default not in c.get(name + "_options", []):
                raise RelayError("invalid_config", f"Default outside options: {name}")
            values[name] = default
    if modality == "text":
        if "openai" not in endpoints or values:
            raise RelayError("unsupported_protocol", "Text protocol or parameters require an adapter")
        parts = [{"type": "text", "text": prompt}]
        for item in media:
            if not item.get("url"):
                raise RelayError("unsupported_protocol", "Media URL is required after upload")
            type_name = item["kind"] + "_url"
            parts.append({"type": type_name, type_name: {"url": item["url"]}})
        return "/v1/chat/completions", {"model": model, "messages": [{"role": "user", "content": parts}], "stream": False}
    if modality == "image" and "midjourney" in endpoints:
        if values:
            raise RelayError("unsupported_parameter", "Midjourney options require a protocol adapter")
        urls = [item["url"] for item in media]
        return "/mj/submit/imagine", {"botType": "MID_JOURNEY", "prompt": prompt, "image_urls": urls}
    if modality == "image" and "gemini" in endpoints:
        if set(values) - {"aspect_ratio", "image_output_resolution"}:
            raise RelayError("unsupported_parameter", "Unmapped Gemini image parameter")
        parts = [{"text": prompt}] + [{"fileData": {"mimeType": item["mime"], "fileUri": item["url"]}} for item in media]
        config = {target: values[source] for source, target in (("aspect_ratio", "aspectRatio"), ("image_output_resolution", "imageSize")) if source in values}
        return f"/v1beta/models/{quote(model, safe='')}:generateContent", {"contents": [{"parts": parts}], "generationConfig": {"imageConfig": config}}
    if modality == "image" and ("image-generation" in endpoints or "openai" in endpoints):
        ratio = values.get("aspect_ratio")
        resolution = values.get("image_output_resolution")
        size = image_size_for(ratio, resolution)
        unsupported = set(values) - {"aspect_ratio", "image_output_resolution"}
        if unsupported:
            raise RelayError("unsupported_parameter", "Unmapped image parameter: " + ", ".join(sorted(unsupported)))
        if media:
            # The documented HFSY image task API is asynchronous and accepts
            # uploaded inputs under reference_images.
            payload = {"model": model, "prompt": prompt,
                       "reference_images": [item["url"] for item in media],
                       **({"size": size} if size else {})}
            return "/v1/image/create", payload
        payload = {"model": model, "prompt": prompt, "n": 1,
                   "response_format": "b64_json", **({"size": size} if size else {})}
        return "/v1/images/generations", payload
    if modality == "video" and ("openai" in endpoints or "videoCreate" in endpoints):
        mapping = {"video_duration": "duration", "video_orientation": "orientation", "aspect_ratio": "ratio"}
        if set(values) - set(mapping):
            raise RelayError("unsupported_parameter", "Unmapped video parameter")
        payload = {"model": model, "prompt": prompt, **{mapping[k]: v for k, v in values.items()}}
        for kind_name in ("image", "video", "audio"):
            urls = [item["url"] for item in media if item["kind"] == kind_name]
            if urls:
                payload[{"image": "images", "video": "videos", "audio": "audios"}[kind_name]] = urls
        return "/v1/video/create", payload
    raise RelayError("unsupported_protocol", "Model protocol is not supported by the current adapter")


def main():
    session = Session()
    try:
        cmd = json.load(sys.stdin)
        if not isinstance(cmd, dict):
            raise RelayError("invalid_parameter", "Input must be a JSON object")
        action = cmd.get("action", "catalog")
        catalog = session.set_key(cmd["key"], load_catalog=action not in ("call", "query", "upload")) if "key" in cmd else None
        if action == "catalog":
            result = catalog if catalog is not None else session.catalog()
        elif action == "call":
            result = session.call(cmd["model"], cmd["prompt"], cmd.get("files"), cmd.get("params"), cmd.get("dry_run", False))
        elif action == "query":
            result = session.query(cmd["task_id"], cmd["task_type"])
        elif action == "upload":
            result = session.upload(cmd.get("files"))
        else:
            raise RelayError("invalid_parameter", "Unknown action")
        if action in ("call", "query"):
            result = prepare_media(result)
        output = {"ok": True, "result": result}
    except RelayError as exc:
        output = {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
    except (ValueError, TypeError, KeyError):
        output = {"ok": False, "error": {"code": "invalid_parameter", "message": "Malformed command or configuration"}}
    finally:
        key, session.key = session.key, None
    text = safe_json(output, ensure_ascii=False)
    print(text.replace(key, "[REDACTED]") if key else text)


def prepare_media(result, output_dir=None):
    """Extract images for native rendering and preserve video URLs as playable media."""
    directory = Path(output_dir or Path(__file__).resolve().parent / "outputs")
    artifacts = []
    warnings = []

    def save_bytes(data, mime):
        try:
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                suffix = ".png"
            elif data.startswith(b"\xff\xd8\xff"):
                suffix = ".jpg"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                suffix = ".webp"
            elif len(data) >= 16 and data[4:8] == b"ftyp" and 16 <= int.from_bytes(data[:4], "big") <= len(data):
                brand = data[8:12]
                if brand in (b"avif", b"avis", b"heic", b"heix", b"mif1", b"msf1"):
                    raise ValueError("Unsupported image container")
                suffix = ".mov" if brand == b"qt  " else ".mp4"
                mime = "video/quicktime" if suffix == ".mov" else "video/mp4"
            else:
                raise ValueError("Unrecognized media signature")
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / ("hfsy-" + uuid.uuid4().hex + suffix)
            path.write_bytes(data)
            if suffix in (".png", ".jpg", ".webp"):
                mime = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[suffix]
            artifact = {"path": str(path.resolve()), "mime_type": mime}
            artifacts.append(artifact)
            return artifact
        except (ValueError, OSError) as exc:
            warnings.append("Media extraction failed: " + str(exc))
            return {"media_error": "Media could not be saved; do not regenerate automatically"}

    def save(encoded, mime):
        try:
            return save_bytes(base64.b64decode(encoded, validate=True), mime)
        except (ValueError, OSError) as exc:
            warnings.append("Image extraction failed: " + str(exc))
            return {"media_error": "Image could not be saved; do not regenerate automatically"}

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        clean = {}
        for name, item in value.items():
            if name in ("thoughtSignature", "thought_signature"):
                continue
            if name in ("inlineData", "inline_data") and isinstance(item, dict) and "data" in item:
                clean["local_image"] = save(item["data"], item.get("mimeType", item.get("mime_type", "image/png")))
            elif name == "b64_json" and isinstance(item, str):
                clean["local_image"] = save(item, "image/png")
            else:
                clean[name] = visit(item)
        return clean

    cleaned = visit(result)
    urls = sorted(set(find_media_urls(cleaned)))
    display = []
    for url in urls:
        suffix = Path(urlparse(url).path).suffix.lower()
        mime = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}.get(suffix, "image/*")
        is_video = mime.startswith("video/")
        # Download hosted media so the MCP response can carry an embedded
        # resource. The client may render video/mp4 as a native player.
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https"):
            try:
                req = Request(url, headers={"Accept": "video/mp4,video/*,image/*", "Cache-Control": "no-cache"})
                with build_opener().open(req, timeout=120) as response:
                    content_type = (response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                    if content_type.startswith("video/"):
                        is_video, mime = True, content_type
                    limit = 8 * 1024 * 1024 if is_video else 25 * 1024 * 1024
                    data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("media exceeds embedding limit; use result URL")
                artifact = save_bytes(data, content_type)
                if artifact.get("path"):
                    mime = artifact["mime_type"]
                    is_video = mime.startswith("video/")
                    if is_video:
                        artifact["source_url"] = url
                        artifact["name"] = "生成的视频" + Path(artifact["path"]).suffix
                        artifact["markdown_file"] = f'[{artifact["name"]}](<{Path(artifact["path"]).as_posix()}>)'
                    if is_video and len(data) > 8 * 1024 * 1024:
                        artifact["embed"] = False
                        warnings.append("Video exceeds embedding limit; use result URL")
            except (HTTPError, URLError, TimeoutError, socket.timeout, ValueError, OSError) as exc:
                warnings.append(f"Remote media download failed: {exc}")
        display.append({
            "url": url,
            "media_type": "video" if is_video else "image",
            "mime_type": mime,
            **({"result_url_text": url,
                "result_url_code_block": "```text\n" + url + "\n```"} if is_video else {
                "markdown_image": f"![生成图片]({url})",
                "result_url_markdown": f"[{url}]({url})",
                "markdown_link": f"[打开原图]({url})",
            }),
        })
    return {"response": cleaned, "artifacts": artifacts, "media_urls": display,
            "media_warnings": warnings}


def find_media_urls(value):
    """Collect URLs from common image/video response fields for chat rendering."""
    if isinstance(value, list):
        urls = []
        for item in value:
            urls.extend(find_media_urls(item))
        return urls
    if not isinstance(value, dict):
        return []
    urls = []
    for name, item in value.items():
        if name.lower() in {"input", "request", "image_urls", "video_urls", "audio_urls"}:
            continue
        if isinstance(item, str) and item.startswith(("https://", "http://")) and name.lower() in {
            "url", "image_url", "imageuri", "fileuri", "file_uri", "video_url", "result_url",
        }:
            urls.append(item)
        else:
            urls.extend(find_media_urls(item))
    return urls


if __name__ == "__main__":
    main()
