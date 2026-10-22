"""验证完整采集发布、失败证据和时序一致性说明。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from har_capture.collector import capture
from har_capture.hdc import CaptureError
from tests.support import (FIXTURES, fake_hdc, png_bytes, raw_accessibility, raw_arkui, raw_layout,
                           raw_system_arkui, system_session_details)


class CollectorTests(unittest.TestCase):
    """通过设备替身运行完整采集流程。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / "captures"

    def read_metadata(self, directory):
        return json.loads((directory / "metadata.json").read_text(encoding="utf-8"))

    def assert_failed_only(self):
        directories = list(self.output.iterdir())
        self.assertEqual(len(directories), 1)
        failed = directories[0]
        self.assertTrue(failed.name.endswith(".failed"))
        self.assertFalse((failed / "metadata.json").exists())
        error = json.loads((failed / "error.json").read_text(encoding="utf-8"))
        self.assertEqual(error["status"], "failed")
        self.assertTrue((failed / "commands.json").is_file())
        return failed

    def test_each_capture_has_independent_files_and_preserves_layout_bytes(self):
        first_hdc, second_hdc = fake_hdc(), fake_hdc()
        first = capture(first_hdc, self.output)
        second = capture(second_hdc, self.output)
        original = raw_layout()
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.output.iterdir())), 2)
        for directory in (first, second):
            self.assertEqual((directory / "layout.json").read_bytes(), original)
            self.assertEqual((directory / "accessibility.json").read_bytes(), raw_accessibility())
            self.assertEqual((directory / "arkui/window-92.txt").read_bytes(), raw_arkui())
            self.assertEqual((directory / "arkui/window-80.txt").read_bytes(), raw_arkui(80, 201))
            self.assertEqual((directory / "session-details.txt").read_text(encoding="utf-8"),
                             (FIXTURES / "session-details.txt").read_text(encoding="utf-8"))
            metadata = self.read_metadata(directory)
            self.assertEqual(metadata["status"], "complete")
            self.assertEqual(metadata["files"]["layout"]["sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual(metadata["consistency"], {"atomic": False})
            self.assertEqual(metadata["z_order_source"], "WindowManagerService.ZOrd")
            self.assertTrue(metadata["device_cleanup"]["success"])
            self.assertEqual(set(metadata["capture_times"]),
                             {"screenshot", "layout", "windows", "accessibility",
                              "dump_arkui_92", "dump_arkui_80", "session_details"})
            self.assertEqual(metadata["capture_times"]["layout"]["duration_seconds"], 0.01)
            for window in metadata["windows"]:
                self.assertEqual(set(window["trees"]), {"accessibility", "arkui"})
                self.assertEqual(window["trees"]["arkui"]["node_count"], 2)
                self.assertEqual(window["trees"]["arkui"]["scope"], "window")
                self.assertFalse(window["trees"]["arkui"]["session"]["is_system"])
                self.assertEqual(window["trees"]["accessibility"]["node_count"], 1)
                tree_file = directory / window["trees"]["arkui"]["file"]
                window_id = window["window_id"]
                self.assertEqual(tree_file.name, f"window-{window_id}.json")
                tree = json.loads(tree_file.read_bytes())
                self.assertEqual(tree["children"][0]["attributes"]["Content"], "原始组件数据")
                self.assertEqual(tree["children"][0]["attributes"]["Hint"], ["第一项", "第二项"])
                self.assertEqual(metadata["files"][f"arkui_{window_id}"]["file"],
                                 f"arkui/window-{window_id}.json")
                self.assertEqual(metadata["files"][f"arkui_raw_{window_id}"], window["trees"]["arkui"]["raw"])
                self.assertEqual(window["trees"]["arkui"]["sha256"],
                                 hashlib.sha256(tree_file.read_bytes()).hexdigest())
        first_remote = first_hdc.receive.call_args_list[0].args[0]
        second_remote = second_hdc.receive.call_args_list[0].args[0]
        self.assertNotEqual(first_remote, second_remote)

    def test_reused_connection_keeps_only_current_capture_commands(self):
        hdc = fake_hdc()
        hdc.records.append({"name": "previous"})
        first = capture(hdc, self.output)
        first_commands = json.loads((first / "commands.json").read_text(encoding="utf-8"))
        second = capture(hdc, self.output)
        second_commands = json.loads((second / "commands.json").read_text(encoding="utf-8"))
        required = {"screenshot", "layout", "windows", "accessibility", "receive_accessibility",
                    "dump_arkui_92", "dump_arkui_80", "receive_arkui_response_92", "receive_arkui_response_80",
                    "cleanup", "session_details"}
        first_names = [record["name"] for record in first_commands]
        second_names = [record["name"] for record in second_commands]
        self.assertTrue(required.issubset(first_names))
        self.assertEqual(first_names, second_names)
        self.assertEqual(len(first_names), len(set(first_names)))
        self.assertNotIn(first.name, json.dumps(second_commands))
        self.assertNotIn("previous", {record["name"] for record in first_commands})

    def test_published_window_identity_uses_the_same_capture_arkui_session(self):
        connections = (
            fake_hdc(),
            fake_hdc({"arkui_response_92": raw_system_arkui()},
                     command_outputs={"session_details": system_session_details()}),
        )
        for hdc in connections:
            with self.subTest(system_window=hdc is connections[1]):
                directory = capture(hdc, self.output)
                metadata = self.read_metadata(directory)
                identities = {window["window_id"]: window["ui_node_id"] for window in metadata["windows"]}
                self.assertEqual(identities, {80: 800, 92: 920})
                for window in metadata["windows"]:
                    session = window["trees"]["arkui"]["session"]
                    self.assertEqual(window["ui_node_id"], session["ui_node_id"])
                    for field in ("window_id", "process_id", "display_id"):
                        self.assertEqual(window[field], session[field])
                self.assertEqual(sum(record["name"] == "session_details" for record in hdc.records), 1)

    def test_unconfirmed_cleanup_is_recorded_without_discarding_valid_capture(self):
        for reply in ("", "rm: Permission denied", "prefix HAR_CAPTURE_CLEANUP_OK suffix"):
            with self.subTest(reply=reply):
                hdc = fake_hdc()
                original_run = hdc.run.side_effect

                def run(arguments, name, targeted=True):
                    result = original_run(arguments, name, targeted)
                    return reply if name == "cleanup" else result

                hdc.run.side_effect = run
                with self.assertLogs("har_capture.collector", level="ERROR"):
                    directory = capture(hdc, self.output)
                metadata = self.read_metadata(directory)
                self.assertEqual(metadata["status"], "complete")
                self.assertFalse(metadata["device_cleanup"]["success"])
                self.assertEqual(set(metadata["device_cleanup"]["remote_files"]),
                                 {"screenshot", "layout", "accessibility", "arkui_92", "arkui_80"})
                self.assertEqual((directory / "layout.json").read_bytes(), raw_layout())

    def test_one_screenshot_is_captured_first_and_paired_with_all_window_evidence(self):
        hdc = fake_hdc()
        directory = capture(hdc, self.output)
        metadata = self.read_metadata(directory)
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["capture_id"], directory.name)
        self.assertEqual(metadata["consistency"], {"atomic": False})
        self.assertEqual(list(directory.rglob("*.png")), [directory / "screenshot.png"])
        commands = json.loads((directory / "commands.json").read_bytes())
        screenshots = [record for record in commands if "screenCap" in record.get("command", [])]
        self.assertEqual(len(screenshots), 1)
        self.assertEqual(commands[0]["name"], "screenshot")
        self.assertEqual([call.args[2] for call in hdc.receive.call_args_list].count("receive_screenshot"), 1)
        self.assertNotIn("screenshot_after", metadata["files"])
        self.assertNotIn("screenshot_after", metadata["capture_times"])
        self.assertNotIn("screenshot_after", {record["name"] for record in commands})
        self.assertNotIn("receive_screenshot_after", {record["name"] for record in commands})
        self.assertEqual([value["file"] for value in metadata["files"].values() if value["file"].endswith(".png")],
                         ["screenshot.png"])
        for artifact in metadata["files"].values():
            self.assertTrue((directory / artifact["file"]).is_file())
        self.assertEqual((directory / "layout.json").read_bytes(), raw_layout())

    def test_required_stage_failures_never_publish_complete_capture(self):
        for stage in ("screenshot", "layout", "windows", "receive_layout", "accessibility",
                      "receive_accessibility", "dump_arkui_92", "receive_arkui_response_92", "session_details"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temporary:
                self.output = Path(temporary) / "captures"
                with self.assertRaises(CaptureError):
                    capture(fake_hdc(failure=stage), self.output)
                self.assert_failed_only()

    def test_invalid_payloads_never_publish_complete_capture(self):
        payloads = ({"layout": b"{"}, {"layout": b"{}"}, {"screenshot": b"not a PNG"},
                    {"screenshot": png_bytes()[:-16]},
                    {"accessibility": b"{}"}, {"arkui_response_92": b"WinId: 92\nPid: 202\n"})
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                self.output = Path(temporary) / "captures"
                with self.assertRaises(Exception):
                    capture(fake_hdc(payload), self.output)
                self.assert_failed_only()

    def test_missing_transferred_file_does_not_reuse_old_capture(self):
        previous = capture(fake_hdc(), self.output)
        hdc = fake_hdc()
        hdc.receive.side_effect = None
        with self.assertRaises(FileNotFoundError):
            capture(hdc, self.output)
        self.assertEqual(self.read_metadata(previous)["status"], "complete")
        failed = next(directory for directory in self.output.iterdir() if directory.name.endswith(".failed"))
        self.assertFalse((failed / "metadata.json").exists())

    def test_unconfirmed_success_and_user_interrupt_keep_failure_evidence(self):
        for result in ("Command failed", KeyboardInterrupt()):
            with self.subTest(result=repr(result)), tempfile.TemporaryDirectory() as temporary:
                self.output = Path(temporary) / "captures"
                hdc = fake_hdc()
                hdc.run.side_effect = result if isinstance(result, BaseException) else None
                hdc.run.return_value = result
                with self.assertRaises((CaptureError, KeyboardInterrupt)):
                    capture(hdc, self.output)
                self.assert_failed_only()

    def test_failed_final_publication_removes_complete_metadata(self):
        original = Path.rename

        def reject_publication(source, target):
            if not str(target).endswith(".failed"):
                raise OSError("Injected publication failure")
            return original(source, target)

        with patch.object(Path, "rename", reject_publication), self.assertRaises(OSError):
            capture(fake_hdc(), self.output)
        self.assert_failed_only()


if __name__ == "__main__":
    unittest.main()
