import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hfsy


class GenerationUploadTests(unittest.TestCase):
    def session(self, uploader):
        transport = Mock(return_value={"id": "task", "status": "IN_PROGRESS"})
        session = hfsy.Session(transport=transport, uploader=uploader)
        session.set_key("test-key", load_catalog=False)
        limits = {"prompt_max_length": 1000}
        for kind in ("image", "video", "audio"):
            limits[kind + "_upload_limit"] = 3
            limits[kind + "_upload_size_hint"] = "20M"
        session._catalog = [{"name": "test-video", "type": "video", "available_to_key": True,
                             "constraints": limits, "raw": {"constraints": limits,
                             "supported_endpoint_types": ["openai"]}}]
        return session, transport

    def test_no_materials_submits_directly(self):
        for files in (None, []):
            with self.subTest(files=files):
                uploader = Mock()
                session, transport = self.session(uploader)
                result = session.call("test-video", "原样提示词", files)
                uploader.assert_not_called()
                transport.assert_called_once_with("/v1/video/create", "test-key",
                                                  {"model": "test-video", "prompt": "原样提示词"})
                self.assertEqual(result, transport.return_value)

    def test_mixed_materials_upload_then_submit_once(self):
        def upload(path, key, mime):
            return {"kind": hfsy.media_kind(mime), "url": "https://example.test/" + Path(path).name}
        uploader = Mock(side_effect=upload)
        session, transport = self.session(uploader)
        with tempfile.TemporaryDirectory() as directory:
            files = []
            for name in ("reference.png", "reference.mp4", "reference.mp3"):
                path = Path(directory) / name
                path.write_bytes(b"fixture")
                files.append(str(path))
            result = session.call("test-video", "参考 @image1 @video1 @audio1", files)
        self.assertEqual(uploader.call_count, 3)
        transport.assert_called_once_with("/v1/video/create", "test-key", {
            "model": "test-video", "prompt": "参考 @image1 @video1 @audio1",
            "images": ["https://example.test/reference.png"],
            "videos": ["https://example.test/reference.mp4"],
            "audios": ["https://example.test/reference.mp3"],
        })
        self.assertEqual(result, {"id": "task", "status": "IN_PROGRESS"})

    def test_failed_upload_does_not_submit_or_retry(self):
        uploader = Mock(side_effect=hfsy.RelayError("upstream", "Upload failed"))
        session, transport = self.session(uploader)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reference.mp4"
            path.write_bytes(b"fixture")
            with self.assertRaises(hfsy.RelayError):
                session.call("test-video", "提示词", [str(path)])
        uploader.assert_called_once()
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
