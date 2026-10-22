"""@brief 验证运行时精确窗口外框、来源共享、严格归属和临时证据清理。"""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from har_capture.coordinates import detached_render_index, render_index
from har_capture.hdc import CaptureError, Hdc
from har_capture.delivery_operations import verify_screen_geometry
from har_capture.scene_evidence import load_source, select_window_scene
from har_capture.tree_capture import artifact_info
from har_capture.window_geometry import attach_captured_screen_geometry, read_window_geometry, window_screen_geometry
from tests.test_window_mapping import ancestor_mapping_fixture, component, mapping_fixture, render_line


def geometry_fixture():
    """@brief 为既有精确映射样例补充已核验来源摘要。"""
    _root, window, _canvas, evidence = mapping_fixture()
    scene = evidence["scene"]
    scene["source"].update(raw={"sha256": "a" * 64}, arkui_geometry={"sha256": "b" * 64})
    return window, scene, render_index(evidence["render_tree"]), detached_render_index(evidence["render_detached_tree"])


def source_fixture():
    """@brief 构造同一系统进程含两系统窗口和一个缩放应用窗口的完整来源。"""
    _root, target, _canvas, evidence = mapping_fixture()
    scene, windows = evidence["scene"], []
    children, native_children, rows = [], [], []
    for window_id in (5, 7):
        node = component(window_id, "WindowScene", "RectT (0.00, 0.00) - [3120.00 x 2080.00]", {
            "AccessibilityId": str(window_id), "compid": f"session{window_id}"})
        node["raw"] = f"SurfaceNode[{500 + window_id}] child[] lazyLoad[0] nodeState[ACTIVE]"
        children.append(node)
        native_children.append({"$ID": window_id, "$type": "WindowScene", "compid": f"session{window_id}",
                                "$rect": "[0.00,0.00],[3120.00,2080.00]"})
        rows.append(render_line(2, "SURFACE_NODE", 500 + window_id, "0 0 3120 2080",
                                f"nodeName: [session{window_id}], OtherModifiers:[Pivot:[x:0 y:0]]"))
        windows.append({**copy.deepcopy(target), "window_id": window_id, "process_id": 4575,
                        "ui_node_id": window_id, "is_system": True,
                        "bounding_box": {"x": 0, "y": 0, "width": 3120, "height": 2080},
                        "transform": {"scale_x": 1, "scale_y": 1}})
    screen = copy.deepcopy(scene["path"][0])
    screen["children"] = children + [scene["path"][-1]]
    native = copy.deepcopy(scene["native"])
    native["$children"] = native_children + native["$children"]
    render = evidence["render_tree"].splitlines(keepends=True)
    render = "".join(render[:2] + rows + render[2:])
    return windows + [target], screen, native, render


def raw_scene(node, depth=0):
    """@brief 序列化匿名组件自身属性和原始渲染关联。"""
    padding = "  " * depth
    result = f"{padding}|-> {node['attributes']['type']} childSize:{len(node['children'])}\n"
    result += "".join(f"{padding}  | {key}: {value}\n" for key, value in node["attributes"].items()
                      if key != "type")
    result += f"{padding}  | {node['raw']}\n"
    return result + "".join(raw_scene(child, depth + 1) for child in node["children"])


def device_fixture(failure=None):
    """@brief 用原始官方格式替代设备，记录收到的临时路径和命令。"""
    windows, screen, native, render = source_fixture()
    hdc, destinations = Mock(spec=Hdc), []
    raw = "WinId: 5\nPid: 4575\n" + raw_scene(screen)
    native_response = "WinId: 5\nPid: 4575\n" + json.dumps({"$type": native["$type"], **native})
    payloads = {"receive_arkui_response_5": raw, "receive_geometry_5": native_response,
                "receive_render_geometry": render, "receive_render_detached_geometry": "-- Render Node Not On Tree\n"}

    def run(_arguments, name):
        if name == failure:
            raise CaptureError("Injected geometry command failure")
        if name == "cleanup":
            return "HAR_CAPTURE_CLEANUP_OK"
        if name.startswith("dump_arkui"):
            return "HAR_CAPTURE_DUMP_READY"
        return "HAR_CAPTURE_RENDER_READY" if name.startswith("render_") else "HAR_CAPTURE_GEOMETRY_READY"

    def receive(_remote, destination, name):
        destinations.append(destination)
        if name == failure:
            raise CaptureError("Injected geometry transfer failure")
        destination.write_text(payloads[name], encoding="utf-8")

    hdc.run.side_effect, hdc.receive.side_effect = run, receive
    return hdc, windows, destinations


def captured_fixture(directory):
    """@brief 保存同一批完整匿名来源和索引，供纯离线附加验证。"""
    windows, screen, native, render = source_fixture()
    payloads = {"scene.txt": "WinId: 5\nPid: 4575\n" + raw_scene(screen), "native.json": json.dumps(native),
                "render.txt": render, "detached.txt": "-- Render Node Not On Tree\n"}
    records = {}
    for name, text in payloads.items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        records[name] = artifact_info(directory, path)
    source = {"source_window_id": 5, "process_id": 4575, "display_id": 0,
              "raw": records["scene.txt"], "arkui_geometry": records["native.json"]}
    for window in windows:
        window["trees"] = {"arkui": {"response": source["raw"], "coordinate_evidence": {
            "arkui_geometry": source["arkui_geometry"], "scene_sources": [source],
            "render_tree": records["render.txt"], "render_detached_tree": records["detached.txt"]}}}
    return {"windows": windows, "files": records}


class WindowScreenGeometryTests(unittest.TestCase):
    """@brief 验证外框必须由实际浮点场景和正确渲染父链证明。"""

    def assert_rejected(self, arguments, expected):
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, expected):
            window_screen_geometry(*arguments)

    def test_exact_outer_bounds_keep_wms_logic_bounds_and_finite_source_identity(self):
        arguments = geometry_fixture()
        before = copy.deepcopy(arguments)
        geometry = window_screen_geometry(*arguments)
        self.assertEqual(arguments, before)
        bounds = geometry["screen_bounds"]
        self.assertAlmostEqual(bounds["x"], 203.3)
        self.assertAlmostEqual(bounds["width"], 1795.444736)
        self.assertNotEqual(bounds["width"], arguments[0]["bounding_box"]["width"])
        self.assertNotAlmostEqual(bounds["width"], 2048 * arguments[0]["transform"]["scale_x"], places=2)
        self.assertEqual(geometry["screen_bounds_source"], {
            "method": "SceneBoard.ArkUI.PaintRectAncestorComposition", "session_node_id": "700",
            "session_render_node_id": "19649475381948", "display_render_node_id": "6962141986818",
            "screen_render_node_id": "90", "raw_sha256": "a" * 64, "native_sha256": "b" * 64})

    def test_outer_bounds_validate_target_and_ancestors_without_traversing_unrelated_descendants(self):
        arguments = geometry_fixture()
        expected = window_screen_geometry(*arguments)
        descendant = component(701, "Text", "invalid descendant frame")
        descendant["attributes"]["ID"] = ["104", "34", "31", "32", "21", "7"]
        descendant["source_attributes"]["ID"] = list(descendant["attributes"]["ID"])
        arguments[1]["path"][-1]["children"].append(descendant)
        before = copy.deepcopy(arguments)
        self.assertEqual(window_screen_geometry(*arguments), expected)
        self.assertEqual(arguments, before)
        arguments[1]["path"][-1]["attributes"]["ID"] = ["700", "701"]
        self.assert_rejected(arguments, "scene_native_identity_or_parent_mismatch")

    def test_exact_identity_native_projection_and_wms_outer_agreement_are_required(self):
        for scope, expected in (("identity", "identity"), ("native", "native_projection"),
                                ("display", "display"), ("wms", "outer_origin"), ("digest", "source_digest")):
            with self.subTest(scope=scope):
                arguments = geometry_fixture()
                if scope == "identity":
                    arguments[0]["ui_node_id"] += 1
                elif scope == "native":
                    arguments[1]["native"]["$children"][0]["$rect"] = "[204.30,0.00],[1999.74,1955.00]"
                elif scope == "display":
                    arguments[1]["source"]["display_id"] = 1
                elif scope == "wms":
                    arguments[0]["bounding_box"]["x"] += 1
                else:
                    arguments[1]["source"]["raw"]["sha256"] = "invalid"
                self.assert_rejected(arguments, expected)

    def test_missing_scene_and_missing_surface_never_use_wms_geometry(self):
        for scene in (None, {}, {"path": []}):
            with self.subTest(scene=scene):
                window, _scene, render, detached = geometry_fixture()
                self.assert_rejected((window, scene, render, detached), "missing_scene_coordinate_evidence")
        arguments = geometry_fixture()
        del arguments[2]["19649475381948"]
        self.assert_rejected(arguments, "invalid_render_parent_chain")

    def test_screen_canvas_and_actual_surface_name_are_required(self):
        for field, value, expected in (("canvas", "0 0 3119 2080", "canvas"),
                                       ("canvas", "1 0 3120 2080", "canvas"),
                                       ("canvas", "0 0 3120.5 2080", "canvas"),
                                       ("name", "session107", "session_identity")):
            with self.subTest(field=field, value=value):
                window, scene, render, detached = geometry_fixture()
                if field == "canvas":
                    render["90"]["line"] = render["90"]["line"].replace("0 0 3120 2080", value)
                else:
                    key = "19649475381948"
                    render[key]["line"] = render[key]["line"].replace("session106", value)
                self.assert_rejected((window, scene, render, detached), expected)

    def test_wrong_render_parent_and_attachment_conflict_remain_rejected(self):
        arguments = geometry_fixture()
        arguments[2]["19649475381948"]["parent"] = "90"
        self.assert_rejected(arguments, "display")
        arguments = geometry_fixture()
        arguments[3]["19649475381948"] = arguments[2]["19649475381948"]
        self.assert_rejected(arguments, "attachment_state")
        _root, window, _canvas, evidence = ancestor_mapping_fixture(reversed_render_order=True)
        evidence["scene"]["source"].update(raw={"sha256": "a" * 64}, arkui_geometry={"sha256": "b" * 64})
        self.assert_rejected((window, evidence["scene"], render_index(evidence["render_tree"]), {}),
                             "frame_ancestor_missing:11")

    def test_shared_generic_render_root_is_structural_but_duplicate_session_surface_is_rejected(self):
        arguments = geometry_fixture()
        expected = window_screen_geometry(*arguments)
        root = {"id": "0", "kind": "RS_NODE", "parent": None, "children_count": 1,
                "line": render_line(0, "RS_NODE", 0, "0 0 3120 2080", "OtherModifiers:[]").strip()}
        arguments[2]["0"] = root
        arguments[2]["90"]["parent"] = "0"
        arguments[3]["0"] = copy.deepcopy(root)
        self.assertEqual(window_screen_geometry(*arguments), expected)
        arguments[3]["19649475381948"] = copy.deepcopy(arguments[2]["19649475381948"])
        self.assert_rejected(arguments, "scene_render_attachment_state_changed")


class WindowGeometryCollectionTests(unittest.TestCase):
    """@brief 验证批次共用来源及失败时设备和主机临时文件均清理。"""

    def test_shared_source_is_collected_once_and_returns_only_persistent_geometry_fields(self):
        hdc, windows, destinations = device_fixture()
        before = copy.deepcopy(windows)
        result = read_window_geometry(hdc, windows)
        self.assertEqual(windows, before)
        self.assertEqual([window["window_id"] for window in result], [5, 7, 106])
        names = [call.args[1] for call in hdc.run.call_args_list]
        self.assertEqual(names, ["dump_arkui_5", "geometry_5", "render_geometry",
                                 "render_detached_geometry", "cleanup"])
        self.assertTrue(all(not path.exists() for path in destinations))
        self.assertEqual(result[-1]["bounding_box"], windows[-1]["bounding_box"])
        self.assertAlmostEqual(result[-1]["screen_bounds"]["width"], 1795.444736)
        self.assertNotIn("file", json.dumps(result[-1]["screen_bounds_source"]))
        cleanup_paths = hdc.run.call_args_list[-1].args[0][3:-3]
        self.assertEqual(len(cleanup_paths), 5)
        self.assertTrue(all(path.startswith("/data/local/tmp/har-window-geometry-") for path in cleanup_paths))

    def test_failed_collection_still_cleans_exact_remote_and_local_files(self):
        for failure in ("dump_arkui_5", "receive_arkui_response_5", "geometry_5", "receive_geometry_5",
                        "render_geometry", "receive_render_geometry", "render_detached_geometry"):
            with self.subTest(failure=failure):
                hdc, windows, destinations = device_fixture(failure)
                with self.assertRaises(CaptureError):
                    read_window_geometry(hdc, windows)
                self.assertEqual(hdc.run.call_args_list[-1].args[1], "cleanup")
                self.assertTrue(all(not path.exists() for path in destinations))

    def test_cleanup_failure_prevents_success_and_no_source_does_not_guess_a_window(self):
        hdc, windows, destinations = device_fixture("cleanup")
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "cleanup failure"):
            read_window_geometry(hdc, windows)
        self.assertTrue(all(not path.exists() for path in destinations))
        hdc, windows, _destinations = device_fixture()
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "no verified SceneBoard"):
            read_window_geometry(hdc, windows[-1:])
        self.assertEqual([call.args[1] for call in hdc.run.call_args_list], ["cleanup"])

    def test_missing_target_and_duplicate_windows_do_not_return_partial_results(self):
        hdc, windows, destinations = device_fixture()
        windows[-1]["ui_node_id"] += 1
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "Expected one SceneBoard source"):
            read_window_geometry(hdc, windows)
        self.assertTrue(all(not path.exists() for path in destinations))
        hdc, windows, _destinations = device_fixture()
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "distinct window"):
            read_window_geometry(hdc, windows + windows[-1:])
        hdc.run.assert_not_called()

    def test_wrong_display_cannot_reuse_another_display_scene(self):
        hdc, windows, destinations = device_fixture()
        windows[-1]["display_id"] = 1
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "Expected one SceneBoard source"):
            read_window_geometry(hdc, windows)
        self.assertTrue(all(not path.exists() for path in destinations))

    def test_cleanup_failure_does_not_hide_the_primary_transfer_failure(self):
        hdc, windows, destinations = device_fixture("receive_geometry_5")
        original = hdc.run.side_effect

        def failed_cleanup(arguments, name):
            if name == "cleanup":
                raise CaptureError("Injected cleanup failure")
            return original(arguments, name)

        hdc.run.side_effect = failed_cleanup
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "transfer failure"):
            read_window_geometry(hdc, windows)
        self.assertTrue(all(not path.exists() for path in destinations))


class CapturedWindowGeometryTests(unittest.TestCase):
    """@brief 验证离线附加使用原始索引并保持所有文件及 WMS 字段。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="har-window-geometry-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.metadata = captured_fixture(self.directory)

    def test_attachment_preserves_original_files_and_all_wms_fields(self):
        before = copy.deepcopy(self.metadata)
        files = {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                 for path in self.directory.iterdir()}
        attach_captured_screen_geometry(self.directory, self.metadata)
        self.assertEqual(self.metadata["files"], before["files"])
        self.assertEqual(files, {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                                 for path in self.directory.iterdir()})
        for window, original in zip(self.metadata["windows"], before["windows"]):
            self.assertEqual({key: window[key] for key in original}, original)
            self.assertEqual(set(window) - set(original), {"screen_bounds", "screen_bounds_source"})

    def test_preparation_recomputes_exact_bounds_and_rejects_missing_or_tampered_fields(self):
        attach_captured_screen_geometry(self.directory, self.metadata)
        before = copy.deepcopy(self.metadata)
        verify_screen_geometry(self.directory, self.metadata)
        self.assertEqual(self.metadata, before)
        for field in ("screen_bounds", "screen_bounds_source"):
            for mutation in ("missing", "changed"):
                with self.subTest(field=field, mutation=mutation):
                    metadata = copy.deepcopy(before)
                    window = metadata["windows"][-1]
                    if mutation == "missing":
                        del window[field]
                    elif field == "screen_bounds":
                        window[field]["x"] += 1
                    else:
                        window[field]["render_tree_sha256"] = "f" * 64
                    original = copy.deepcopy(metadata)
                    with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "screen geometry differs"):
                        verify_screen_geometry(self.directory, metadata)
                    self.assertEqual(metadata, original)

    def test_wrong_source_and_render_digest_or_unindexed_reference_are_rejected(self):
        for mutation in ("header", "digest", "index"):
            with self.subTest(mutation=mutation):
                metadata = copy.deepcopy(self.metadata)
                if mutation == "header":
                    metadata["windows"][0]["trees"]["arkui"]["coordinate_evidence"]["scene_sources"][0][
                        "process_id"] = 4576
                elif mutation == "digest":
                    (self.directory / "render.txt").write_text("invalid render tree", encoding="utf-8")
                else:
                    del metadata["files"]["detached.txt"]
                with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                    attach_captured_screen_geometry(self.directory, metadata)
                self.assertTrue(all("screen_bounds" not in window for window in metadata["windows"]))

    def test_one_invalid_window_prevents_any_geometry_mutation(self):
        self.metadata["windows"][-1]["bounding_box"]["width"] += 1
        before = copy.deepcopy(self.metadata)
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            attach_captured_screen_geometry(self.directory, self.metadata)
        self.assertEqual(self.metadata, before)

    def test_duplicate_scene_candidates_are_rejected_instead_of_selecting_a_source(self):
        window = self.metadata["windows"][-1]
        source = window["trees"]["arkui"]["coordinate_evidence"]["scene_sources"][0]
        forest = load_source(self.directory, source, {})
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "found 2"):
            select_window_scene([forest, forest], window)


if __name__ == "__main__":
    unittest.main()
