"""验证零退出码错误、缺失文件与设备选择的失败行为。"""

import subprocess
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from har_capture.hdc import CaptureError, Hdc


class HdcTests(unittest.TestCase):
    """所有进程调用均被替换，不访问设备。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "hdc.exe"
        self.executable.write_bytes(b"test fixture")
        self.hdc = Hdc(self.executable, "test-device")

    def test_zero_exit_code_with_fail_marker_is_error(self):
        result = subprocess.CompletedProcess([], 0, b"[Fail] Permission denied\n", b"")
        with patch("har_capture.hdc.subprocess.run", return_value=result), self.assertRaises(CaptureError):
            self.hdc.run(["shell", "test"], "test")
        record = self.hdc.records[0]
        self.assertEqual(record["return_code"], 0)
        self.assertNotIn("stdout", record)
        self.assertEqual(record["stdout_sha256"], hashlib.sha256(result.stdout).hexdigest())
        self.assertIn("finished_at", record)

    def test_receive_requires_new_nonempty_file(self):
        destination = self.root / "layout.json"
        with patch.object(self.hdc, "run", return_value="FileTransfer finish"):
            with self.assertRaises(CaptureError):
                self.hdc.receive("/data/local/tmp/layout.json", destination, "receive")
            destination.write_bytes(b"previous data")
            with self.assertRaises(CaptureError):
                self.hdc.receive("/data/local/tmp/layout.json", destination, "receive")
        self.assertEqual(destination.read_bytes(), b"previous data")

    def test_connect_requires_exactly_one_authorized_device(self):
        cases = ("[Empty]", "serial USB Unauthorized device hdc",
                 "first USB Connected device hdc\nsecond USB Connected device hdc")
        for output in cases:
            with self.subTest(output=output):
                hdc = Hdc(self.executable, None)
                with patch.object(hdc, "run", return_value=output), self.assertRaises(CaptureError):
                    hdc.connect()

    def test_receive_rejects_created_file_without_transfer_confirmation(self):
        destination = self.root / "layout.json"

        def transfer_without_confirmation(arguments, name):
            destination.write_bytes(b"incomplete transfer")
            return "Remote transfer failed"

        with patch.object(self.hdc, "run", side_effect=transfer_without_confirmation):
            with self.assertRaises(CaptureError):
                self.hdc.receive("/data/local/tmp/layout.json", destination, "receive")

    def test_explicit_device_selection_is_preserved(self):
        output = "other USB Connected device hdc\ntest-device USB Connected device hdc"
        with patch.object(self.hdc, "run", return_value=output):
            self.hdc.connect()
        self.assertEqual(self.hdc.device, "test-device")

    def test_timeout_records_failure_and_duration(self):
        error = subprocess.TimeoutExpired(["hdc"], 60)
        with patch("har_capture.hdc.subprocess.run", side_effect=error), self.assertRaises(CaptureError):
            self.hdc.run(["shell", "test"], "timeout")
        self.assertIn("error", self.hdc.records[0])
        self.assertIn("duration_seconds", self.hdc.records[0])
        self.assertIn("finished_at", self.hdc.records[0])


if __name__ == "__main__":
    unittest.main()
