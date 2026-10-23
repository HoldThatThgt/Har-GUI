"""验证独立采集命令读取来源声明，并只打印正式目录。"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import capture as cli


class CaptureCliTests(unittest.TestCase):
    def test_project_selectors_reach_capture_and_missing_project_defaults_to_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project.toml"
            project.write_text('[dataset]\nstability_regions = [{bundle_name="system.test",resource_id="clock"}]',
                               encoding="utf-8")
            for arguments, rules in (([], []), (["--project", str(project)], [
                {"bundle_name": "system.test", "resource_id": "clock"}])):
                with patch("sys.argv", ["capture.py", "--device", "device", *arguments]), \
                        patch.object(cli, "Hdc") as connection, patch.object(cli, "capture") as collector, \
                        contextlib.redirect_stdout(io.StringIO()):
                    collector.return_value = {"path": Path(directory), "page_observation": None}
                    self.assertEqual(cli.main(), 0)
                collector.assert_called_once_with(connection.return_value, Path("captures"), 0, rules, None)

    def test_explicit_target_is_paired_and_transient_images_never_reach_stdout(self):
        output = io.StringIO()
        with patch("sys.argv", ["capture.py", "--target-window-id", "92", "--target-bundle-name", "app.test"]), \
                patch.object(cli, "Hdc") as connection, patch.object(cli, "capture") as collector, \
                contextlib.redirect_stdout(output):
            collector.return_value = {"path": Path("saved"), "page_observation": {"before_image": "secret-image"}}
            self.assertEqual(cli.main(), 0)
        collector.assert_called_once_with(connection.return_value, Path("captures"), 0, [],
                                          {"window_id": 92, "bundle_name": "app.test"})
        self.assertEqual(output.getvalue().strip(), "saved")

    def test_project_does_not_accept_arbitrary_pixel_rectangles(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project.toml"
            project.write_text('[dataset]\nstability_regions = [{bounds=[0,0,3120,2080]}]', encoding="utf-8")
            with patch("sys.argv", ["capture.py", "--project", str(project)]), patch.object(cli, "capture") as collect:
                self.assertEqual(cli.main(), 1)
                collect.assert_not_called()
