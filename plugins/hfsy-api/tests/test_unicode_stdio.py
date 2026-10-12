import json
import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from hfsy import RelayError, upload_file, validate


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class _UploadTransport:
    def __init__(self):
        self.request = None

    def open(self, request, timeout):
        self.request = request
        return _Response(json.dumps({"success": True, "data": {"url": "https://cdn.example/image.png"}}).encode())


class UnicodeStdioTests(unittest.TestCase):
    def test_image_upload_uses_json_base64_endpoint(self):
        transport = _UploadTransport()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.png"
            data = b"png-bytes"
            path.write_bytes(data)
            result = upload_file(path, "test-key", "image/png", transport)

        self.assertEqual(result["url"], "https://cdn.example/image.png")
        self.assertEqual(transport.request.full_url, "https://www.hfsyapi.cn/v1/files/image-upload")
        self.assertEqual(transport.request.headers["Authorization"], "Bearer test-key")
        payload = json.loads(transport.request.data.decode("utf-8"))
        self.assertEqual(payload, {
            "base64": base64.b64encode(data).decode("ascii"),
            "mime_type": "image/png",
            "filename": "test.png",
        })

    def test_utf8_input_survives_windows_legacy_encoding(self):
        prompt = "\u751f\u6210\u4e00\u5f20\u9f9f\u5154\u8d5b\u8dd1\u7684\u56fe\u7247 \U0001f422\n  exact spaces"
        # Exercise the real stdio reader without credentials or provider calls.
        program = (
            "import mcp_server as server; "
            "server.dispatch = lambda request: request; server.main()"
        )
        for ascii_json in (False, True):
            with self.subTest(ascii_json=ascii_json):
                result = subprocess.run(
                    [sys.executable, "-c", program], cwd=SCRIPTS,
                    env={**os.environ, "PYTHONIOENCODING": "cp936:surrogateescape"},
                    input=(json.dumps({"prompt": prompt}, ensure_ascii=ascii_json) + "\n").encode("utf-8"),
                    capture_output=True, timeout=10, check=True,
                )
                self.assertEqual(json.loads(result.stdout)["prompt"], prompt)

    def test_invalid_unicode_is_rejected_before_submission(self):
        with self.assertRaises(RelayError) as caught:
            validate({}, "test\udc80", [], {})
        self.assertEqual(caught.exception.code, "invalid_parameter")

    def test_length_counts_utf16_units(self):
        row = {"constraints": {"prompt_max_length": 1}}
        with self.assertRaises(RelayError) as caught:
            validate(row, "\U0001f422", [], {})
        self.assertEqual(caught.exception.code, "invalid_parameter")


if __name__ == "__main__":
    unittest.main()
