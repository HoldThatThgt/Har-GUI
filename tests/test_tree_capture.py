"""验证完整标准输出、原生来源和窗口归属，禁止私有文件分流。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from har_capture.hdc import CaptureError
from har_capture.metadata import parse_windows
from har_capture.tree_capture import collect_accessibility, collect_arkui, collect_trees
from tests.support import (
    FIXTURES, fake_hdc, layout_data, native_arkui, native_system_arkui, raw_accessibility, raw_arkui, raw_system_arkui,
    system_session_details, window_node,
)


class TreeCaptureTests(unittest.TestCase):
    """以官方响应格式替代设备，不访问实机。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.stage = Path(self.temporary.name)
        self.remote = {"layout": "/data/local/tmp/test-capture.json"}
        self.window = parse_windows((FIXTURES / "windows.txt").read_text(encoding="utf-8"))[92]
        self.window["bundle_name"] = "com.example.settings"


    def collect_component_tree(self, hdc):
        (self.stage / "arkui").mkdir()
        return collect_arkui(hdc, self.stage, self.remote, self.window)

    def test_stdout_tree_preserves_entire_response_bytes(self):
        hdc = fake_hdc()
        evidence = self.collect_component_tree(hdc)
        self.assertEqual(evidence["origin"], {"kind": "stdout"})
        self.assertEqual(evidence["file"], "arkui/window-92.json")
        self.assertEqual(evidence["raw"]["file"], "arkui/window-92.txt")
        self.assertEqual(evidence["node_count"], 2)
        self.assertEqual(evidence["raw"]["sha256"], hashlib.sha256(raw_arkui()).hexdigest())
        self.assertEqual((self.stage / evidence["raw"]["file"]).read_bytes(), raw_arkui())
        self.assertEqual((self.stage / evidence["response"]["file"]).read_bytes(), raw_arkui())
        saved = (self.stage / evidence["file"]).read_bytes()
        root = json.loads(saved)
        self.assertEqual(hashlib.sha256(saved).hexdigest(), evidence["sha256"])
        self.assertEqual(len(saved), evidence["bytes"])
        self.assertEqual(root["attributes"]["type"], "root")
        self.assertEqual(root["declared_child_count"], 1)
        self.assertEqual(root["source_line"], 4)
        leaf = root["children"][0]
        self.assertEqual(leaf["attributes"]["Content"], "原始组件数据")
        self.assertEqual(leaf["attributes"]["Hint"], ["第一项", "第二项"])
        self.assertEqual(leaf["source_attributes"]["Hint"], ["第一项", "第二项"])
        self.assertEqual(leaf["children"], [])
        self.assertIn("        Hint: 第一项\r\n        Hint: 第二项\r\n", leaf["raw"])
        receives = [call.args[2] for call in hdc.receive.call_args_list]
        self.assertEqual(receives, ["receive_arkui_response_92", "receive_geometry_92"])
        self.assertEqual(self.remote["arkui_92"], self.remote["layout"] + "-arkui-92.txt")


    def test_response_must_match_both_process_and_window(self):
        for response in (raw_arkui(window_id=80), raw_arkui(process_id=201), b"|-> root childSize:0\n"):
            with self.subTest(response=response), tempfile.TemporaryDirectory() as temporary:
                self.stage = Path(temporary)
                with self.assertRaises(CaptureError):
                    self.collect_component_tree(fake_hdc({"arkui_response_92": response}))
                self.assertFalse((self.stage / "arkui/window-92.txt").exists())

    def test_unconfirmed_component_dump_is_not_received(self):
        hdc = fake_hdc(command_outputs={"dump_arkui_92": "No data was produced"})
        with self.assertRaises(CaptureError):
            self.collect_component_tree(hdc)
        hdc.receive.assert_not_called()

    def test_complete_stdout_uses_native_coverage_without_stat_find_or_application_file_transfers(self):
        hdc = fake_hdc()
        evidence = self.collect_component_tree(hdc)
        self.assertEqual([record["name"] for record in hdc.records],
                         ["dump_arkui_92", "receive_arkui_response_92", "geometry_92", "receive_geometry_92"])
        commands = "\n".join(str(record.get("command", [])) for record in hdc.records)
        self.assertIn("-w 92 -default -all", commands)
        self.assertIn("-w 92 -allInfoWithParamConfig", commands)
        self.assertNotIn("find ", commands)
        self.assertNotIn("stat ", commands)
        self.assertNotIn("/proc/", commands)
        self.assertNotIn("arkui.dump", commands)
        self.assertEqual(evidence["source"], "WindowManagerService.-default.-all")
        self.assertEqual(evidence["forest_node_count"], 2)
        self.assertEqual(evidence["forest"]["file"], "arkui/window-92-forest.json")
        native = json.loads((self.stage / evidence["coordinate_evidence"]["arkui_geometry"]["file"]).read_bytes())
        self.assertEqual(native["appInfo"], {"WindowID": 92, "BundleName": "com.example.settings"})
        self.assertEqual(native["$children"][0]["$ID"], 1)

    def test_missing_complete_stdout_cannot_be_replaced_by_an_otherwise_valid_native_source(self):
        hdc = fake_hdc({"arkui_response_92": raw_arkui(include_tree=False)})
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            self.collect_component_tree(hdc)
        self.assertEqual([record["name"] for record in hdc.records],
                         ["dump_arkui_92", "receive_arkui_response_92", "geometry_92", "receive_geometry_92"])
        self.assertFalse((self.stage / "arkui/window-92.json").exists())

    def test_native_source_missing_a_real_raw_child_cannot_publish_a_window_tree(self):
        for children in ([], [{"$type": "Text", "$ID": 999}]):
            native = {"$type": "root", "$ID": 0, "$children": children}
            hdc = fake_hdc({"geometry_92": native_arkui(root=native)})
            with self.subTest(children=children), tempfile.TemporaryDirectory() as temporary:
                self.stage = Path(temporary)
                with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                    self.collect_component_tree(hdc)
                self.assertFalse((self.stage / "arkui/window-92.json").exists())
                self.assertEqual((self.stage / "arkui/window-92.txt").read_bytes(), raw_arkui())

    def test_complete_native_application_identity_must_match_the_requested_window(self):
        for payload in (native_arkui(root={"$type": "root", "$ID": 0}, bundle="app.other"),
                        native_arkui().replace(b'"WindowID": 92', b'"WindowID": 80')):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                self.stage = Path(temporary)
                with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                    self.collect_component_tree(fake_hdc({"geometry_92": payload}))
                self.assertFalse((self.stage / "arkui/window-92.json").exists())

    def test_full_forest_is_independently_indexed_and_keeps_unselected_source_roots(self):
        payload = raw_arkui() + b"|-> Detached,9000,1 childSize:0\r\n      ID: 9000\r\n"
        evidence = self.collect_component_tree(fake_hdc({"arkui_response_92": payload}))
        forest_file = self.stage / evidence["forest"]["file"]
        forest = json.loads(forest_file.read_bytes())
        window = json.loads((self.stage / evidence["file"]).read_bytes())
        self.assertEqual([node["attributes"]["ID"] for node in forest], ["0", "9000"])
        self.assertEqual(evidence["forest_node_count"], 3)
        self.assertEqual(evidence["node_count"], 2)
        self.assertEqual(evidence["forest"]["sha256"], hashlib.sha256(forest_file.read_bytes()).hexdigest())
        self.assertEqual(window["source_header"], "root,0,0")
        self.assertEqual(window["attributes"]["type"], "root")
        self.assertEqual(window["children"][0]["source_association"]["native_parent_id"], "0")
        self.assertEqual((self.stage / evidence["raw"]["file"]).read_bytes(), payload)

    def test_accessibility_array_is_saved_without_reformatting(self):
        original = raw_accessibility()
        trees = collect_accessibility(fake_hdc(), self.stage, self.remote, 0)
        self.assertEqual((self.stage / "accessibility.json").read_bytes(), original)
        self.assertEqual(trees[80]["json_pointer"], "/0")
        self.assertEqual(trees[92]["json_pointer"], "/1")
        self.assertEqual(trees[92]["bundle_name"], "com.example.settings")
        self.assertEqual(trees[92]["node_count"], 1)
        self.assertEqual(self.remote["accessibility"], self.remote["layout"] + "-accessibility.json")

    def test_accessibility_wrong_bundle_cannot_be_attached(self):
        wrong_bundle = [window_node("92", "com.example.other")]
        hdc = fake_hdc({"accessibility": raw_accessibility(wrong_bundle)})
        with self.assertRaises(CaptureError):
            collect_trees(hdc, self.stage, self.remote, [self.window], 0)
        self.assertNotIn("dump_arkui_92", [record["name"] for record in hdc.records])

    def test_accessibility_missing_windows_remain_for_native_window_collection(self):
        for roots in ([window_node("80", "com.example.reader")], []):
            with self.subTest(roots=roots), tempfile.TemporaryDirectory() as temporary:
                hdc = fake_hdc({"accessibility": raw_accessibility(roots)})
                self.assertEqual(collect_trees(hdc, Path(temporary), self.remote, [self.window], 0), {})
                self.assertNotIn("dump_arkui_92", [record["name"] for record in hdc.records])

    def test_accessibility_duplicate_windows_and_wrong_descendant_ownership_are_rejected(self):
        wrong_descendant = window_node()
        wrong_descendant["children"] = [window_node("80", "com.example.reader")]
        cases = ([window_node(), window_node()], [wrong_descendant])
        for roots in cases:
            with self.subTest(roots=roots), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(CaptureError):
                    collect_accessibility(fake_hdc({"accessibility": raw_accessibility(roots)}),
                                          Path(temporary), self.remote, 0)

    def test_full_tree_collection_keeps_independent_sources_for_each_window(self):
        inventory = parse_windows((FIXTURES / "windows.txt").read_text(encoding="utf-8"))
        windows = []
        for root in layout_data()["children"]:
            window = dict(inventory[int(root["attributes"]["hostWindowId"])])
            window["bundle_name"] = root["attributes"]["bundleName"]
            windows.append(window)
        result = collect_trees(fake_hdc(), self.stage, self.remote, windows, 0)
        self.assertEqual(set(result), {80, 92})
        for window_id, trees in result.items():
            self.assertEqual(set(trees), {"accessibility", "arkui"})
            self.assertEqual(trees["accessibility"]["node_count"], 1)
            self.assertEqual(trees["arkui"]["node_count"], 2)
            self.assertEqual(trees["arkui"]["file"], f"arkui/window-{window_id}.json")
            self.assertEqual(trees["arkui"]["raw"]["file"], f"arkui/window-{window_id}.txt")
            self.assertEqual(trees["accessibility"]["source"], "UITest.dumpLayout.-i")
            self.assertEqual(trees["arkui"]["source"], "WindowManagerService.-default.-all")
            self.assertEqual(trees["arkui"]["scope"], "window")
            self.assertEqual(trees["arkui"]["display_id"], 0)
            self.assertEqual(trees["arkui"]["session"]["window_id"], window_id)
            self.assertFalse(trees["arkui"]["session"]["is_system"])

    def test_system_window_uses_complete_tree_and_keeps_only_its_original_slice(self):
        details = system_session_details()
        original = raw_system_arkui()
        hdc = fake_hdc({"arkui_response_92": original, "geometry_92": native_system_arkui()},
                       command_outputs={"session_details": details})
        result = collect_trees(hdc, self.stage, self.remote, [self.window], 0)
        evidence = result[92]["arkui"]
        self.assertEqual(evidence["scope"], "window")
        self.assertEqual(evidence["source"], "WindowManagerService.-default.-all")
        self.assertEqual(evidence["display_id"], 0)
        expected = {"is_system": True, "ui_node_id": 920, "process_id": 202, "window_id": 92, "display_id": 0}
        self.assertEqual({key: evidence["session"][key] for key in expected}, expected)
        self.assertEqual(evidence["origin"]["kind"], "source_slice")
        selection = evidence["origin"]["selection"]
        self.assertEqual(selection["accessibility_id"], 920)
        self.assertEqual(selection["node_id"], 192)
        self.assertEqual(selection["component_id"], "session92")
        start = original.index(b"  |-> WindowScene,192,920")
        end = original.index(b"  |-> WindowScene,194,922")
        self.assertEqual(selection["byte_offset"], start)
        self.assertEqual(selection["byte_length"], end - start)
        self.assertEqual((self.stage / evidence["raw"]["file"]).read_bytes(), original[start:end])
        root = json.loads((self.stage / evidence["file"]).read_bytes())
        self.assertEqual(root["attributes"]["compid"], "session92")
        self.assertEqual(root["children"][0]["attributes"]["Content"], "原始系统组件数据")
        self.assertEqual((self.stage / evidence["response"]["file"]).read_bytes(), original)
        self.assertEqual(evidence["node_count"], 2)
        self.assertEqual((self.stage / "session-details.txt").read_text(encoding="utf-8"), details)
        hdc.run.assert_any_call(["shell", "hidumper", "-s", "WindowManagerService", "-a", "-c"],
                               "session_details")
        dump = next(record for record in hdc.records if record["name"] == "dump_arkui_92")
        self.assertIn("-w 92 -default -all", dump["command"][1])
        self.assertNotIn("arkui_before_92", [record["name"] for record in hdc.records])

    def test_system_component_id_mismatch_cannot_publish_an_unrelated_tree(self):
        hdc = fake_hdc({"arkui_response_92": raw_system_arkui("session80")},
                       command_outputs={"session_details": system_session_details()})
        with self.assertRaises(CaptureError):
            collect_trees(hdc, self.stage, self.remote, [self.window], 0)
        self.assertFalse((self.stage / "arkui/window-92.txt").exists())

    def test_session_details_must_match_window_process_and_display(self):
        details = (FIXTURES / "session-details.txt").read_text(encoding="utf-8")
        mutations = {"window": details.replace("ExampleSettings|92", "ExampleSettings|93"),
                     "process": details.replace("202|10000|0|", "999|10000|0|"),
                     "display": details.replace("Display: 0.5|2|0|-1|0|", "Display: 0.5|2|0|-1|1|")}
        for label, output in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                hdc = fake_hdc(command_outputs={"session_details": output})
                with self.assertRaises(CaptureError):
                    collect_trees(hdc, Path(temporary), self.remote, [self.window], 0)
                self.assertNotIn("dump_arkui_92", [record["name"] for record in hdc.records])


if __name__ == "__main__":
    unittest.main()
