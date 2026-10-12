import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hfsy

DOCS = json.loads((Path(__file__).parent / "fixtures" / "api_docs.json").read_text(encoding="utf-8-sig"))

class DocumentedLimitTests(unittest.TestCase):
    def row(self, **overrides):
        limits = {
            "prompt_max_length": 10000,
            "image_upload_limit": 9, "video_upload_limit": 3,
            "audio_upload_limit": 3, "total_upload_limit": 12,
            "image_upload_size_hint": "20M",
            "image_upload_total_max_size": 30 * 1024**2,
            "video_duration_options": list(range(5, 16)),
            "video_orientation_options": ["landscape", "portrait"],
            "video_orientation_default": "portrait",
        }
        limits.update(overrides)
        return {"model_name": "sd-2-vip-480", "constraints": limits,
                "documented_constraints": hfsy.documentation_constraints(DOCS["data"]["models"]["sd-2-vip-480"]),
                "supported_endpoint_types": ["openai"]}

    def test_fallback_and_catalog_precedence(self):
        row = self.row(video_upload_size_hint="100M")
        for value in (row["constraints"], json.dumps(row["constraints"])):
            row["constraints"] = value
            merged = hfsy.constraints(row)
            self.assertEqual(merged["video_upload_size_hint"], "100M")
            self.assertEqual(merged["audio_upload_size_hint"], "15MB")

    def test_other_models_still_fail_closed(self):
        row = self.row()
        row["model_name"] = "another-video-model"
        row.pop("documented_constraints")
        with patch.object(Path, "read_bytes", return_value=b"test"):
            with self.assertRaises(hfsy.RelayError) as caught:
                hfsy.validate(row, "test", ["video.mp4"], {})
        self.assertEqual(caught.exception.code, "unpublished_limit")

    def test_size_boundaries(self):
        for suffix, limit in (("mp4", 200), ("mp3", 15)):
            for extra in (0, 1):
                with self.subTest(suffix=suffix, extra=extra):
                    # Size-only fixture avoids allocating hundreds of MB.
                    data = Mock()
                    data.__len__ = Mock(return_value=limit * 1024**2 + extra)
                    with patch.object(Path, "read_bytes", return_value=data):
                        if extra:
                            with self.assertRaises(hfsy.RelayError) as caught:
                                hfsy.validate(self.row(), "test", ["ref." + suffix], {})
                            self.assertEqual(caught.exception.code, "invalid_parameter")
                        else:
                            hfsy.validate(self.row(), "test", ["ref." + suffix], {})

    def test_image_total_excludes_video_and_audio(self):
        sizes = {"cat.png": 16, "girl.jpg": 14, "ref.mp4": 40, "ref.mp3": 10}
        def read(path):
            data = Mock()
            data.__len__ = Mock(return_value=sizes[path.name] * 1024**2)
            return data
        with patch.object(Path, "read_bytes", read):
            hfsy.validate(self.row(), "test", list(sizes), {})
            sizes["girl.jpg"] = 15
            with self.assertRaises(hfsy.RelayError) as caught:
                hfsy.validate(self.row(), "test", list(sizes), {})
        self.assertIn("total image bytes", str(caught.exception))

    def test_count_and_duration_rejected(self):
        with patch.object(Path, "read_bytes", return_value=b"test"):
            for files, params in ((["ref.mp4"] * 4, {}), ([], {"video_duration": 4})):
                with self.assertRaises(hfsy.RelayError) as caught:
                    hfsy.validate(self.row(), "test", files, params)
                self.assertEqual(caught.exception.code, "invalid_parameter")

    def test_request_passes_preflight_and_preserves_references(self):
        row = self.row()
        transport = Mock(return_value={"id": "test-task"})
        uploader = Mock(side_effect=lambda path, key, mime: {
            "kind": hfsy.media_kind(mime), "url": "https://example.test/" + Path(path).name})
        session = hfsy.Session(transport=transport, uploader=uploader)
        session.set_key("test-key", load_catalog=False)
        session._catalog = [{"name": row["model_name"], "type": "video",
                             "available_to_key": True, "raw": row,
                             "constraints": hfsy.constraints(row)}]
        prompt = "@image1小猫和@image2美女在钓鱼@video1，音频参考@audio1"
        with tempfile.TemporaryDirectory() as directory:
            files = []
            for name in ("cat.png", "girl.jpg", "video.mp4", "audio.mp3"):
                path = Path(directory) / name
                path.write_bytes(b"fixture")
                files.append(str(path))
            result = session.call(row["model_name"], prompt, files,
                                  {"video_duration": 5}, dry_run=True)
            self.assertTrue(result["validated"])
            uploader.assert_not_called()
            transport.assert_not_called()
            session.call(row["model_name"], prompt, files, {"video_duration": 5})
        self.assertEqual(uploader.call_count, 4)
        transport.assert_called_once()
        payload = transport.call_args.args[2]
        self.assertEqual(payload["prompt"], prompt)
        self.assertEqual(payload["duration"], 5)
        self.assertEqual(payload["orientation"], "portrait")
        self.assertEqual(payload["images"], ["https://example.test/cat.png", "https://example.test/girl.jpg"])
        self.assertEqual(payload["videos"], ["https://example.test/video.mp4"])
        self.assertEqual(payload["audios"], ["https://example.test/audio.mp3"])

    def test_live_document_schema_extracts_sd25_limits(self):
        limits = hfsy.documentation_constraints(DOCS["data"]["models"]["sd-2.5-480"])
        self.assertEqual(limits["video_upload_size_hint"], "200MB")
        self.assertEqual(limits["audio_upload_size_hint"], "15MB")
        self.assertEqual(limits["image_upload_size_hint"], "20MB")
        self.assertEqual(limits["image_upload_total_max_size"], 30 * 1024**2)
        self.assertEqual(limits["video_upload_limit"], 10)
        self.assertEqual(limits["video_duration_options"], list(range(4, 31)))

    def test_catalog_reads_documents_and_dry_run_does_not_upload(self):
        row = self.row()
        row.pop("documented_constraints")
        row["model_name"] = "sd-2.5-480"
        row["tags"] = "视频"
        responses = {"/api/pricing": {"data": [row]}, "/api/docs": DOCS,
                     "/v1/models": {"data": [{"id": "sd-2.5-480"}]}}
        transport = Mock(side_effect=lambda path, key=None: responses[path])
        uploader = Mock()
        session = hfsy.Session(transport=transport, uploader=uploader)
        session.set_key("test-key")
        transport.assert_any_call("/api/docs", None)
        with patch.object(Path, "read_bytes", return_value=b"test"):
            result = session.call("sd-2.5-480", "@image1 @video1 @audio1", ["i.jpg", "v.mp4", "a.mp3"], {"video_duration": 5}, dry_run=True)
        self.assertTrue(result["validated"])
        uploader.assert_not_called()
        self.assertEqual(transport.call_count, 3)
        self.assertIn("sd-2.5-480", session._catalog[0]["raw"]["documentation"]["url"])

    def test_document_failure_clears_stale_catalog(self):
        session = hfsy.Session(transport=Mock(return_value={"data": []}))
        session._catalog = ["stale"]
        with self.assertRaises(hfsy.RelayError):
            session.catalog()
        self.assertIsNone(session._catalog)

    def test_conflicting_catalog_cannot_relax_documented_size(self):
        row = self.row(video_upload_size_hint="500M")
        data = Mock()
        data.__len__ = Mock(return_value=201 * 1024**2)
        with patch.object(Path, "read_bytes", return_value=data):
            with self.assertRaises(hfsy.RelayError) as caught:
                hfsy.validate(row, "test", ["v.mp4"], {})
        self.assertEqual(caught.exception.code, "invalid_parameter")

    def test_total_size_is_not_mistaken_for_per_file_size(self):
        limits = hfsy.documentation_constraints({"requestParams": [{"name": "images", "description": "总大小 ≤ 30 MB"}]})
        self.assertNotIn("image_upload_size_hint", limits)
        self.assertEqual(limits["image_upload_total_max_size"], 30 * 1024**2)

    def test_unrecognized_wording_does_not_invent_limits(self):
        self.assertEqual(hfsy.documentation_constraints({"requestParams": [{"name": "videos", "description": "见供应商要求"}]}), {})


if __name__ == "__main__":
    unittest.main()
