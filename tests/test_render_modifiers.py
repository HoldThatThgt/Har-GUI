"""@brief 官方空 ClipRrect 序列化必须由完整结构及当前 drawable 状态共同证明。"""

import copy
import unittest

from har_capture.coordinates import modifier_values, normalize_arkui, verify_modifiers
from tests.test_coordinates import fixture


METADATA = "drawableVecStatus: 48, opincRootCacheNull: true, layerPartCacheNull: true"


def clip_rrect(bounds=(0, 0, 0, 0), radius=(0, 0, 0, 0, 0, 0, 0, 0)):
    """@brief 保留官方 dump 缺少 ClipRrect 自身右括号的精确格式。"""
    x, y, width, height = bounds
    pairs = " ".join(f"[{radius[index]:.6f} {radius[index + 1]:.6f}]" for index in range(0, 8, 2))
    return (f"ClipRrect:[rect:[x:{x:.6f} y:{y:.6f} width:{width:.6f} height:{height:.6f}] "
            f"radius:[{pairs}]")


def render_line(modifiers=None, metadata=METADATA, prefix=""):
    """@brief 外层 OtherModifiers 的右括号独立存在，不能借给 ClipRrect。"""
    content = clip_rrect() if modifiers is None else modifiers
    return ("| CANVAS_NODE[102], Properties: Bounds[5 -10 20 30] Frame[5 -10 20 30], "
            f"{prefix}{metadata}, OtherModifiers:[pid:1001->{content}], sortedChildren: 0()")


def record(line):
    """@brief 独立验证同一原生节点，不引入坐标归一化的其他前置条件。"""
    return {"id": "102", "kind": "CANVAS_NODE", "line": line, "children_count": 0}


def fixture_with_line(line):
    """@brief 只替换叶节点原文，保留父节点现有裁剪和原生几何证据。"""
    args = fixture()
    rows = args[3]["render_tree"].splitlines(keepends=True)
    rows[-1] = "          " + line + "\n"
    args[3]["render_tree"] = "".join(rows)
    return args


class RenderModifierTests(unittest.TestCase):
    def reject_line(self, line, expression=r"ClipRrect|RRect|serialization|invalid_current_render_clip_status"):
        with self.assertRaisesRegex(ValueError, expression):
            verify_modifiers(record(line))

    def test_official_twelve_values_without_own_closing_bracket_are_preserved(self):
        values = modifier_values(render_line())
        self.assertEqual(set(values), {"ClipRrect"})
        self.assertEqual(len(values["ClipRrect"]), 1)
        self.assertIn("rect:[x:0.000000 y:0.000000 width:0.000000 height:0.000000]",
                      values["ClipRrect"][0])
        self.assertIn("radius:[[0.000000 0.000000]", values["ClipRrect"][0])
        self.assertEqual(verify_modifiers(record(render_line())), values)

    def test_either_nonpositive_printed_extent_accepts_with_disabled_current_clip(self):
        for bounds in ((0, 0, 0, 10), (0, 0, 10, 0), (3, -4, -1, 8), (3, -4, 8, -1)):
            with self.subTest(bounds=bounds):
                verify_modifiers(record(render_line(clip_rrect(bounds))))

    def test_empty_rect_does_not_require_zero_origin_or_zero_radii(self):
        line = render_line(clip_rrect((3, -4, 0, 10), (1, 2, 3, 4, 5, 6, 7, 8)))
        verify_modifiers(record(line))

    def test_positive_area_rrect_is_rejected_even_without_rounding(self):
        for radius in ((0,) * 8, (1, 2, 3, 4, 5, 6, 7, 8)):
            with self.subTest(radius=radius):
                self.reject_line(render_line(clip_rrect((0, 0, 20, 30), radius)))

    def test_printed_zero_is_not_proof_that_current_clip_is_disabled(self):
        for status in (1, 49):
            with self.subTest(status=status):
                self.reject_line(render_line(metadata=METADATA.replace(": 48,", f": {status},")))

    def test_pending_empty_clip_cannot_authorize_a_current_rrect(self):
        line = render_line().replace("Frame[5 -10 20 30],", "Frame[5 -10 20 30], ClipRrect[0 0 20 30],")
        self.reject_line(line, r"unsupported_current_render_modifier:ClipRrect")

    def test_clip_bit_is_independent_of_other_drawable_bits(self):
        for status in (0, 2, 16, 48, 50):
            with self.subTest(status=status):
                verify_modifiers(record(render_line(metadata=METADATA.replace(": 48,", f": {status},"))))

    def test_missing_duplicate_or_invalid_top_level_status_is_rejected(self):
        bad_metadata = ["", METADATA + ", " + METADATA,
                        METADATA.replace("48", "-1"), METADATA.replace("48", "48.0"),
                        METADATA.replace("48", "0x30"), METADATA.replace("48", "48suffix"),
                        METADATA.replace("48", "NaN"), METADATA.replace("48", "[48]"),
                        METADATA.replace("drawableVecStatus: 48, ", "")]
        for metadata in bad_metadata:
            with self.subTest(metadata=metadata):
                self.reject_line(render_line(metadata=metadata))

    def test_status_requires_its_exact_official_following_fields(self):
        bad_metadata = ["drawableVecStatus: 48",
                        "drawableVecStatus: 48, opincRootCacheNull: true",
                        "drawableVecStatus: 48, layerPartCacheNull: true, opincRootCacheNull: true",
                        METADATA.replace("opincRootCacheNull: true", "opincRootCacheNull: maybe"),
                        METADATA.replace("layerPartCacheNull: true", "layerPartCacheNull: maybe"),
                        METADATA.replace(", opinc", ", unexpectedField: 1, opinc")]
        for metadata in bad_metadata:
            with self.subTest(metadata=metadata):
                self.reject_line(render_line(metadata=metadata))

    def test_nested_fake_status_cannot_replace_missing_top_level_evidence(self):
        for prefix in (f"nodeName: [{METADATA}], ", f"DrawCmdModifiers:[Diagnostic:[{METADATA}]], "):
            with self.subTest(prefix=prefix):
                self.reject_line(render_line(metadata="", prefix=prefix))
        self.reject_line(render_line(f"Diagnostic:[{METADATA}], {clip_rrect()}", metadata=""))

    def test_nested_fake_status_does_not_shadow_real_top_level_status(self):
        fake = METADATA.replace("48", "49")
        verify_modifiers(record(render_line(prefix=f"nodeName: [{fake}], ")))
        line = render_line().replace(", OtherModifiers:", f", DrawCmdModifiers:[Diagnostic:[{fake}]], OtherModifiers:")
        verify_modifiers(record(line))
        verify_modifiers(record(render_line(f"Diagnostic:[{fake}], {clip_rrect()}")))

    def test_current_clip_flag_is_independent_of_printed_pending_zero(self):
        line = render_line().replace("Frame[5 -10 20 30],", "Frame[5 -10 20 30], ClipToBounds[True],")
        self.reject_line(line, r"unverified_current_render_clip:ClipRrect")

    def test_conflicting_pending_rrects_are_rejected(self):
        line = render_line(clip_rrect() + ", pid:2002->" + clip_rrect((0, 0, 0, 20)))
        self.reject_line(line, r"ambiguous_render_clip:ClipRrect")

    def test_nested_fake_disabled_status_cannot_override_enabled_current_clip(self):
        for prefix in (f"nodeName: [{METADATA}], ", f"DrawCmdModifiers:[Diagnostic:[{METADATA}]], "):
            with self.subTest(prefix=prefix):
                self.reject_line(render_line(metadata=METADATA.replace("48", "49"), prefix=prefix))

    def test_unbalanced_top_level_metadata_context_is_rejected(self):
        for prefix in ("nodeName: [unfinished, ", "DrawCmdModifiers:[Diagnostic:[unfinished], "):
            with self.subTest(prefix=prefix):
                self.reject_line(render_line(prefix=prefix))

    def test_following_same_owner_or_other_owner_skew_is_not_swallowed(self):
        for separator in (", ", ", pid:1001->", ", pid:2002->"):
            line = render_line(clip_rrect() + separator + "Skew:[x:1 y:0]")
            with self.subTest(separator=separator):
                values = modifier_values(line)
                self.assertEqual(set(values), {"ClipRrect", "Skew"})
                self.assertEqual(values["Skew"], ["x:1 y:0"])
                self.reject_line(line, r"unsupported_render_modifier:Skew")

    def test_missing_inner_brackets_and_wrong_field_boundaries_are_rejected(self):
        value = clip_rrect()
        malformed = [value.replace("height:0.000000]", "height:0.000000"),
                     value.replace("radius:[[", "radius:[", 1),
                     value.replace("[0.000000 0.000000]", "[0.000000]", 1),
                     value.replace("[0.000000 0.000000]", "[0.000000 0.000000 0.000000]", 1),
                     value.replace("width:", "depth:"), value.replace("radius:", "radii:"),
                     value.replace("radius:[[", "radius:[pid:2002->[", 1), value[:-1]]
        for modifiers in malformed:
            with self.subTest(modifiers=modifiers):
                self.reject_line(render_line(modifiers))

    def test_nonfinite_rrect_coordinates_and_radii_are_rejected(self):
        for invalid in ("nan", "inf", "-inf"):
            for field in ("width:0.000000", "radius:[[0.000000"):
                modifiers = clip_rrect().replace(field, field.replace("0.000000", invalid), 1)
                with self.subTest(invalid=invalid, field=field):
                    self.reject_line(render_line(modifiers))

    def test_other_unclosed_modifiers_cannot_borrow_clip_or_outer_brackets(self):
        for modifiers in ("ShadowMask:[x:0", "Scale:[x:1 y:1", "Translate:[x:0 y:0",
                          clip_rrect() + ", ShadowMask:[x:0"):
            with self.subTest(modifiers=modifiers):
                self.reject_line(render_line(modifiers), r"serialization")

    def test_missing_outer_modifier_boundary_is_rejected(self):
        self.reject_line(render_line().replace("], sortedChildren", ", sortedChildren"), r"serialization")

    def test_normalize_preserves_every_node_and_existing_parent_clip(self):
        baseline = normalize_arkui(*fixture())
        args = fixture_with_line(render_line())
        original = copy.deepcopy(args[0])
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        self.assertEqual(args[0], original)
        self.assertEqual(result["attributes"]["ID"], "0")
        self.assertEqual(len(result["children"]), 1)
        self.assertEqual(result["children"][0]["attributes"]["ID"], "1")
        child = result["children"][0]["children"][0]
        self.assertEqual(child["attributes"]["ID"], "2")
        self.assertEqual(child["children"], [])
        original_geometry = baseline["children"][0]["children"][0]["geometry"]
        self.assertEqual(child["geometry"]["clip_chain"], original_geometry["clip_chain"])
        self.assertEqual(child["geometry"]["screen_bounds"], original_geometry["screen_bounds"])
        self.assertEqual(child["geometry"]["rectangular_clip_bounds"],
                         {"x": 115, "y": 70, "width": 20, "height": 20})
        self.assertFalse(any(item.get("render_node_id") == "102" for item in child["geometry"]["clip_chain"]))


if __name__ == "__main__":
    unittest.main()
