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


class UploadTests(unittest.TestCase):
    def test_mcp_upload_returns_url_without_catalog_generation_or_download(self):
        transport = Mock(side_effect=AssertionError("Unexpected model request"))
        session = hfsy.Session(transport=transport)
        response = io.BytesIO(b'{"success":true,"data":{"url":"https://example.test/video.mp4"}}')
        opener = Mock(open=Mock(return_value=response))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "参考视频.mp4"
            path.write_bytes(b"test video bytes")
            with patch.object(hfsy, "build_opener", return_value=opener), \
                    patch.object(mcp_server, "prepare_media") as prepare:
                result = mcp_server.invoke({"action": "upload", "key": "test-key",
                                            "files": [str(path)]}, session_factory=lambda: session)
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["url"], "https://example.test/video.mp4")
        self.assertEqual(data["kind"], "video")
        self.assertEqual(data["name"], "参考视频.mp4")
        self.assertEqual(data["response"], {"success": True, "data": {"url": data["url"]}})
        self.assertEqual([block["type"] for block in result["content"]], ["text"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, hfsy.BASE + "/v1/files/video-upload")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        opener.open.assert_called_once()
        prepare.assert_not_called()
        transport.assert_not_called()

    def test_audio_upload_and_echo(self):
        url = "https://example.test/audio.mp3?expires=123&signature=test"
        provider = {"success": True, "data": {"url": url}}
        response = io.BytesIO(json.dumps(provider).encode())
        opener = Mock(open=Mock(return_value=response))
        transport = Mock(side_effect=AssertionError("Unexpected model request"))
        session = hfsy.Session(transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "男(2).mp3"
            path.write_bytes(b"ID3 test audio")
            with patch.object(hfsy, "build_opener", return_value=opener), \
                    patch.object(mcp_server, "prepare_media") as prepare:
                result = mcp_server.invoke({"action": "upload", "key": "test-key", "files": [str(path)]},
                                          session_factory=lambda: session)
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["response"], provider)
        self.assertEqual(data["url"], url)
        self.assertEqual(data["mime"], "audio/mpeg")
        self.assertEqual([block["type"] for block in result["content"]], ["text"])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, hfsy.BASE + "/v1/files/audio-upload")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIn(b'name="file";', request.data)
        self.assertIn(b"Content-Type: audio/mpeg", request.data)
        self.assertIn(b"ID3 test audio", request.data)
        opener.open.assert_called_once()
        transport.assert_not_called()
        prepare.assert_not_called()

    def test_existing_session_key_is_reused(self):
        uploader = Mock(return_value={"url": "https://example.test/v.mp4"})
        session = hfsy.Session(uploader=uploader)
        session.set_key("test-key", load_catalog=False)
        result = mcp_server.invoke({"action": "upload", "files": ["video.mp4"]},
                                  session_factory=lambda: session)
        self.assertFalse(result["isError"])
        uploader.assert_called_once_with("video.mp4", "test-key")

    def test_invalid_files_rejected_before_upload(self):
        for files in (None, [], "video.mp4", ["a.mp4", "b.mp4"], [1], [""]):
            with self.subTest(files=files):
                uploader = Mock()
                session = hfsy.Session(uploader=uploader)
                session.set_key("test-key", load_catalog=False)
                with self.assertRaises(hfsy.RelayError):
                    session.upload(files)
                uploader.assert_not_called()

    def test_timeout_not_retried_and_key_redacted(self):
        uploader = Mock(side_effect=hfsy.RelayError("timeout", "timeout test-key"))
        session = hfsy.Session(uploader=uploader)
        session.set_key("test-key", load_catalog=False)
        result = mcp_server.invoke({"action": "upload", "files": ["video.mp4"]},
                                  session_factory=lambda: session)
        self.assertTrue(result["isError"])
        self.assertNotIn("test-key", result["content"][0]["text"])
        self.assertEqual(json.loads(result["content"][0]["text"])["code"], "timeout")
        uploader.assert_called_once()

    def test_missing_key_and_generation_fields_are_rejected(self):
        for extra in ({}, {"key": "test-key", "dry_run": True},
                      {"key": "test-key", "model": "model"}):
            session = hfsy.Session(uploader=Mock())
            result = mcp_server.invoke({"action": "upload", "files": ["video.mp4"], **extra},
                                      session_factory=lambda: session)
            self.assertTrue(result["isError"])
            session.uploader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
