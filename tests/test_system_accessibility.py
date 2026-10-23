"""系统无障碍 Inspector 来源、会话归属和原始证据读取测试。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from har_capture.arkui import count_arkui_nodes, parse_arkui, parse_forest
from har_capture.hdc import CaptureError
from har_capture.system_accessibility import (
    collect_inspector, normalize_inspector_tree, read_window_accessibility, save_window_accessibility,
    select_inspector_window,
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


def inspector_forest_text(target_later=False):
    """保留实际双根深度与叶子属性格式，正文仅使用匿名合成文本。"""
    text, _root = inspector_fixture()
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith("|->"):
            line = "  " + line
        elif line.startswith("        |"):
            line = "          " + line.strip().removeprefix("|").lstrip()
        lines.append(line)
    main = "\r\n".join(lines) + "\r\n"
    empty = ("  |-> root childSize:0\r\n      ID: 10602\r\n      compid: \r\n"
             "      top: 0.000000\r\n      left: 0.000000\r\n      width: 3120.000000\r\n"
             "      height: 2080.000000\r\n      visible: 1\r\n")
    return "WinId: 9\r\nPid: 200\r\nbundleName:com.example.system\r\n" + (
        empty + main if target_later else main + empty)


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
        self.inspector = {"roots": [root], "raw": artifact_info(self.directory, path)}

    def test_selection_requires_both_native_accessibility_id_and_session_component(self):
        selected = select_inspector_window(self.inspector["roots"], self.window)
        self.assertEqual(selected["attributes"]["ID"], "90")
        for altered in ({"ui_node_id": 91}, {"ui_node_id": 900}):
            with self.assertRaises(CaptureError):
                select_inspector_window(self.inspector["roots"], {**self.window, "session": altered})
        duplicated = copy.deepcopy(self.inspector["roots"][0])
        duplicated["children"].append(copy.deepcopy(duplicated["children"][0]))
        with self.assertRaises(CaptureError):
            select_inspector_window([duplicated], self.window)

    def test_normalization_preserves_precise_coordinates_and_original_properties(self):
        selected = select_inspector_window(self.inspector["roots"], self.window)
        normalized = normalize_inspector_tree(selected, self.window)
        self.assertEqual(normalized["screen_bounds"], {"x": 12.5, "y": 20.25, "width": 100.5, "height": 40.5})
        self.assertEqual(normalized["attributes"]["bounds"], "[12,20][113,61]")
        self.assertEqual(normalized["source_attributes"], selected["source_attributes"])
        self.assertEqual(normalized["raw"], selected["raw"])
        self.assertEqual(normalized["children"][0]["attributes"]["text"], "Test label")
        self.assertEqual(normalized["children"][0]["attributes"]["hostWindowId"], "9")
        self.assertNotIn("hostWindowId", selected["attributes"])

    def test_collect_and_save_keep_complete_crlf_forest_and_select_the_actual_system_session(self):
        for target_later in (False, True):
            with self.subTest(target_later=target_later):
                stage = self.directory / ("later" if target_later else "first")
                (stage / "accessibility-inspector").mkdir(parents=True)
                raw = inspector_forest_text(target_later).encode("utf-8")
                hdc = fake_hdc({"inspector_process-200": raw}, command_outputs={
                    "inspector_process-200": "HAR_CAPTURE_INSPECTOR_READY"})
                inspector = collect_inspector(hdc, stage, {"layout": "/fixture"}, self.window)
                roots = inspector["roots"]
                original = copy.deepcopy(roots)
                self.assertEqual(len(roots), 2)
                self.assertEqual(sum(count_arkui_nodes(root) for root in roots), 4)
                empty = next(root for root in roots if root["attributes"]["ID"] == "10602")
                self.assertEqual(empty["children"], [])
                empty_raw = empty["raw"]
                self.assertTrue(empty_raw.startswith("      ID: 10602\r\n      compid: \r\n"))
                self.assertNotIn("|", empty_raw)
                selected = select_inspector_window(roots, self.window)
                self.assertIs(selected, roots[1 if target_later else 0]["children"][0])
                record = save_window_accessibility(stage, self.window, inspector)
                saved = read_window_accessibility(stage, {**self.window, "trees": {"accessibility": record}})
                self.assertEqual(record["selection"]["source_line"], selected["source_line"])
                self.assertGreater(selected["source_line"], roots[1 if target_later else 0]["source_line"])
                self.assertEqual(record["node_count"], 2)
                self.assertEqual((stage / record["raw"]["file"]).read_bytes(), raw)
                self.assertEqual(saved["raw"], selected["raw"])
                self.assertEqual(saved["children"][0]["raw"], selected["children"][0]["raw"])
                self.assertIn("\r\n", saved["raw"])
                self.assertNotIn("\n", saved["raw"].replace("\r\n", ""))
                self.assertIn("\r\n", saved["children"][0]["raw"])
                self.assertNotIn("\n", saved["children"][0]["raw"].replace("\r\n", ""))
                self.assertEqual(empty["raw"], empty_raw)
                self.assertEqual(roots, original)

    def test_duplicate_system_target_in_distinct_roots_is_not_uniquely_owned(self):
        roots = parse_forest(inspector_forest_text())
        roots.append(copy.deepcopy(roots[0]))
        original = copy.deepcopy(roots)
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "uniquely identify"):
            select_inspector_window(roots, self.window)
        self.assertEqual(roots, original)

    def test_ambiguous_target_properties_are_rejected_even_with_another_valid_root(self):
        text = inspector_forest_text()
        valid = parse_forest(text)[0]
        for label, repeated in (("ID", "90"), ("ID", "900"), ("compid", "session9"), ("compid", "session99")):
            with self.subTest(label=label, repeated=repeated):
                value = "90" if label == "ID" else "session9"
                old = f"      | {label}: {value}\r\n"
                self.assertIn(old, text)
                roots = parse_forest(text.replace(old, old + f"      | {label}: {repeated}\r\n", 1))
                roots.append(copy.deepcopy(valid))
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "ambiguous identity"):
                    select_inspector_window(roots, self.window)

    def test_malformed_later_inspector_root_is_rejected_after_saving_the_complete_raw_response(self):
        raw = inspector_forest_text().replace("  |-> root childSize:0", "  |-> root childSize:invalid").encode()
        hdc = fake_hdc({"inspector_process-200": raw}, command_outputs={
            "inspector_process-200": "HAR_CAPTURE_INSPECTOR_READY"})
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "Malformed ArkUI node header"):
            collect_inspector(hdc, self.directory, {"layout": "/fixture"}, self.window)
        source = self.directory / "accessibility-inspector/process-200-inspector.txt"
        self.assertEqual(source.read_bytes(), raw)
        self.assertFalse((self.directory / "accessibility-inspector/window-9.json").exists())

    def test_application_inspector_requires_one_root_instead_of_merging_or_dropping_extra_roots(self):
        application = {"window_id": 92, "is_system": False}
        root = parse_arkui(application_inspector().decode())
        self.assertIs(select_inspector_window([root], application), root)
        for roots in ([], [root, copy.deepcopy(root)]):
            with self.subTest(root_count=len(roots)), self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(CaptureError, "cannot assign multiple roots"):
                    select_inspector_window(roots, application)

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
