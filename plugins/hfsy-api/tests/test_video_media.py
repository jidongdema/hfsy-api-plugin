import base64
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hfsy
import mcp_server

# Container header fixture; tests validate transport, not video decoding.
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42"


class VideoMediaTests(unittest.TestCase):
    def test_video_upload_url_used_in_create_payload(self):
        url = "https://example.test/uploaded-video.mp4"
        response = io.BytesIO(json.dumps({"success": True, "data": {"url": url}}).encode())
        transport = Mock(open=Mock(return_value=response))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video4.mp4"
            path.write_bytes(MP4)
            uploaded = hfsy.upload_file(path, "test-key", transport=transport)
        request = transport.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.hfsyapi.cn/v1/files/video-upload")
        self.assertIn('name="file"; filename="video4.mp4"', request.data.decode())
        self.assertIn(MP4, request.data)
        entry = {"name": "video-model", "type": "video", "constraints": {},
                 "raw": {"supported_endpoint_types": ["openai"]}}
        route, payload = hfsy.build_payload(entry, "参考 @video1", [uploaded], {"video_duration": 5})
        self.assertEqual(route, "/v1/video/create")
        self.assertEqual(payload, {"model": "video-model", "prompt": "参考 @video1",
                                   "duration": 5, "videos": [url]})

    def test_video_create_groups_reference_media(self):
        entry = {"name": "video-model", "type": "video", "constraints": {},
                 "raw": {"supported_endpoint_types": ["openai"]}}
        media = [{"kind": kind, "url": "https://example.test/" + name}
                 for kind, name in [("video", "v1"), ("image", "i1"),
                                    ("audio", "a1"), ("video", "v2")]]
        _, payload = hfsy.build_payload(entry, "原始提示词", media, {})
        self.assertEqual(payload["videos"], ["https://example.test/v1", "https://example.test/v2"])
        self.assertEqual(payload["images"], ["https://example.test/i1"])
        self.assertEqual(payload["audios"], ["https://example.test/a1"])
        self.assertFalse({"video", "audio", "image_urls"} & payload.keys())

    def prepare(self, body, mime, url="https://example.test/result"):
        response = io.BytesIO(body)
        response.headers = {"Content-Type": mime}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(hfsy, "build_opener", return_value=Mock(open=Mock(return_value=response))):
                result = hfsy.prepare_media({"video_url": url}, directory)
            if result["artifacts"]:
                self.assertEqual(Path(result["artifacts"][0]["path"]).read_bytes(), body)
            return result

    def test_extensionless_video(self):
        result = self.prepare(MP4, "video/mp4")
        self.assertEqual(result["artifacts"][0]["mime_type"], "video/mp4")
        artifact = result["artifacts"][0]
        self.assertEqual(artifact["source_url"], "https://example.test/result")
        self.assertEqual(artifact["name"], "生成的视频.mp4")
        self.assertIn(Path(artifact["path"]).as_posix(), artifact["markdown_file"])
        self.assertEqual(result["media_urls"][0]["result_url_text"], "https://example.test/result")
        self.assertEqual(result["media_urls"][0]["result_url_code_block"],
                         "```text\nhttps://example.test/result\n```")
        self.assertNotIn("result_url_markdown", result["media_urls"][0])
        self.assertNotIn("markdown_link", result["media_urls"][0])
        self.assertNotIn("markdown_image", result["media_urls"][0])
        self.assertFalse(result["media_warnings"])

    def test_html_is_not_saved_as_video(self):
        result = self.prepare(b"<html>Error</html>", "text/html", "https://example.test/result.mp4")
        self.assertFalse(result["artifacts"])
        self.assertTrue(result["media_warnings"])
        self.assertEqual(len(result["media_urls"]), 1)

    def test_large_video_retains_link(self):
        result = self.prepare(MP4 + bytes(8 * 1024 * 1024), "video/mp4")
        self.assertFalse(result["artifacts"])
        self.assertTrue(result["media_warnings"])
        self.assertEqual(result["media_urls"][0]["mime_type"], "video/mp4")

    def test_input_references_excluded(self):
        self.assertEqual(hfsy.find_media_urls({"input": {"url": "https://example.test/input"},
                                             "video_url": "https://example.test/output"}),
                         ["https://example.test/output"])

    def test_standard_mcp_resource(self):
        session = Mock(key="test-key")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.mp4"
            path.write_bytes(MP4)
            prepared = {"artifacts": [{"path": str(path), "mime_type": "video/mp4"}],
                        "media_urls": [{"url": "https://example.test/result.mp4", "media_type": "video"}],
                        "media_warnings": [], "response": {}}
            with patch.object(mcp_server, "prepare_media", return_value=prepared):
                result = mcp_server.invoke({"action": "query", "task_id": "test", "task_type": "video"},
                                           session_factory=lambda: session)
        self.assertFalse(result["isError"])
        block = result["content"][0]
        self.assertEqual(block["type"], "resource")
        self.assertEqual(block["resource"]["mimeType"], "video/mp4")
        self.assertEqual(base64.b64decode(block["resource"]["blob"]), MP4)
        self.assertEqual(json.loads(result["content"][1]["text"])["image_count"], 0)
        link = next(item for item in result["content"] if item["type"] == "resource_link")
        self.assertEqual(link["uri"], path.as_uri())
        self.assertEqual(link["name"], "生成的视频.mp4")
        self.assertEqual(link["mimeType"], "video/mp4")
        self.assertEqual(link["size"], len(MP4))
        self.assertFalse(any(item["type"] == "resource_link" and
                             item["uri"].startswith("https://") for item in result["content"]))


if __name__ == "__main__":
    unittest.main()
