"""完整来源中的旋转、材质、透明历史状态和未物化零帧保持严格关联。"""

import copy
import unittest

from har_capture.coordinates import (modifier_values, normalize_arkui, owned_render_modifier, render_index)
from tests.test_coordinates import fixture
from tests.test_transparent_subtree import transparent_fixture, rewrite_render
from tests.test_viewport_geometry import empty_subtree_fixture, native_rectangle, raw_rectangle
from tests.test_window_mapping import component


def rotate_frame(args, node, render_id, angle, axis="Rotation"):
    node["attributes"][axis] = str(angle)
    rows = args[3]["render_tree"].splitlines(keepends=True)
    for index, row in enumerate(rows):
        if f"CANVAS_NODE[{render_id}]," in row:
            rows[index] = row.replace("OtherModifiers:[", f"{axis}[{angle}], OtherModifiers:[{axis}:[{angle}], ")
    args[3]["render_tree"] = "".join(rows)


def prepared_transparent_fixture(box="-inf -inf -inf -inf"):
    args = transparent_fixture()
    rewrite_render(args, 183, lambda row: row.replace("PrepareSeq: 0", "PrepareSeq: 177")
                   .replace("PostPrepareSeq: 0", "PostPrepareSeq: 146").replace("VsyncId: 0", "VsyncId: 2900000")
                   .replace("Bounds[-inf -inf -inf -inf] Frame[-inf -inf -inf -inf]",
                            f"Bounds[{box}] Frame[{box}]"))
    return args


def lazy_empty_fixture():
    args = empty_subtree_fixture()
    parent = args[0]["children"][0]["children"][0]
    child = parent["children"][0]
    wrapper = component(1350, "Column", raw_rectangle(0, 0, 0, 0))
    wrapper["raw"] = "CanvasNode[1350] child[] lazyLoad[1] nodeState[LAZY_LOAD]"
    wrapper["children"], parent["children"] = [child], [wrapper]
    native_parent = args[3]["arkui_geometry"]["$children"][0]["$children"][0]
    native_child = native_parent["$children"][0]
    native_parent["$children"] = [{"$ID": 1350, "$type": "Column", "$rect": native_rectangle(0, 169, 0, 0),
                                   "$children": [native_child]}]
    return args


class CompleteNativeGeometryTests(unittest.TestCase):
    def verified(self, args):
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        return result

    def rejected(self, args, code=""):
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        self.assertTrue(any(code in issue["code"] for issue in result["coordinate_validation"]["issues"]),
                        result["coordinate_validation"]["issues"])

    def test_rotated_parent_maps_descendants_and_retains_exact_clip_transform(self):
        args = fixture()
        parent = args[0]["children"][0]
        rotate_frame(args, parent, 101, 90)
        native_parent = args[3]["arkui_geometry"]["$children"][0]
        native_parent["$rect"] = "[20,10],[100,110]"
        native_parent["$children"][0]["$rect"] = "[80,15],[110,35]"
        result = self.verified(args)
        parent = result["children"][0]
        self.assertEqual(parent["geometry"]["screen_bounds"], {"x": 120, "y": 60, "width": 80, "height": 100})
        child = parent["children"][0]
        self.assertEqual(child["geometry"]["screen_bounds"], {"x": 180, "y": 65, "width": 30, "height": 20})
        clip = next(c for c in child["geometry"]["clip_chain"] if c.get("render_node_id") == "101")
        self.assertEqual(clip["local_to_screen"], [0, 1, -1, 0, 200, 60])
        self.assertEqual(clip["corners"], [[200, 60], [200, 160], [120, 160], [120, 60]])
        self.assertEqual(child["geometry"]["rectangular_clip_bounds"],
                         {"x": 180, "y": 65, "width": 20, "height": 20})

    def test_opposite_nested_rotations_compose_as_full_matrices(self):
        args = fixture()
        parent, native_parent = args[0]["children"][0], args[3]["arkui_geometry"]["$children"][0]
        rotate_frame(args, parent, 101, 90)
        rotate_frame(args, parent["children"][0], 102, -90)
        native_parent["$rect"] = "[20,10],[100,110]"
        native_parent["$children"][0]["$rect"] = "[85,10],[105,40]"
        child = self.verified(args)["children"][0]["children"][0]
        self.assertEqual(child["geometry"]["screen_bounds"], {"x": 185, "y": 60, "width": 20, "height": 30})

    def test_nonorthogonal_rotation_preserves_polygon_clip_instead_of_replacing_it_with_its_box(self):
        args = fixture()
        parent = args[0]["children"][0]
        parent["children"] = []
        native_parent = args[3]["arkui_geometry"]["$children"][0]
        native_parent["$children"] = []
        native_parent["$rect"] = "[-3.64,-3.64],[123.64,123.64]"
        args[3]["render_tree"] = "\n".join(row for row in args[3]["render_tree"].splitlines()
                                          if "CANVAS_NODE[102]" not in row)
        rotate_frame(args, parent, 101, 45)
        geometry = self.verified(args)["children"][0]["geometry"]
        clip = next(c for c in geometry["clip_chain"] if c.get("render_node_id") == "101")
        self.assertEqual(len(clip["corners"]), 4)
        self.assertNotEqual(clip["corners"][0][0], clip["bounds"]["x"])
        self.assertNotEqual(clip["local_to_screen"][1], 0)

    def test_native_projection_and_current_rotation_stay_independent(self):
        args = fixture()
        parent = args[0]["children"][0]
        rotate_frame(args, parent, 101, 90)
        self.rejected(args, "native_geometry_local_projection_mismatch")
        args[3]["arkui_geometry"]["$children"][0]["$rect"] = "[20,10],[100,110]"
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[80,15],[110,35]"
        args[3]["render_tree"] = args[3]["render_tree"].replace("Rotation[90]", "Rotation[-90]")
        self.rejected(args, "current_render_scalar_mismatch:Rotation")

    def test_nested_material_geometry_names_cannot_become_host_modifiers_or_clips(self):
        args = fixture()
        material = ("pid:1->MaterialNGFilter:[FrostedGlass:[Shape[SDFRRectShape:[RRect[rect:[0 0 20 30] "
                    "radius:[[1 1] [1 1] [1 1] [1 1]]]], ClipToBounds[True], Bounds[999 999 1 1], "
                    "Frame[999 999 1 1], Alpha:[0.0]], pid:1->Scale:[x:1 y:1]")
        rows = args[3]["render_tree"].splitlines(keepends=True)
        rows[-1] = rows[-1].replace("OtherModifiers:[]", f"OtherModifiers:[{material}]")
        args[3]["render_tree"] = "".join(rows)
        result = self.verified(args)
        child = result["children"][0]["children"][0]
        self.assertFalse(any(c.get("render_node_id") == "102" for c in child["geometry"]["clip_chain"]))
        values = modifier_values(rows[-1])
        self.assertEqual(set(values), {"MaterialNGFilter", "Scale"})
        record = render_index(args[3]["render_tree"])["102"]
        self.assertIsNone(owned_render_modifier(record, "Alpha", 1))
        self.assertIsNone(owned_render_modifier(record, "Bounds", 1))

    def test_top_level_unknown_geometry_is_not_hidden_by_a_material(self):
        args = fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToBounds:[true]]",
            "OtherModifiers:[pid:1->MaterialShader:[nested[Shape[SDFRRectShape:[x]]], "
            "pid:1->Skew:[x:1 y:0], pid:1->ClipToBounds:[true]]")
        self.rejected(args, "unsupported_render_modifier:Skew")

    def test_duplicate_current_clip_flags_are_rejected(self):
        args = fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "ClipToBounds[True]", "ClipToBounds[True], ClipToBounds[False]")
        self.rejected(args, "ambiguous_render_clip:ClipToBounds")

    def test_current_transform_and_clip_cannot_be_supplied_by_matching_pending_fields(self):
        for field, code in (("Scale[-1 1]", "current_render_vector_mismatch:Scale"),
                            ("Translate[100 0]", "current_render_vector_mismatch:Translate"),
                            ("TranslateZ[100]", "unsupported_render_transform:TranslateZ"),
                            ("ClipBounds[Path[x]]", "unsupported_current_render_modifier:ClipBounds")):
            args = fixture()
            args[3]["render_tree"] = args[3]["render_tree"].replace(
                "ClipToBounds[True], OtherModifiers:", f"ClipToBounds[True], {field}, OtherModifiers:")
            with self.subTest(field=field):
                self.rejected(args, code)

    def test_actual_current_corner_radius_is_exported_and_explicit_pending_conflict_is_rejected(self):
        args = fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "ClipToBounds[True], OtherModifiers:", "ClipToBounds[True], CornerRadius[30 30 30 30], OtherModifiers:")
        parent = self.verified(args)["children"][0]
        clip = next(c for c in parent["geometry"]["clip_chain"] if c.get("render_node_id") == "101")
        self.assertEqual(clip["kind"], "rounded_rectangle")
        self.assertEqual(clip["corner_radius_local"], [30, 30, 30, 30])
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToBounds:[true]]", "OtherModifiers:[ClipToBounds:[true], CornerRadius:[1 1 1 1]]")
        self.rejected(args, "current_render_corner_radius_mismatch")

    def test_camera_distance_alone_is_dormant_in_a_verified_two_dimensional_transform(self):
        args = fixture()
        parent = args[0]["children"][0]
        parent["attributes"]["CameraDistance"] = "-52"
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[ClipToBounds:[true]]",
            "OtherModifiers:[ClipToBounds:[true], CameraDistance:[-52], PositionZ:[2], "
            "PositionZApplicableCamera3d:[false]]")
        self.verified(args)
        bad = copy.deepcopy(args)
        bad[3]["render_tree"] = bad[3]["render_tree"].replace(
            "PositionZApplicableCamera3d:[false]", "PositionZApplicableCamera3d:[true]")
        self.rejected(bad, "unsupported_render_transform:PositionZ")
        bad = copy.deepcopy(args)
        parent = bad[0]["children"][0]
        rotate_frame(bad, parent, 101, 1, "RotationX")
        self.rejected(bad, "unsupported_raw_spatial_transform")

    def test_spatial_rotation_requires_an_exact_zero_area_leaf_and_draw_proof(self):
        args = fixture()
        child = args[0]["children"][0]["children"][0]
        child["attributes"].update(FrameRect=raw_rectangle(5, -10, 0, 0))
        child["attributes"]["PaintRect without transform"] = raw_rectangle(5, -10, 0, 0)
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[15,10],[15,10]"
        args[3]["render_tree"] = args[3]["render_tree"].replace("Bounds[5 -10 20 30] Frame[5 -10 20 30]",
            "Bounds[5 -10 0 0] Frame[5 -10 0 0]").replace("CANVAS_NODE[102],",
            "CANVAS_NODE[102], hasDrawContent: 0, hasChildrenDrawContent: 0,")
        rotate_frame(args, child, 102, 180, "RotationY")
        self.verified(args)
        bad = copy.deepcopy(args)
        bad[3]["render_tree"] = bad[3]["render_tree"].replace("hasDrawContent: 0,", "hasDrawContent: 1,")
        self.rejected(bad, "unsupported_render_transform:RotationX_or_Y")

    def test_prepared_transparent_descendant_retains_unapplied_current_state(self):
        for bounds in ("-inf -inf -inf -inf", "46 23 0 0"):
            args = prepared_transparent_fixture(bounds)
            child = self.verified(args)["children"][0]["children"][0]
            self.assertFalse(child["geometry"]["rendered"])
            self.assertEqual(child["geometry"]["source"]["render_geometry_state"], "unapplied")
            self.assertEqual(child["geometry"]["screen_bounds"], {"x": 1022, "y": 788, "width": 46, "height": 46})
        bad = prepared_transparent_fixture()
        rewrite_render(bad, 183, lambda row: row.replace("pid:2000->Bounds:", "pid:2001->Bounds:"))
        self.rejected(bad, "transparent_render_pending_bounds_mismatch")

    def test_transparent_current_conflicts_and_non_draw_claims_remain_rejected(self):
        args = prepared_transparent_fixture("46 23 0 0")
        rewrite_render(args, 183, lambda row: row.replace("Frame[46 23 0 0]", "Frame[46 23 1 0]"))
        self.rejected(args, "transparent_render_current_bounds_conflict")
        args = prepared_transparent_fixture()
        rewrite_render(args, 183, lambda row: row.replace("hasDrawContent: 0", "hasDrawContent: 1"))
        self.rejected(args, "transparent_render_preparation_unverified")

    def test_lazy_zero_frame_is_preserved_as_unmaterialized_in_empty_subtree(self):
        result = self.verified(lazy_empty_fixture())
        parent = result["children"][0]["children"][0]
        self.assertEqual(parent["geometry"]["source"]["no_draw_unmaterialized_render_node_ids"], ["1350"])
        wrapper = parent["children"][0]
        self.assertFalse(wrapper["geometry"]["rendered"])
        self.assertEqual(wrapper["geometry"]["render_state_source"], "ArkUI.lazy_unmaterialized")
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 5)
        bad = lazy_empty_fixture()
        bad[0]["children"][0]["children"][0]["children"][0]["raw"] = "CanvasNode[1350] child[] lazyLoad[0] nodeState[ACTIVE]"
        self.rejected(bad, "empty_render_subtree_attachment_unverified")

    def test_enabled_zero_area_clip_needs_same_process_bounds_and_current_flag(self):
        args = lazy_empty_fixture()
        parent = args[0]["children"][0]["children"][0]
        token = (2000 << 32) + 1
        parent["raw"] = (f"CanvasNode[1363] child[1364] lazyLoad[0] nodeState[ACTIVE] token:{token} "
                         f"modifierExtractor rsUIContext token is {token}node Is 1 "
                         "Bounds[0.0 0.0 0.0 0.0] Frame[0.0 0.0 0.0 0.0]")
        rows = args[3]["render_tree"].splitlines(keepends=True)
        for index, row in enumerate(rows):
            if "CANVAS_NODE[1363]" in row:
                rows[index] = row.replace("OtherModifiers:[]", f"ClipToBounds[True], RSUIContextToken: [ {token} ], OtherModifiers:["
                    "pid:2000->ClipToBounds:[true]]")
        args[3]["render_tree"] = "".join(rows)
        parent = self.verified(args)["children"][0]["children"][0]
        clip = next(c for c in parent["geometry"]["clip_chain"] if c.get("render_node_id") == "1363")
        self.assertEqual(clip["bounds_source"], "ArkUI.native_zero_area")
        self.assertEqual(parent["geometry"]["source"]["zero_area_clip_client"]["ui_context_token"], str(token))
        bad = copy.deepcopy(args)
        bad[3]["render_tree"] = bad[3]["render_tree"].replace("pid:2000->ClipToBounds:", "pid:2001->ClipToBounds:")
        self.rejected(bad, "unsupported_empty_render_clip")
        bad = copy.deepcopy(args)
        bad[0]["children"][0]["children"][0]["raw"] = parent["raw"].replace(str(token), str((2001 << 32) + 1))
        self.rejected(bad, "empty_render_clip_client_context_mismatch")
        bad = copy.deepcopy(args)
        bad[0]["children"][0]["children"][0]["raw"] = parent["raw"].replace(str(token), str(token + 1))
        self.rejected(bad, "empty_render_clip_client_context_mismatch")


if __name__ == "__main__":
    unittest.main()
