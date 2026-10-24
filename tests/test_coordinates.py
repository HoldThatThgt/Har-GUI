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
from har_capture.metadata import file_info


def component(node_id, tag, local, children=None, properties=None):
    attrs = {"ID": str(node_id), "type": tag, "FrameRect": local, "PaintRect without transform": local}
    attrs.update(properties or {})
    return {"attributes": attrs, "source_attributes": dict(attrs), "children": children or [],
            "source_line": node_id + 1, "raw": f"CanvasNode[{node_id + 100}] child[] lazyLoad[0] nodeState[ACTIVE]"}


def scene_fixture():
    """@brief 构造由屏幕、系统会话原文及原生投影独立证明的窗口映射。"""
    screen = component(900, "Screen", "RectT (0.00, 0.00) - [3120.00 x 2080.00]")
    screen["raw"] = "DisplayNode[900] child[] lazyLoad[0] nodeState[ACTIVE]"
    session = component(901, "WindowScene", "RectT (100.00, 50.00) - [200.00 x 150.00]",
                        properties={"AccessibilityId": "90", "compid": "session9"})
    session["raw"] = "SurfaceNode[901] child[] lazyLoad[0] nodeState[ACTIVE]"
    native = {"$ID": 900, "$type": "Screen", "$rect": "[0.00,0.00],[3120.00,2080.00]", "$children": [
        {"$ID": 901, "$type": "WindowScene", "compid": "session9", "$rect": "[100.00,50.00],[300.00,200.00]"}]}
    render = (
        "| SCREEN_NODE[899], Properties: Bounds[0 0 3120 2080] Frame[0 0 3120 2080], "
        "screenId[0], OtherModifiers:[]\n"
        "  | LOGICAL_DISPLAY_NODE[900], Properties: Bounds[0 0 3120 2080] Frame[0 0 3120 2080], "
        "OtherModifiers:[]\n"
        "    | SURFACE_NODE[901], Properties: Bounds[100 50 200 150] Frame[100 50 200 150], "
        "nodeName: [session9], ClipToFrame[True], OtherModifiers:[ClipToFrame:[true]]\n")
    return {"path": [screen, session], "native": native,
            "source": {"process_id": 1000, "display_id": 0, "source_window_id": 5}}, render


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
    scene, outer_render = scene_fixture()
    render = outer_render + "".join("      " + line for line in render.splitlines(keepends=True))
    window = {"window_id": 9, "process_id": 1001, "display_id": 0, "ui_node_id": 90, "is_system": False,
              "bounding_box": {"x": 100, "y": 50, "width": 200, "height": 150},
              "transform": {"offset_x": 0, "offset_y": 0, "scale_x": 1, "scale_y": 1,
                            "pivot_x": 0.5, "pivot_y": 0.5}}
    return root, window, {"width": 3120, "height": 2080}, {
        "arkui_geometry": native, "render_tree": render, "render_detached_tree": "-- Render Node Not On Tree\n",
        "scene": scene}


def overlay_syntax_fixture():
    args = fixture()
    overlay = args[0]["children"][0]["children"][0]
    overlay["attributes"].update(Description="OverlayNode", LastParentId="1")
    syntax = {"attributes": {"ID": "3", "type": "IfElse", "Description": "Children"},
              "raw": "", "children": [], "source_line": 4}
    overlay["children"] = [syntax]
    args[3]["arkui_geometry"]["$children"][0]["$children"] = []
    return args


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
        original_frame = before["children"][0]["children"][0]["attributes"]["FrameRect"]
        self.assertEqual(text["attributes"]["FrameRect"], original_frame)

    def test_nested_scale_and_pivot_are_checked_against_native_values(self):
        args = fixture()
        args[0]["children"][0]["attributes"]["scale(x,y)"] = "2.000000,0.500000"
        native = args[3]["arkui_geometry"]["$children"][0]
        native["$rect"] = "[-40.00, 40.00],[160.00,80.00]"
        native["$children"][0]["$rect"] = "[-30.00, 35.00],[10.00,50.00]"
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToBounds:[true]]",
            "ScaleX[2.0] ScaleY[0.5], OtherModifiers:[ClipToBounds:[true], Scale:[x:2.0 y:0.5]]")
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["children"][0]["children"][0]["geometry"]["screen_bounds"],
                         {"x": 70, "y": 85, "width": 40, "height": 15})

    def test_scene_scale_translation_and_wms_outer_envelope_cross_check(self):
        args = fixture()
        session = args[3]["scene"]["path"][-1]
        session["attributes"].update({"scale(x,y)": "1.5,0.5", "translate(x,y)": "10,-5"})
        args[3]["scene"]["native"]["$children"][0]["$rect"] = "[60.00,82.50],[360.00,157.50]"
        args[1]["bounding_box"].update(x=60, y=82)
        args[1]["transform"].update(scale_x=1.5, scale_y=76 / 150)
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToFrame:[true]]",
            "ScaleX[1.5] ScaleY[0.5] TranslateX[10] TranslateY[-5], "
            "OtherModifiers:[ClipToFrame:[true], Scale:[x:1.5 y:0.5], Translate:[x:10 y:-5]]")
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["geometry"]["screen_bounds"],
                         {"x": 60, "y": 82.5, "width": 300, "height": 75})

    def test_reflected_leaf_pivot_translation_and_content_clip_use_signed_axes(self):
        args = fixture()
        leaf = args[0]["children"][0]["children"][0]
        leaf["attributes"].update({"scale(x,y)": "-2,-0.5", "Center": "x:0.25 y:0.75",
                                    "translate(x,y)": "3,-2"})
        native = args[3]["arkui_geometry"]["$children"][0]["$children"][0]
        native["$rect"] = "[-7,26.75],[33,41.75]"
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "Frame[5 -10 20 30], OtherModifiers:[]",
            "Frame[5 -10 20 30], ScaleX[-2] ScaleY[-0.5] Pivot[0.25,0.75] "
            "TranslateX[3] TranslateY[-2] CornerRadius[1 2 3 4], "
            "ClipToBounds[True], OtherModifiers:[Scale:[x:-2 y:-0.5], "
            "Pivot:[x:0.25 y:0.75], Translate:[x:3 y:-2], CornerRadius:[x:1 y:2 z:3 w:4], "
            "CustomClipToFrame:[x:2 y:4 z:8 w:18]]")
        result = normalize_arkui(*args)
        geometry = result["children"][0]["children"][0]["geometry"]
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(geometry["screen_bounds"], {"x": 93, "y": 76.75, "width": 40, "height": 15})
        clip = next(item for item in geometry["clip_chain"] if "local_bounds_ltrb" in item)
        self.assertEqual(clip["bounds"], {"x": 117, "y": 82.75, "width": 12, "height": 7})
        rounded = next(item for item in geometry["clip_chain"] if item["kind"] == "rounded_rectangle")
        self.assertEqual(rounded["corner_radius_local"], [1, 2, 3, 4])
        self.assertEqual(rounded["local_to_screen"][:4], [-2, 0, 0, -0.5])
        self.assertEqual(geometry["rectangular_clip_bounds"], clip["bounds"])

    def test_reflection_does_not_bypass_native_projection_or_render_direction_checks(self):
        args = fixture()
        args[0]["children"][0]["children"][0]["attributes"]["scale(x,y)"] = "-1,1"
        self.assert_failed(args, "scene_render_vector_missing:Scale")
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "Frame[5 -10 20 30], OtherModifiers:[]",
            "Frame[5 -10 20 30], ScaleX[-1] ScaleY[1], OtherModifiers:[Scale:[x:-1 y:1]]")
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[16,10],[36,40]"
        self.assert_failed(args, "native_geometry_local_projection_mismatch")

    def test_zero_scale_requires_matching_native_geometry(self):
        for scale in ("0,1", "1,-0", "-1,0"):
            args = fixture()
            args[0]["children"][0]["children"][0]["attributes"]["scale(x,y)"] = scale
            self.assert_failed(args, "native_geometry_local_projection_mismatch")

    def test_native_ancestor_envelope_conflicting_with_reflection_is_rejected(self):
        args = fixture()
        args[0]["children"][0]["attributes"]["scale(x,y)"] = "-1,1"
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToBounds:[true]]",
            "ScaleX[-1] ScaleY[1], OtherModifiers:[ClipToBounds:[true], Scale:[x:-1 y:1]]")
        # 官方祖先包围框偏移计算保留负尺寸，ToBounds 不对端点排序，必须拒收。
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[5,10],[-15,40]"
        self.assert_failed(args, "negative_native_geometry_size")

    def test_overlay_below_reflected_parent_is_checked_against_render_transform(self):
        args = fixture()
        args[0]["children"][0]["attributes"]["scale(x,y)"] = "-1,1"
        leaf = args[0]["children"][0]["children"][0]
        leaf["attributes"].update({"Description": "OverlayNode", "LastParentId": "1", "scale(x,y)": "-1,1"})
        args[3]["arkui_geometry"]["$children"][0]["$children"] = []
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToBounds:[true]]",
            "ScaleX[-1] ScaleY[1], OtherModifiers:[ClipToBounds:[true], Scale:[x:-1 y:1]]")
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "Frame[5 -10 20 30], OtherModifiers:[]",
            "Frame[5 -10 20 30], ScaleX[-1] ScaleY[1], OtherModifiers:[Scale:[x:-1 y:1]]")
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["children"][0]["children"][0]["geometry"]["screen_bounds"],
                         {"x": 185, "y": 60, "width": 20, "height": 30})
        args[3]["render_tree"] = args[3]["render_tree"].replace("Bounds[5 -10 20 30]", "Bounds[6 -10 20 30]")
        self.assert_failed(args, "overlay_render_projection_mismatch")

    def test_inactive_geometry_only_frames_still_require_raw_render_evidence(self):
        args = fixture()
        args[3]["arkui_geometry"]["$children"].append(
            {"$ID": 9, "$type": "ListItem", "$rect": "[0,200],[100,300]", "active": "false"})
        result = self.assert_failed(args, "frame_set_mismatch")
        self.assertEqual(len(result["children"]), 1)
        self.assertEqual(result["coordinate_validation"]["issues"][0]["code"],
                         "native_geometry_frame_set_mismatch:native_only=9;raw_only=")

    def test_system_root_global_origin_is_not_added_twice(self):
        args = fixture()
        args[1].update(is_system=True, process_id=1000)
        args[0]["attributes"].update(type="WindowScene,0,90", compid="session9", AccessibilityId="90")
        for key in ("FrameRect", "PaintRect without transform"):
            args[0]["attributes"][key] = "RectT (100.00, 50.00) - [200.00 x 150.00]"
        args[0]["raw"] = "SurfaceNode[901] child[] lazyLoad[0] nodeState[ACTIVE]"
        native = args[3]["arkui_geometry"]
        native.update({"$type": "WindowScene", "compid": "session9", "$rect": "[100.00, 50.00],[300.00,200.00]"})
        native["$children"][0]["$rect"] = "[110.00, 70.00],[210.00,150.00]"
        native["$children"][0]["$children"][0]["$rect"] = "[115.00, 60.00],[135.00,90.00]"
        args[3]["scene"]["path"][-1] = args[0]
        args[3]["scene"]["native"]["$children"] = [native]
        args[3]["arkui_geometry"] = args[3]["scene"]["native"]
        lines = args[3]["render_tree"].splitlines(keepends=True)
        args[3]["render_tree"] = "".join(lines[:3]) + "".join(line[2:] for line in lines[4:])
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

    def test_overlay_syntax_node_without_frame_parent_field_is_preserved(self):
        args = overlay_syntax_fixture()
        before = copy.deepcopy(args[0])
        result = normalize_arkui(*args)
        syntax = result["children"][0]["children"][0]["children"][0]
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(result["coordinate_validation"]["non_layout_nodes"], 1)
        self.assertEqual(syntax["geometry"], {"status": "not_applicable", "reason": "non_layout_arkui_node"})
        self.assertNotIn("LastParentId", syntax["attributes"])
        self.assertNotIn("screen_bounds", syntax["geometry"])
        self.assertEqual(args[0], before)

    def test_overlay_frame_below_syntax_retains_render_projection_checks(self):
        args = overlay_syntax_fixture()
        syntax = args[0]["children"][0]["children"][0]["children"][0]
        syntax["children"] = [component(4, "Button", "RectT (2.00, 3.00) - [8.00 x 9.00]",
                                          properties={"Description": "Children", "LastParentId": "3"})]
        args[3]["render_tree"] += (
            "            | CANVAS_NODE[104], Properties: Bounds[2 3 8 9] Frame[2 3 8 9], OtherModifiers:[]\n")
        result = normalize_arkui(*args)
        leaf = result["children"][0]["children"][0]["children"][0]["children"][0]
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        self.assertEqual(leaf["geometry"]["screen_bounds"], {"x": 117, "y": 63, "width": 8, "height": 9})
        self.assertTrue(leaf["geometry"]["source"]["overlay_render_cross_checked"])
        args[3]["render_tree"] = args[3]["render_tree"].replace("Bounds[2 3 8 9]", "Bounds[4 3 8 9]")
        self.assert_failed(args, "overlay_render_projection_mismatch")

    def test_overlay_syntax_requires_verified_parent_edge_and_known_node_type(self):
        for description in (None, "BrokenChildren", "DisappearingChildren"):
            with self.subTest(description=description):
                args = overlay_syntax_fixture()
                syntax = args[0]["children"][0]["children"][0]["children"][0]
                syntax["attributes"]["Description"] = description
                self.assert_failed(args, "overlay_non_layout_parent_unverified")
        args = overlay_syntax_fixture()
        args[0]["children"][0]["children"][0]["children"][0]["attributes"]["type"] = "UnknownNode"
        self.assert_failed(args, "layout_geometry_missing")

    def test_overlay_layout_nodes_still_require_parent_and_render_evidence(self):
        for parent in (None, "99"):
            with self.subTest(parent=parent):
                args = overlay_syntax_fixture()
                args[0]["children"][0]["children"][0]["attributes"]["LastParentId"] = parent
                self.assert_failed(args, "overlay_parent_evidence_mismatch")
        args = overlay_syntax_fixture()
        args[3]["render_tree"] = "\n".join(args[3]["render_tree"].splitlines()[:-1])
        self.assert_failed(args, "overlay_render_parent_unavailable")

    def test_recycle_dummy_ids_are_parent_scoped_and_preserved(self):
        args = fixture()
        for node_id in (3, 4):
            raw = {"attributes": {"ID": str(node_id), "type": "IfElse"}, "raw": "", "children": [
                {"attributes": {"ID": "-1", "type": "__Recycle__"}, "raw": "", "children": []}]}
            raw["children"][0]["attributes"]["Parent"] = raw["attributes"]["type"]
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
        self.assert_failed(args, "invalid_render_bounds")
        lines = args[3]["render_tree"].splitlines()
        self.assertIn("CANVAS_NODE[102]", lines[-1])
        args[3]["render_tree"] = "\n".join(lines[:-1])
        args[3]["render_detached_tree"] += lines[-1].strip()
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified")
        geometry = result["children"][0]["children"][0]["geometry"]
        self.assertEqual(geometry["screen_bounds"], {"x": 114, "y": 59, "width": 1, "height": 1})
        self.assertFalse(geometry["rendered"])
        self.assertEqual(geometry["render_state"], "not_rendered")

    def test_rounded_clip_preserves_corner_geometry_without_claiming_pixel_visibility(self):
        args = fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToBounds:[true]]",
            "CornerRadius[4.0 5.0 6.0 7.0], "
            "OtherModifiers:[ClipToBounds:[true], CornerRadius:[x:4.0 y:5.0 z:6.0 w:7.0]]")
        result = normalize_arkui(*args)
        clip = next(item for item in result["children"][0]["geometry"]["clip_chain"]
                    if item["kind"] == "rounded_rectangle")
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
        content = next(item for item in leaf["clip_chain"] if "local_bounds_ltrb" in item)
        self.assertEqual(content["bounds"], {"x": 117, "y": 70, "width": 23, "height": 40})

    def test_native_root_size_must_match_window(self):
        args = fixture()
        args[1]["bounding_box"]["width"] = 201
        for key in ("FrameRect", "PaintRect without transform"):
            args[3]["scene"]["path"][-1]["attributes"][key] = "RectT (100.00, 50.00) - [201.00 x 150.00]"
        args[3]["scene"]["native"]["$children"][0]["$rect"] = "[100.00,50.00],[301.00,200.00]"
        args[3]["render_tree"] = args[3]["render_tree"].replace("100 50 200 150", "100 50 201 150")
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
                tree = json.dumps({"appInfo": {"WindowID": 1, "BundleName": "com.ohos.sceneboard"},
                                   "$type": "root", "$ID": 0})
                destination.write_text("WinId: 5\nPid: 100\n" + tree, encoding="utf-8")

        hdc.receive.side_effect = receive
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "arkui").mkdir()
            raw = stage / "arkui" / "system-response.txt"
            raw.write_text("WinId: 5\nPid: 100\n|-> root childSize:0\n    ID: 0\n", encoding="utf-8")
            response = {**file_info(raw), "file": str(raw.relative_to(stage))}
            for window in windows:
                window["trees"]["arkui"]["response"] = response
            result = collect_geometry_evidence(hdc, stage, {"layout": "/data/local/tmp/probe"}, windows)
            self.assertEqual(result[5]["source_window_id"], 5)
            self.assertEqual(result[7]["source_window_id"], 5)
            self.assertEqual(result[5]["arkui_geometry"], result[7]["arkui_geometry"])
            self.assertEqual([call.args[1] for call in hdc.run.call_args_list],
                             ["geometry_5", "render_geometry", "render_detached_geometry"])
            self.assertEqual(result[5]["render_detached_tree"], result[7]["render_detached_tree"])
            self.assertEqual(result[5]["scene_sources"], result[7]["scene_sources"])
            self.assertEqual(result[5]["scene_sources"][0]["raw"], response)


if __name__ == "__main__":
    unittest.main()
