"""@brief 验证同批 SceneBoard 来源的身份、摘要、完整父链和共享采集。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from har_capture.arkui_selection import index_nodes
from har_capture.capture_facts import verify_tree_references
from har_capture.geometry_capture import collect_geometry_evidence
from har_capture.hdc import CaptureError
from har_capture.scene_evidence import load_scene_evidence, parse_source_node
from har_capture.arkui import node_property_text
from har_capture.metadata import artifact_info


def session_text(window_id, accessibility_id, children=False):
    """@brief 创建匿名会话节点及可选完整子树。"""
    text = (
        f"  |-> WindowScene childSize:{int(children)}\n"
        f"    | ID: {window_id}\n"
        f"    | AccessibilityId: {accessibility_id}\n"
        f"    | compid: session{window_id}\n"
    )
    if children:
        text += (
            "    |-> WindowScene childSize:1\n"
            "      | ID: 107\n"
            "      |-> Text childSize:0\n"
            "        | ID: 108\n"
            "        | Content: Example\n"
        )
    return text


def save_source(directory, window_id, process_id, targets):
    """@brief 保存带独立顶层根的完整匿名森林和原生几何。"""
    text = f"WinId: {window_id}\nPid: {process_id}\n|-> root childSize:{len(targets)}\n  | ID: 0\n"
    text += "".join(session_text(wid, uid, wid == 106) for wid, uid in targets)
    text += "|-> JsView childSize:0\n  | ID: 500\n"
    raw_path = directory / f"scene-{window_id}.txt"
    raw_path.write_text(text, encoding="utf-8")
    native_path = directory / f"geometry-{process_id}.json"
    native_path.write_text(json.dumps({"$type": "root", "$ID": 0}), encoding="utf-8")
    source = {"source_window_id": window_id, "process_id": process_id, "display_id": 0,
              "raw": artifact_info(directory, raw_path), "arkui_geometry": artifact_info(directory, native_path)}
    return source, text


def window_record(window_id, accessibility_id, process_id, source):
    """@brief 创建带明确系统分类和原始来源引用的匿名窗口。"""
    system = source is not None
    return {
        "window_id": window_id, "ui_node_id": accessibility_id, "process_id": process_id,
        "display_id": 0, "is_system": system,
        "trees": {"arkui": {
            "session": {"is_system": system}, "response": source["raw"] if system else {},
            "coordinate_evidence": {"arkui_geometry": source["arkui_geometry"] if system else {},
                                     "source_window_id": window_id, "process_id": process_id, "response": {}}
        }}
    }


class SceneEvidenceTests(unittest.TestCase):
    """@brief 验证来源事实不依赖文件名、旧窗口尺寸或补救路径。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="har-scene-evidence-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source, self.text = save_source(self.directory, 9, 100, ((23, 230), (9, 90), (106, 1060)))
        self.windows = [window_record(23, 230, 100, self.source), window_record(106, 1060, 200, None),
                        window_record(9, 90, 100, self.source)]
        self.attach_sources([self.source])

    def test_embedded_source_offsets_move_with_the_host_without_modifying_raw_text(self):
        raw = ("  |-> UIExtension,104,104 childSize:0\n"
               "      ID: 104\n"
               "      UI Extension info:\n"
               "      |-> root,0,0 childSize:0\n"
               "            ID: 0\n"
               "      ------------start print rsNode\n"
               "      CanvasNode[123]\n").encode()
        node = parse_source_node(raw, 1709)
        self.assertEqual(node["source_line"], 1709)
        block = node["embedded_trees"][0]
        self.assertEqual(block["source_line"], 1711)
        self.assertEqual(block["roots"][0]["source_line"], 1712)
        self.assertEqual(node["raw"], raw.decode().split("\n", 1)[1])
        own = node_property_text(node)
        self.assertIn("ID: 104", own)
        self.assertIn("CanvasNode[123]", own)
        self.assertNotIn("ID: 0", own)

    def attach_sources(self, sources):
        for window in self.windows:
            window["trees"]["arkui"]["coordinate_evidence"]["scene_sources"] = list(sources)

    def assert_rejected(self, expected, windows=None):
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, expected):
            load_scene_evidence(self.directory, self.windows if windows is None else windows)

    def rewrite_raw(self, text):
        path = self.directory / self.source["raw"]["file"]
        path.write_text(text, encoding="utf-8")
        self.source["raw"].update(artifact_info(self.directory, path))

    def test_shared_source_files_are_read_and_indexed_once_per_batch(self):
        reads, original = [], Path.read_bytes

        def measured(path):
            reads.append(path.name)
            return original(path)

        with patch.object(Path, "read_bytes", measured), patch(
                "har_capture.scene_evidence.index_nodes", wraps=index_nodes) as build_index:
            result = load_scene_evidence(self.directory, self.windows)
        self.assertCountEqual(reads, ["scene-9.txt", "geometry-100.json"])
        self.assertEqual(build_index.call_count, 1)
        self.assertEqual(set(result), {23, 9, 106})
        self.assertIs(result[106]["native"], result[9]["native"])

    def test_target_keeps_complete_children_global_lines_and_actual_source_window(self):
        result = load_scene_evidence(self.directory, self.windows)
        scene = result[106]
        self.assertEqual(scene["source"]["source_window_id"], 9)
        self.assertEqual([node["attributes"]["ID"] for node in scene["path"]], ["0", "106"])
        root, target = scene["path"]
        self.assertEqual(root["children"], [])
        self.assertEqual(root["source_line"], 3)
        self.assertEqual(target["source_line"], 13)
        child = target["children"][0]
        self.assertEqual(child["attributes"]["ID"], "107")
        self.assertEqual(child["source_line"], 17)
        self.assertEqual(child["children"][0]["attributes"]["ID"], "108")
        self.assertEqual(child["children"][0]["source_line"], 19)

    def test_modified_raw_or_geometry_bytes_are_rejected_by_digest(self):
        for kind in ("raw", "arkui_geometry"):
            with self.subTest(kind=kind):
                path = self.directory / self.source[kind]["file"]
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                self.assert_rejected("digest differs")
                path.write_bytes(original)

    def test_updated_digest_cannot_hide_wrong_or_duplicate_response_identity(self):
        for old, replacement in (("WinId: 9", "WinId: 23"), ("Pid: 100", "Pid: 999"),
                                 ("Pid: 100", "Pid: 100\nPid: 100")):
            with self.subTest(replacement=replacement):
                self.rewrite_raw(self.text.replace(old, replacement, 1))
                self.assert_rejected("header differs")
        self.rewrite_raw(self.text)

    def test_missing_or_empty_source_list_is_rejected(self):
        for value in (None, []):
            with self.subTest(value=value):
                windows = copy.deepcopy(self.windows)
                geometry = windows[1]["trees"]["arkui"]["coordinate_evidence"]
                if value is None:
                    del geometry["scene_sources"]
                else:
                    geometry["scene_sources"] = value
                self.assert_rejected("Missing SceneBoard coordinate sources", windows)

    def test_wrong_display_non_system_source_and_duplicate_source_are_rejected(self):
        windows = copy.deepcopy(self.windows)
        windows[1]["display_id"] = 1
        self.assert_rejected("wrong display", windows)
        windows = copy.deepcopy(self.windows)
        windows[2]["is_system"] = False
        self.assert_rejected("captured system window", windows)
        windows = copy.deepcopy(self.windows)
        windows[1]["trees"]["arkui"]["coordinate_evidence"]["scene_sources"].append(self.source)
        self.assert_rejected("duplicate system process", windows)

    def test_matching_component_without_matching_accessibility_identity_is_rejected(self):
        windows = copy.deepcopy(self.windows)
        windows[1]["ui_node_id"] = 999
        self.assert_rejected("window 106; found 0", windows)

    def test_duplicate_target_in_one_forest_is_rejected(self):
        duplicate = session_text(106, 1060)
        self.rewrite_raw(self.text.replace("|-> JsView childSize:0", duplicate + "|-> JsView childSize:0"))
        self.assert_rejected("WindowScene for window 106, found 2")

    def test_target_in_distinct_system_sources_is_rejected_as_ambiguous(self):
        second, _ = save_source(self.directory, 10, 101, ((10, 1000), (106, 1060)))
        self.windows.append(window_record(10, 1000, 101, second))
        self.attach_sources([self.source, second])
        self.assert_rejected("SceneBoard source for window 106; found 2")

    def test_scene_raw_and_geometry_must_both_exist_in_verified_file_index(self):
        names = ("access", "tree", "raw", "response", "geometry", "geometry-response", "render", "detached")
        records = {name: {"file": name, "bytes": 1, "sha256": "a" * 64} for name in names}
        geometry = dict(zip(("arkui_geometry", "response", "render_tree", "render_detached_tree"),
                            (records[name] for name in ("geometry", "geometry-response", "render", "detached"))))
        geometry["scene_sources"] = [self.source]
        tree = {**records["tree"], "node_count": 1, "raw": records["raw"], "response": records["response"],
                "coordinate_evidence": geometry, "session": {"is_system": True}}
        window = {"trees": {"arkui": tree, "accessibility": {"node_count": 1, "artifact": records["access"]}}}
        indexed = {record["file"]: record for record in (*records.values(), self.source["raw"],
                                                        self.source["arkui_geometry"])}
        verify_tree_references(window, indexed)
        for kind in ("raw", "arkui_geometry"):
            with self.subTest(kind=kind):
                missing = dict(indexed)
                del missing[self.source[kind]["file"]]
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "verified file index"):
                    verify_tree_references(window, missing)

    def geometry_hdc(self, windows):
        hdc, index = Mock(), {window["window_id"]: window for window in windows}
        hdc.run.side_effect = lambda _args, name: (
            "HAR_CAPTURE_RENDER_READY" if name.startswith("render_") else "HAR_CAPTURE_GEOMETRY_READY")

        def receive(_remote, destination, name):
            if name.startswith("receive_render_"):
                destination.write_text("render evidence", encoding="utf-8")
            else:
                wid = int(name.rsplit("_", 1)[1])
                tree = json.dumps({"appInfo": {"WindowID": 1, "BundleName": "com.ohos.sceneboard"},
                                   "$type": "root", "$ID": 0})
                destination.write_text(f"WinId: {wid}\nPid: {index[wid]['process_id']}\n" + tree, encoding="utf-8")

        hdc.receive.side_effect = receive
        return hdc

    def test_geometry_collection_reuses_sources_without_extra_device_commands(self):
        (self.directory / "arkui").mkdir()
        hdc = self.geometry_hdc(self.windows)
        result = collect_geometry_evidence(hdc, self.directory, {"layout": "/data/local/tmp/test"}, self.windows)
        self.assertEqual([call.args[1] for call in hdc.run.call_args_list],
                         ["geometry_23", "render_geometry", "render_detached_geometry"])
        self.assertEqual(result[106]["source_window_id"], 106)
        self.assertEqual(result[9]["source_window_id"], 23)
        sources = result[106]["scene_sources"]
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_window_id"], 9)
        self.assertEqual(sources[0]["raw"], self.source["raw"])
        self.assertEqual(sources[0]["arkui_geometry"], result[23]["arkui_geometry"])
        for window in self.windows:
            window["trees"]["arkui"]["coordinate_evidence"] = result[window["window_id"]]
        self.assertEqual(set(load_scene_evidence(self.directory, self.windows)), {23, 9, 106})

    def test_capture_without_system_source_records_empty_list_and_export_still_rejects(self):
        (self.directory / "arkui").mkdir()
        windows = [copy.deepcopy(self.windows[1])]
        hdc = self.geometry_hdc(windows)
        result = collect_geometry_evidence(hdc, self.directory, {"layout": "/data/local/tmp/test"}, windows)
        self.assertEqual(result[106]["scene_sources"], [])
        windows[0]["trees"]["arkui"]["coordinate_evidence"] = result[106]
        self.assert_rejected("Missing SceneBoard coordinate sources", windows)


if __name__ == "__main__":
    unittest.main()
