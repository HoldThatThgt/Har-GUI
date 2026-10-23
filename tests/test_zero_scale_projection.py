"""@brief 零缩放只改变前向投影，完整原文、原生框和独立渲染挂载证据仍须成立。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from har_capture import affine, coordinates
from har_capture.arkui import count_arkui_nodes, parse_forest
from har_capture.arkui_selection import read_window_arkui, select_native_tree
from har_capture.capture_facts import normalized_arkui
from har_capture.tree_capture import save_full_arkui_tree
from tests.test_coordinates import component, fixture
from tests.test_undefined_image_identity import raw_frame, save_source
from tests.test_window_mapping import render_line


def zero_scale_source():
    """@brief 匿名复现零面积宿主和正面积图片后代，原文仍保留图片的真实局部大小。"""
    text = raw_frame(0, ("root", 0, 10, 1), None, "RectT (0, 0) - [200 x 150]", 100)
    text += raw_frame(2, ("Column", 1, 11, 1), ("root", 0, 10), "RectT (10, 20) - [100 x 80]", 101)
    text += raw_frame(4, ("__Common__", 2, 12, 1), ("Column", 1, 11), "RectT (5, -10) - [0 x 0]", 102)
    text += "        scale(x,y): 0.000000,0.000000\n"
    text += ("      |-> JsView,3,13 childSize:1\n          ID: 3\n          AccessibilityId: 13\n"
             "          Description: Children\n          Parent: __Common__,2,12\n")
    text += raw_frame(8, ("Image", 4, 14, 0), ("JsView", 3, 13), "RectT (0, 0) - [53 x 53]", 104)
    text = text.replace("FrameRect: RectT (5, -10) - [0 x 0]", "FrameRect: RectT (4.5, -9.5) - [0 x 0]")
    native = {"$ID": 0, "$type": "root", "$rect": "[0,0],[200,150]", "$children": [
        {"$ID": 1, "$type": "Column", "$rect": "[10,20],[110,100]", "$children": [
            {"$ID": 2, "$type": "__Common__", "$rect": "[15,10],[15,10]", "visible": "false", "$children": [
                {"$ID": 3, "$type": "JsView", "$children": [
                    {"$ID": 4, "$type": "Image", "$rect": "[15,10],[15,10]"}]}]}]}]}
    return text, native


def zero_scale_fixture():
    """@brief 原始与待应用缩放均为零；当前 RS 全无穷框只作为明确未挂载来源。"""
    args = list(fixture())
    text, native = zero_scale_source()
    args[0] = select_native_tree(parse_forest(text), native)
    args[3]["arkui_geometry"] = native
    args[3]["render_tree"] = "".join(row for row in args[3]["render_tree"].splitlines(keepends=True)
                                     if "CANVAS_NODE[102]" not in row)
    flags = ("PrepareSeq: 0, PostPrepareSeq: 0, VsyncId: 0, IsSubTreeSkipped: 0, "
             "hasDrawContent: 0, hasChildrenDrawContent: 0, ")
    parent = ("OtherModifiers:[pid:1001->Bounds:[x:5 y:-10 z:0 w:0], pid:1001->Frame:[x:5 y:-10 z:0 w:0], "
              "pid:1001->Scale:[x:0.0 y:0.0], pid:1001->Pivot:[x:0.5 y:0.5]], sortedChildren: 1(104 )")
    child = ("OtherModifiers:[pid:1001->Bounds:[x:0 y:0 z:53 w:53], pid:1001->Frame:[x:0 y:0 z:53 w:53], "
             "pid:1001->Scale:[x:1.0 y:1.0], pid:1001->Pivot:[x:0.5 y:0.5]]")
    args[3]["render_detached_tree"] = "-- Render Node Not On Tree\n"
    args[3]["render_detached_tree"] += render_line(0, "CANVAS_NODE", 102, "-inf -inf -inf -inf", flags + parent)
    args[3]["render_detached_tree"] += render_line(1, "CANVAS_NODE", 104, "-inf -inf -inf -inf", flags + child)
    return args


def saved_zero_scale_fixture(stage):
    """@brief 使用生产完整来源保存接口，确保正例可由正式 normalized_arkui 重新读取。"""
    args = zero_scale_fixture()
    window, canvas, evidence = args[1:]
    window["bundle_name"] = "com.example.zero-scale"
    text, native = zero_scale_source()
    native["appInfo"] = {"WindowID": 9, "BundleName": window["bundle_name"]}
    header = "WinId: 9\nPid: 1001\n"
    raw = save_source(stage, header + text, "application-response.txt")
    geometry = {"response": save_source(stage, header + json.dumps(native), "native-response.txt"),
                "arkui_geometry": save_source(stage, json.dumps(native), "native.json"),
                "render_tree": save_source(stage, evidence["render_tree"], "render-tree.txt"),
                "render_detached_tree": save_source(stage, evidence["render_detached_tree"], "render-detached.txt")}
    record = save_full_arkui_tree(stage, stage / raw["file"], geometry)
    record.update(response=raw, origin={"kind": "stdout"}, session={"is_system": False})
    window["trees"] = {"arkui": record}
    return window, canvas, {9: evidence["scene"]}


class ZeroScaleProjectionTests(unittest.TestCase):
    def verified(self, args):
        result = coordinates.normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        return result

    def rejected(self, args, code):
        with self.assertLogs(level="ERROR"):
            result = coordinates.normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        self.assertTrue(any(code in issue["code"] for issue in result["coordinate_validation"]["issues"]),
                        result["coordinate_validation"]["issues"])
        self.assertEqual(count_arkui_nodes(result), count_arkui_nodes(args[0]))
        return result

    def test_detached_zero_scale_preserves_parent_and_positive_size_descendant(self):
        args = zero_scale_fixture()
        before = copy.deepcopy(args)
        result = self.verified(args)
        parent = result["children"][0]["children"][0]
        image = parent["children"][0]["children"][0]
        self.assertEqual(count_arkui_nodes(result), 5)
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 4)
        self.assertEqual(result["coordinate_validation"]["non_layout_nodes"], 1)
        self.assertEqual(parent["attributes"]["scale(x,y)"], "0.000000,0.000000")
        self.assertEqual(image["attributes"]["PaintRect without transform"], "RectT (0, 0) - [53 x 53]")
        for node in (parent, image):
            self.assertEqual(node["geometry"]["screen_bounds"], {"x": 115, "y": 60, "width": 0, "height": 0})
            self.assertEqual(node["geometry"]["native_window_rect"], "[15,10],[15,10]")
            self.assertFalse(node["geometry"]["rendered"])
            self.assertEqual(node["geometry"]["render_state_source"], "RenderService.allNodeNotOnTree")
            self.assertFalse(node["geometry"]["source"]["render_node_on_screen_tree"])
            self.assertNotIn("render_geometry_state", node["geometry"]["source"])
            self.assertIsNone(node["geometry"]["rectangular_clip_bounds"])
        self.assertEqual(args, before)

    def test_forward_matrices_collapse_all_child_points_without_changing_its_local_rect(self):
        args = zero_scale_fixture()
        context = coordinates.prepare_context(*args)
        expected = (0, 0, 0, 0, 115, 60)
        self.assertEqual(context["matrices"]["102"], expected)
        self.assertEqual(context["matrices"]["104"], expected)
        self.assertEqual(affine.corners(context["matrices"]["104"],
                                        {"x": 0, "y": 0, "width": 53, "height": 53}), ((115, 60),) * 4)
        self.assertEqual(affine.apply(context["matrices"]["104"], -100, 250), (115, 60))
        self.assertEqual(context["raw_frames"]["4"]["attributes"]["FrameRect"], "RectT (0, 0) - [53 x 53]")

    def test_production_normalized_arkui_reads_full_sources_without_rewriting_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            window, canvas, scenes = saved_zero_scale_fixture(stage)
            before = {path.name: path.read_bytes() for path in stage.iterdir()}
            original = read_window_arkui(stage, window)
            result = normalized_arkui(stage, window, canvas, scenes)["tree"]
            self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 4)
            old_parent = original["children"][0]["children"][0]
            parent = result["children"][0]["children"][0]
            for old, new in ((old_parent, parent), (old_parent["children"][0]["children"][0],
                                                   parent["children"][0]["children"][0])):
                for key in ("attributes", "source_attributes", "raw", "source_header", "source_line"):
                    self.assertEqual(new[key], old[key])
                self.assertEqual(new["geometry"]["screen_bounds"], {"x": 115, "y": 60, "width": 0, "height": 0})
            self.assertEqual(before, {path.name: path.read_bytes() for path in stage.iterdir()})

    def test_pending_zero_scale_does_not_become_a_claim_about_current_rs_properties(self):
        args = zero_scale_fixture()
        record = coordinates.detached_render_index(args[3]["render_detached_tree"])["102"]
        self.assertEqual(coordinates.modifier_values(record["line"])["Scale"], ["x:0.0 y:0.0"])
        current = coordinates.render_current_properties(record)
        self.assertNotIn("ScaleX[", current)
        self.assertNotIn("ScaleY[", current)
        with self.assertRaisesRegex(ValueError, "non_finite_geometry"):
            coordinates.current_local_matrix(record)
        self.assertEqual(self.verified(args)["coordinate_validation"]["status"], "verified")

    def test_conflicting_native_parent_or_child_bounds_remain_rejected(self):
        for target in ("parent", "image"):
            args = zero_scale_fixture()
            node = args[3]["arkui_geometry"]["$children"][0]["$children"][0]
            if target == "image":
                node = node["$children"][0]["$children"][0]
            node["$rect"] = "[16,10],[16,10]"
            with self.subTest(target=target):
                self.rejected(args, "native_geometry_local_projection_mismatch")

    def test_identity_scale_cannot_impersonate_a_collapsed_positive_area_child(self):
        args = zero_scale_fixture()
        parent = args[0]["children"][0]["children"][0]
        parent["attributes"]["scale(x,y)"] = "1.000000,1.000000"
        self.rejected(args, "native_geometry_local_projection_mismatch")

    def test_wrong_native_parent_still_fails_when_point_rectangles_are_identical(self):
        args = zero_scale_fixture()
        column = args[3]["arkui_geometry"]["$children"][0]
        view = column["$children"][0]["$children"][0]
        column["$children"].append(view["$children"].pop())
        self.rejected(args, "native_geometry_parent_mismatch")

    def test_missing_detached_image_evidence_is_not_inferred_from_zero_native_area(self):
        args = zero_scale_fixture()
        rows = args[3]["render_detached_tree"].splitlines(keepends=True)
        args[3]["render_detached_tree"] = "".join(row for row in rows if "CANVAS_NODE[104]" not in row)
        self.rejected(args, "active_render_node_missing:104")

    def test_mounted_and_detached_identity_conflict_is_still_rejected(self):
        args = zero_scale_fixture()
        args[3]["render_tree"] += render_line(5, "CANVAS_NODE", 104, "0 0 53 53", "OtherModifiers:[]")
        self.rejected(args, "render_attachment_state_changed")

    def test_mounted_zero_crossing_interval_still_cannot_supply_a_current_matrix(self):
        for text in ("ScaleX[0] ScaleY[1]", "ScaleX[1] ScaleY[0.0]", "ScaleX[0.0] ScaleY[0.0]"):
            record = coordinates.render_index(render_line(0, "CANVAS_NODE", 104, "0 0 53 53",
                                                           text + ", OtherModifiers:[]"))["104"]
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "unsupported_render_zero_scale"):
                coordinates.current_local_matrix(record)

    def test_forward_zero_axis_reflection_and_parent_composition_have_exact_geometry(self):
        cases = [
            ((0, 2), 0, affine.IDENTITY, (0, 0, 0, 2, 15, 15), (15, 15, 0, 8)),
            ((-2, 0), 0, affine.IDENTITY, (-2, 0, 0, 0, 19, 21), (3, 21, 16, 0)),
            ((0, 0), 0, affine.IDENTITY, (0, 0, 0, 0, 15, 21), (15, 21, 0, 0)),
            ((0, -2), 90, affine.IDENTITY, (0, 0, 2, 0, 9, 21), (9, 21, 8, 0)),
            ((0, 2), 0, (2, 0, 0, 3, 100, -50), (0, 0, 0, 6, 130, -5), (130, -5, 0, 24)),
            ((0, 2), 0, (0, 1, -1, 0, 100, 50), (0, 0, -2, 0, 85, 65), (77, 65, 8, 0)),
        ]
        for scale, rotation, outer, expected_matrix, expected_bounds in cases:
            node = component(9, "__Common__", "RectT (10, 20) - [8 x 4]", properties={
                "scale(x,y)": f"{scale[0]},{scale[1]}", "translate(x,y)": "3,-2",
                "Center": "x:0.25 y:0.75", "Rotation": str(rotation)})
            with self.subTest(scale=scale, rotation=rotation, parent=outer):
                projection, bounds = coordinates.local_projection(node, (outer, 0))
                self.assertEqual(projection[0], expected_matrix)
                self.assertEqual(bounds, dict(zip(("x", "y", "width", "height"), expected_bounds)))

    def test_nonfinite_components_and_wrong_vector_lengths_remain_invalid(self):
        cases = (("scale(x,y)", "0"), ("scale(x,y)", "0,0,0"), ("scale(x,y)", "nan,0"),
                 ("scale(x,y)", "0,inf"), ("scale(x,y)", "0,-inf"), ("scale(x,y)", "1e309,0"),
                 ("translate(x,y)", "0"), ("translate(x,y)", "0,0,0"), ("translate(x,y)", "nan,0"),
                 ("Center", "x:0.5"), ("Center", "x:0.5 y:0.5 z:0"), ("Rotation", "nan"))
        for field, value in cases:
            node = component(9, "__Common__", "RectT (10, 20) - [8 x 4]", properties={"scale(x,y)": "0,0"})
            node["attributes"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                coordinates.local_projection(node, coordinates.IDENTITY_PROJECTION)

    def test_zero_scale_does_not_bypass_unsupported_spatial_transforms(self):
        for field, value in (("RotationX", "30"), ("RotationY", "15"), ("TranslateZ", "1"), ("PivotZ", "1")):
            node = component(9, "__Common__", "RectT (10, 20) - [8 x 4]", properties={"scale(x,y)": "0,0"})
            node["attributes"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unsupported_raw_spatial_transform"):
                coordinates.local_projection(node, coordinates.IDENTITY_PROJECTION)


if __name__ == "__main__":
    unittest.main()
