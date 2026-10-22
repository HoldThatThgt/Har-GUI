"""@brief 验证原生 ArkUI 坐标证据、父链、变换及不完整信息的拒收。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from har_capture.coordinates import normalize_arkui, render_index
from har_capture.geometry_capture import collect_geometry_evidence, parse_geometry_response
from har_capture.hdc import CaptureError


def component(node_id, tag, local, children=None, properties=None):
    attrs = {"ID": str(node_id), "type": tag, "FrameRect": local, "PaintRect without transform": local}
    attrs.update(properties or {})
    return {"attributes": attrs, "source_attributes": dict(attrs), "children": children or [],
            "source_line": node_id + 1, "raw": f"CanvasNode[{node_id + 100}] child[] lazyLoad[0] nodeState[ACTIVE]"}


def fixture():
    text = component(2, "Text", "RectT (5.00, -10.00) - [20.00 x 30.00]")
    column = component(1, "Column", "RectT (10.00, 20.00) - [100.00 x 80.00]", [text])
    root = component(0, "root", "RectT (0.00, 0.00) - [200.00 x 150.00]", [column])
    native = {"$ID": 0, "$type": "root", "$rect": "[0.00, 0.00],[200.00,150.00]", "$children": [
        {"$ID": 1, "$type": "Column", "$rect": "[10.00, 20.00],[110.00,100.00]", "$children": [
            {"$ID": 2, "$type": "Text", "$rect": "[15.00, 10.00],[35.00,40.00]"}]}]}
    render = (
        "| ROOT_NODE[100], Properties: Bounds[0 0 200 150] Frame[0 0 200 150], OtherModifiers:[]\n"
        "  | CANVAS_NODE[101], Properties: Bounds[10 20 100 80] Frame[10 20 100 80], ClipToBounds[True], "
        "OtherModifiers:[ClipToBounds:[true]]\n"
        "    | CANVAS_NODE[102], Properties: Bounds[5 -10 20 30] Frame[5 -10 20 30], OtherModifiers:[]\n"
    )
    window = {"window_id": 9, "bounding_box": {"x": 100, "y": 50, "width": 200, "height": 150},
              "transform": {"offset_x": 0, "offset_y": 0, "scale_x": 1, "scale_y": 1,
                            "pivot_x": 0.5, "pivot_y": 0.5}}
    return root, window, {"width": 3120, "height": 2080}, {
        "arkui_geometry": native, "render_tree": render, "render_detached_tree": "-- Render Node Not On Tree\n"}


class CoordinateTests(unittest.TestCase):
    def test_native_parent_coordinates_and_scroll_offset_are_not_added_twice(self):
        args = fixture()
        before = copy.deepcopy(args[0])
        result = normalize_arkui(*args)
        text = result["children"][0]["children"][0]
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(text["geometry"]["screen_bounds"], {"x": 115, "y": 60, "width": 20, "height": 30})
        self.assertEqual(text["geometry"]["rectangular_clip_bounds"],
                         {"x": 115, "y": 70, "width": 20, "height": 20})
        self.assertEqual(args[0], before)
        self.assertEqual(text["attributes"]["FrameRect"], before["children"][0]["children"][0]["attributes"]["FrameRect"])

    def test_nested_scale_and_pivot_are_checked_against_native_values(self):
        args = fixture()
        args[0]["children"][0]["attributes"]["scale(x,y)"] = "2.000000,0.500000"
        native = args[3]["arkui_geometry"]["$children"][0]
        native["$rect"] = "[-40.00, 40.00],[160.00,80.00]"
        native["$children"][0]["$rect"] = "[-30.00, 35.00],[10.00,50.00]"
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToBounds:[true]]", "OtherModifiers:[ClipToBounds:[true], Scale:[x:2.0 y:0.5]]")
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["children"][0]["children"][0]["geometry"]["screen_bounds"],
                         {"x": 70, "y": 85, "width": 40, "height": 15})

    def test_window_scale_translation_and_canvas_clip(self):
        args = fixture()
        args[1]["transform"].update(scale_x=1.5, scale_y=0.5, offset_x=10, offset_y=-5)
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["geometry"]["screen_bounds"],
                         {"x": 60, "y": 82.5, "width": 300, "height": 75})

    def test_system_root_global_origin_is_not_added_twice(self):
        args = fixture()
        args[0]["attributes"].update(type="WindowScene,0,1000", compid="session9")
        args[0]["attributes"]["PaintRect without transform"] = "RectT (100.00, 50.00) - [200.00 x 150.00]"
        native = args[3]["arkui_geometry"]
        native.update({"$type": "WindowScene", "compid": "session9", "$rect": "[100.00, 50.00],[300.00,200.00]"})
        native["$children"][0]["$rect"] = "[110.00, 70.00],[210.00,150.00]"
        native["$children"][0]["$children"][0]["$rect"] = "[115.00, 60.00],[135.00,90.00]"
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["children"][0]["children"][0]["geometry"]["screen_bounds"]["y"], 60)

    def test_overlay_omitted_by_native_traversal_requires_actual_render_parent_geometry(self):
        args = fixture()
        args[0]["children"][0]["children"][0]["attributes"].update(
            Description="OverlayNode", LastParentId="1")
        args[3]["arkui_geometry"]["$children"][0]["$children"] = []
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        source = result["children"][0]["children"][0]["geometry"]["source"]
        self.assertTrue(source["overlay_render_cross_checked"])
        self.assertFalse(source["native_rect_cross_checked"])
        args[3]["render_tree"] = args[3]["render_tree"].replace("Bounds[5 -10 20 30]", "Bounds[9 -10 20 30]")
        self.assert_failed(args, "overlay_render_projection_mismatch")

    def test_recycle_dummy_ids_are_parent_scoped_and_preserved(self):
        args = fixture()
        for node_id in (3, 4):
            raw = {"attributes": {"ID": str(node_id), "type": "IfElse"}, "raw": "", "children": [
                {"attributes": {"ID": "-1", "type": "__Recycle__"}, "raw": "", "children": []}]}
            native = {"$ID": node_id, "$type": "IfElse", "$children": [{"$ID": -1, "$type": "__Recycle__"}]}
            args[0]["children"].append(raw)
            args[3]["arkui_geometry"]["$children"].append(native)
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["coordinate_validation"]["non_layout_nodes"], 4)
        self.assertEqual(result["children"][-1]["children"][0]["attributes"]["ID"], "-1")

    def test_negative_unlaid_dimensions_follow_native_skew_corner_envelope(self):
        args = fixture()
        leaf = args[0]["children"][0]["children"][0]
        leaf["attributes"]["PaintRect without transform"] = "RectT (5.00, -10.00) - [-1.00 x -1.00]"
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[14.00,9.00],[15.00,10.00]"
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["children"][0]["children"][0]["geometry"]["screen_bounds"],
                         {"x": 114, "y": 59, "width": 1, "height": 1})

    def test_rounded_clip_preserves_corner_geometry_without_claiming_pixel_visibility(self):
        args = fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "ClipToBounds:[true]", "ClipToBounds:[true], CornerRadius:[x:4.0 y:5.0 z:6.0 w:7.0]")
        result = normalize_arkui(*args)
        clip = result["children"][0]["geometry"]["clip_chain"][-1]
        self.assertEqual(clip["kind"], "rounded_rectangle")
        self.assertEqual(clip["corner_radius_local"], [4, 5, 6, 7])
        self.assertNotIn("visible_bounds", result["children"][0]["geometry"])

    def assert_failed(self, args, code):
        with self.assertLogs(level="ERROR"):
            result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        self.assertTrue(any(code in issue["code"] for issue in result["coordinate_validation"]["issues"]))
        return result

    def test_native_geometry_is_required_instead_of_accessibility_bounds(self):
        args = fixture()
        args[0]["attributes"]["bounds"] = "[100,50][300,200]"
        del args[3]["arkui_geometry"]
        self.assert_failed(args, "arkui_geometry")

    def test_geometry_change_between_source_reads_is_rejected(self):
        args = fixture()
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[16.00, 10.00],[36.00,40.00]"
        result = self.assert_failed(args, "local_projection_mismatch")
        self.assertIsNone(result["children"][0]["children"][0]["geometry"]["screen_bounds"])

    def test_parent_identity_change_is_rejected(self):
        args = fixture()
        native = args[3]["arkui_geometry"]
        native["$children"].append(native["$children"][0]["$children"].pop())
        self.assert_failed(args, "parent_mismatch")

    def test_missing_active_render_node_is_rejected(self):
        args = fixture()
        args[3]["render_tree"] = "\n".join(args[3]["render_tree"].splitlines()[:-1])
        self.assert_failed(args, "active_render_node_missing")

    def test_detached_node_is_retained_with_native_bounds_and_no_applied_clip(self):
        args = fixture()
        lines = args[3]["render_tree"].splitlines()
        args[3]["render_tree"] = "\n".join(lines[:-1])
        args[3]["render_detached_tree"] += lines[-1].strip()
        result = normalize_arkui(*args)
        geometry = result["children"][0]["children"][0]["geometry"]
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(geometry["screen_bounds"], {"x": 115, "y": 60, "width": 20, "height": 30})
        self.assertEqual(geometry["render_state"], "not_rendered")
        self.assertFalse(geometry["rendered"])
        self.assertEqual(geometry["render_state_source"], "RenderService.allNodeNotOnTree")
        self.assertEqual(geometry["clip_chain"], [])
        self.assertIsNone(geometry["rectangular_clip_bounds"])
        self.assertEqual(geometry["clipping_status"], "not_applicable")

    def test_attachment_change_and_unrecognized_detached_evidence_are_rejected(self):
        args = fixture()
        args[3]["render_detached_tree"] += args[3]["render_tree"].splitlines()[-1].strip()
        self.assert_failed(args, "attachment_state_changed")
        args = fixture()
        args[3]["render_detached_tree"] = "Usage: unknown command"
        self.assert_failed(args, "missing_detached_render_tree_section")

    def test_explicit_lazy_render_node_is_preserved(self):
        args = fixture()
        args[3]["render_tree"] = "\n".join(args[3]["render_tree"].splitlines()[:-1])
        leaf = args[0]["children"][0]["children"][0]
        leaf["raw"] = "CanvasNode[102] child[] lazyLoad[1] nodeState[LAZY_LOAD]"
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertFalse(result["children"][0]["children"][0]["geometry"]["source"]["render_node_materialized"])

    def test_rotation_and_custom_clip_cannot_pass_as_axis_aligned(self):
        for modifier in ("Rotation:[13.0]", "Skew:[x:1.0 y:0.0]", "ClipBounds:[path]",
                         "CustomClipPath:[path]"):
            with self.subTest(modifier=modifier):
                args = fixture()
                args[3]["render_tree"] = args[3]["render_tree"].replace(
                    "OtherModifiers:[]", "OtherModifiers:[" + modifier + "]", 1)
                self.assert_failed(args, "unsupported_render")

    def test_native_content_clip_is_ltrb_and_clips_children(self):
        args = fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "ClipToBounds:[true]", "ClipToBounds:[true], CustomClipToFrame:[x:7.0 y:0.0 z:30.0 w:40.0]")
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        leaf = result["children"][0]["children"][0]["geometry"]
        self.assertEqual(leaf["rectangular_clip_bounds"], {"x": 117, "y": 70, "width": 18, "height": 20})
        self.assertEqual(leaf["clip_chain"][-1]["bounds"], {"x": 117, "y": 70, "width": 23, "height": 40})

    def test_native_root_size_must_match_window(self):
        args = fixture()
        args[1]["bounding_box"]["width"] = 201
        self.assert_failed(args, "root_window_size_mismatch")

    def test_native_missing_and_duplicate_ids_are_rejected(self):
        args = fixture()
        args[3]["arkui_geometry"]["$children"][0]["$children"].clear()
        self.assert_failed(args, "frame_missing")
        args = fixture()
        args[3]["arkui_geometry"]["$children"].append(copy.deepcopy(args[3]["arkui_geometry"]["$children"][0]))
        self.assert_failed(args, "duplicate_native_geometry_id")

    def test_new_frame_in_native_geometry_cannot_be_silently_dropped(self):
        args = fixture()
        args[3]["arkui_geometry"]["$children"].append(
            {"$ID": 9, "$type": "Button", "$rect": "[0.00, 0.00],[10.00,10.00]"})
        self.assert_failed(args, "frame_set_mismatch")

    def test_render_truncation_and_duplicate_nodes_are_rejected(self):
        for suffix in ("===== WARNING: exceed max depth for dumping render node tree =====", "| ROOT_NODE[100]"):
            args = fixture()
            args[3]["render_tree"] += suffix
            self.assert_failed(args, "render")

    def test_non_layout_node_keeps_children_without_inventing_bounds(self):
        args = fixture()
        raw = {"attributes": {"type": "JsView", "ID": "3"}, "raw": "", "children": args[0]["children"]}
        args[0]["children"] = [raw]
        native = {"$ID": 3, "$type": "JsView", "$children": args[3]["arkui_geometry"]["$children"]}
        args[3]["arkui_geometry"]["$children"] = [native]
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["children"][0]["geometry"]["status"], "not_applicable")
        self.assertNotIn("screen_bounds", result["children"][0]["geometry"])

    def test_allinfo_response_requires_exactly_one_native_tree(self):
        self.assertEqual(parse_geometry_response('WinId: 9\n{"$type":"root","$ID":0}\n')["$ID"], 0)
        for text in ('', '{"$type":broken}', '{"$type":"root","$ID":0}\n{"$type":"root","$ID":1}'):
            with self.subTest(text=text), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                parse_geometry_response(text)

    def test_system_process_geometry_is_requested_once_with_source_identity_preserved(self):
        hdc = Mock()
        hdc.run.side_effect = lambda _args, name: (
            "HAR_CAPTURE_RENDER_READY" if name.startswith("render_") else "HAR_CAPTURE_GEOMETRY_READY")
        windows = [{"window_id": wid, "process_id": 100, "display_id": 0,
                    "trees": {"arkui": {"session": {"is_system": True}}}} for wid in (5, 7)]

        def receive(_remote, destination, name):
            if name.startswith("receive_render_"):
                destination.write_text("render evidence", encoding="utf-8")
            else:
                tree = json.dumps({"$type": "root", "$ID": 0})
                destination.write_text("WinId: 5\nPid: 100\n" + tree, encoding="utf-8")

        hdc.receive.side_effect = receive
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "arkui").mkdir()
            result = collect_geometry_evidence(hdc, stage, {"layout": "/data/local/tmp/probe"}, windows)
            self.assertEqual(result[5]["source_window_id"], 5)
            self.assertEqual(result[7]["source_window_id"], 5)
            self.assertEqual(result[5]["arkui_geometry"], result[7]["arkui_geometry"])
            self.assertEqual([call.args[1] for call in hdc.run.call_args_list],
                             ["geometry_5", "render_geometry", "render_detached_geometry"])
            self.assertEqual(result[5]["render_detached_tree"], result[7]["render_detached_tree"])


if __name__ == "__main__":
    unittest.main()
