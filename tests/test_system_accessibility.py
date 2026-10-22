"""系统无障碍 Inspector 来源、会话归属和原始证据读取测试。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from har_capture.arkui import parse_arkui
from har_capture.hdc import CaptureError
from har_capture.system_accessibility import (
    normalize_inspector_tree, read_window_accessibility, save_window_accessibility, select_inspector_window,
    supplement_missing_windows,
)
from har_capture.tree_capture import artifact_info
from tests.support import fake_hdc, raw_arkui


def inspector_fixture():
    """构造真实 Inspector 属性命名的匿名系统全树。"""
    properties = ("      | ID: 90\n      | compid: session9\n      | left: 12.5\n      | top: 20.25\n"
                  "      | width: 100.5\n      | height: 40.5\n      | text: \n      | visible: 1\n")
    text = ("|-> root childSize:1\n    | ID: 0\n  |-> WindowScene childSize:1\n" + properties
            + "    |-> Text childSize:0\n        | ID: 91\n        | compid: label\n"
            + "        | left: 15\n        | top: 23\n        | width: 30\n        | height: 10\n"
            + "        | text: Test label\n        | visible: 1\n")
    return text, parse_arkui(text)


def application_inspector(window_id=92, process_id=202, left=10, bundle="com.example.app"):
    """普通应用响应已经绑定单窗，根 ID 可以与同进程另一窗口相同。"""
    text = (f"WinId: {window_id}\nPid: {process_id}\nbundleName:{bundle}\n"
            "|-> root childSize:1\n    | ID: 0\n    | compid: \n"
            f"    | left: {left}\n    | top: 20\n    | width: 100\n    | height: 40\n"
            "  |-> Text childSize:0\n      | ID: 1\n      | compid: label\n"
            f"      | left: {left + 2}\n      | top: 23\n      | width: 30\n      | height: 10\n"
            f"      | text: Window {window_id}\n      | visible: 1\n")
    return text.encode("utf-8")


class SystemAccessibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "accessibility-inspector").mkdir()
        self.window = {"window_id": 9, "display_id": 0, "process_id": 200, "bundle_name": "com.example.system",
                       "is_system": True, "session": {"ui_node_id": 90}}
        text, root = inspector_fixture()
        path = self.directory / "accessibility-inspector/process-200-inspector.txt"
        path.write_text(text, encoding="utf-8")
        self.inspector = {"root": root, "raw": artifact_info(self.directory, path)}

    def test_selection_requires_both_native_accessibility_id_and_session_component(self):
        selected = select_inspector_window(self.inspector["root"], self.window)
        self.assertEqual(selected["attributes"]["ID"], "90")
        for altered in ({"ui_node_id": 91}, {"ui_node_id": 900}):
            with self.assertRaises(CaptureError):
                select_inspector_window(self.inspector["root"], {**self.window, "session": altered})
        duplicated = copy.deepcopy(self.inspector["root"])
        duplicated["children"].append(copy.deepcopy(duplicated["children"][0]))
        with self.assertRaises(CaptureError):
            select_inspector_window(duplicated, self.window)

    def test_normalization_preserves_precise_coordinates_and_original_properties(self):
        selected = select_inspector_window(self.inspector["root"], self.window)
        normalized = normalize_inspector_tree(selected, self.window)
        self.assertEqual(normalized["screen_bounds"], {"x": 12.5, "y": 20.25, "width": 100.5, "height": 40.5})
        self.assertEqual(normalized["attributes"]["bounds"], "[12,20][113,61]")
        self.assertEqual(normalized["source_attributes"], selected["source_attributes"])
        self.assertEqual(normalized["raw"], selected["raw"])
        self.assertEqual(normalized["children"][0]["attributes"]["text"], "Test label")
        self.assertEqual(normalized["children"][0]["attributes"]["hostWindowId"], "9")
        self.assertNotIn("hostWindowId", selected["attributes"])

    def test_saved_system_tree_has_explicit_source_and_round_trips_through_verifier(self):
        record = save_window_accessibility(self.directory, self.window, self.inspector)
        window = {**self.window, "trees": {"accessibility": record}}
        root = read_window_accessibility(self.directory, window)
        self.assertEqual(record["node_count"], 2)
        self.assertEqual(record["json_pointer"], "")
        self.assertIn("AccessibilityManager", record["source"])
        self.assertEqual(record["raw"], self.inspector["raw"])
        self.assertEqual(root["attributes"]["bundleName"], self.window["bundle_name"])
        (self.directory / record["file"]).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "digest"):
            read_window_accessibility(self.directory, window)

    def test_verifier_rejects_wrong_bundle_and_path_escape(self):
        record = save_window_accessibility(self.directory, self.window, self.inspector)
        with self.assertRaisesRegex(CaptureError, "ownership"):
            read_window_accessibility(self.directory, {**self.window, "bundle_name": "wrong.bundle",
                                                       "trees": {"accessibility": record}})
        record["file"] = "../escape.json"
        with self.assertRaisesRegex(CaptureError, "escapes"):
            read_window_accessibility(self.directory, {**self.window, "trees": {"accessibility": record}})

    def test_missing_application_windows_in_one_process_keep_independent_native_trees(self):
        stage = self.directory / "capture"
        (stage / "arkui").mkdir(parents=True)
        windows = [{"window_id": window_id, "process_id": 202, "display_id": 0, "is_system": False,
                    "z_order": window_id, "bounding_box": {"x": left, "y": 20, "width": 100, "height": 40},
                    "session": {"window_id": window_id, "process_id": 202, "display_id": 0,
                                "is_system": False, "ui_node_id": window_id * 10,
                                "is_visible": True, "rs_visible": True, "visibility_state": 1}}
                   for window_id, left in ((92, 10), (93, 200))]
        metadata = {"windows": [], "window_inventory": windows, "files": {},
                    "display": {"id": 0, "width": 3120, "height": 2080}}
        hdc = fake_hdc({"inspector_window-92": application_inspector(),
                        "inspector_window-93": application_inspector(93, left=200),
                        "arkui_response_93": raw_arkui(93)}, command_outputs={
                            "inspector_window-92": "HAR_CAPTURE_INSPECTOR_READY",
                            "inspector_window-93": "HAR_CAPTURE_INSPECTOR_READY"})
        supplement_missing_windows(hdc, stage, {"layout": "/fixture"}, metadata)
        self.assertEqual([window["window_id"] for window in metadata["windows"]], [93, 92])
        raw_files = []
        for window in metadata["windows"]:
            record = window["trees"]["accessibility"]
            self.assertEqual(record["selection"]["kind"], "window_response")
            self.assertEqual(record["selection"]["window_id"], window["window_id"])
            root = read_window_accessibility(stage, window)
            self.assertEqual(root["children"][0]["attributes"]["text"], f"Window {window['window_id']}")
            self.assertEqual(record["node_count"], 2)
            self.assertEqual(window["trees"]["arkui"]["session"], window["session"])
            raw_files.append(record["raw"]["file"])
        self.assertEqual(len(set(raw_files)), 2)
        commands = [record["name"] for record in hdc.records]
        self.assertEqual(sum(name.startswith("inspector_") for name in commands), 2)
        self.assertIn("dump_arkui_92", commands)
        self.assertIn("dump_arkui_93", commands)


if __name__ == "__main__":
    unittest.main()
