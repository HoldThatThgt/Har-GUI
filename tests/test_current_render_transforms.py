"""@brief 验证官方当前逐轴字段、打印精度和原始几何证据各自独立。"""

import copy
import unittest

from har_capture import affine
from har_capture.coordinates import (
    ROSEN_FLOAT_EPSILON,
    current_local_matrix,
    current_vector_intervals,
    normalize_arkui,
    printed_interval,
    verify_current_vector,
    verify_render_vectors,
)
from tests.test_coordinates import component, fixture


def render_record(current="", modifier=""):
    """@brief 构造匿名官方属性行，当前字段与修饰器字段保持独立。"""
    return {"id": "102", "kind": "CANVAS_NODE", "children_count": 0,
            "line": "| CANVAS_NODE[102], Properties: Bounds[5 -10 2560 30] Frame[5 -10 2560 30], "
                    + current + ", OtherModifiers:[" + modifier + "]"}


def rounded_scale_fixture():
    """@brief 用匿名宽框复现精确缩放与当前一位小数打印的差别。"""
    args = fixture()
    leaf = args[0]["children"][0]["children"][0]
    leaf["attributes"].update({"FrameRect": "RectT (5.00, -10.00) - [2560.00 x 30.00]",
                               "PaintRect without transform": "RectT (5.00, -10.00) - [2560.00 x 30.00]",
                               "scale(x,y)": "1.001000,1.001000"})
    leaf["source_attributes"] = copy.deepcopy(leaf["attributes"])
    args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[13.72,9.99],[2576.28,40.02]"
    line = render_record("ScaleX[1.0] ScaleY[1.0]", "Scale:[x:1.001000 y:1.001000]")["line"]
    rows = args[3]["render_tree"].splitlines(keepends=True)
    rows[-1] = "          " + line + "\n"
    args[3]["render_tree"] = "".join(rows)
    return args


class CurrentRenderTransformTests(unittest.TestCase):
    def test_printed_intervals_preserve_decimal_and_exponent_precision(self):
        cases = (("1", (1, 1)), ("1.0", (0.95, 1.05)), ("1.001000", (1.0009995, 1.0010005)),
                 ("1.00e2", (99.5, 100.5)), ("-1.00e-2", (-0.01005, -0.00995)))
        for text, expected in cases:
            with self.subTest(text=text):
                for actual, wanted in zip(printed_interval(text), expected, strict=True):
                    self.assertAlmostEqual(actual, wanted, places=14)

    def test_axis_fields_parse_independently_and_keep_printed_uncertainty(self):
        render = render_record("ScaleX[1.0] ScaleY[-2.50] TranslateX[4.00] TranslateY[-3.0]")
        scale = current_vector_intervals(render, "Scale")
        translate = current_vector_intervals(render, "Translate")
        self.assertEqual(scale["parameters"], [(0.95, 1.05), (-2.505, -2.495)])
        self.assertEqual(scale["matrix"], scale["parameters"])
        self.assertEqual(scale["present"], [True, True])
        self.assertEqual(translate["parameters"], [(3.995, 4.005), (-3.05, -2.95)])

    def test_two_omitted_axes_have_default_parameter_range_but_exact_applied_identity(self):
        for name, default in (("Scale", 1.0), ("Translate", 0.0)):
            with self.subTest(name=name):
                state = current_vector_intervals(render_record(), name)
                self.assertEqual(state["parameters"],
                                 [(default - ROSEN_FLOAT_EPSILON, default + ROSEN_FLOAT_EPSILON)] * 2)
                self.assertEqual(state["matrix"], [(default, default)] * 2)
                self.assertEqual(state["present"], [False, False])
                verify_current_vector(render_record(), name, f"{default},{default}", [0, 1])

    def test_one_omitted_axis_keeps_float_epsilon_in_applied_matrix(self):
        cases = (("Scale", "ScaleX[2.0]", 1.0), ("Translate", "TranslateX[4.0]", 0.0))
        for name, current, default in cases:
            with self.subTest(name=name):
                state = current_vector_intervals(render_record(current), name)
                self.assertEqual(state["present"], [True, False])
                self.assertEqual(state["matrix"][1],
                                 (default - ROSEN_FLOAT_EPSILON, default + ROSEN_FLOAT_EPSILON))
                near_default = default + ROSEN_FLOAT_EPSILON / 2
                original = ("2" if name == "Scale" else "4") + f",{near_default:.15f}"
                verify_current_vector(render_record(current), name, original, [0, 1])

    def test_missing_nondefault_axis_is_rejected_even_if_modifier_supplies_it(self):
        cases = (("Scale", "ScaleX[2.0]", "Scale:[x:2 y:3]", "2,3"),
                 ("Translate", "TranslateY[4.0]", "Translate:[x:3 y:4]", "3,4"))
        for name, current, modifier, original in cases:
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "current_render_vector_missing:" + name):
                verify_current_vector(render_record(current, modifier), name, original, [0, 1])

    def test_omitted_axes_do_not_borrow_modifier_transform(self):
        render = render_record(modifier="Scale:[x:1.001000 y:1.001000], Translate:[x:20 y:30]")
        self.assertEqual(current_vector_intervals(render, "Scale")["matrix"], [(1.0, 1.0)] * 2)
        self.assertEqual(current_vector_intervals(render, "Translate")["matrix"], [(0.0, 0.0)] * 2)
        with self.assertRaisesRegex(ValueError, "current_render_vector_missing:Scale"):
            verify_current_vector(render, "Scale", "1.001000,1.001000", [0, 1])

    def test_raw_scale_inside_current_printed_range_is_compatible(self):
        render = render_record("ScaleX[1.0] ScaleY[1.0]", "Scale:[x:1.001000 y:1.001000]")
        node = component(2, "Text", "RectT (5.00, -10.00) - [2560.00 x 30.00]",
                         properties={"scale(x,y)": "1.001000,1.001000"})
        verify_render_vectors(render, node)
        matrix = current_local_matrix(render)
        self.assertLessEqual(matrix[0][0], 0.95)
        self.assertGreaterEqual(matrix[0][1], 1.05)
        self.assertNotEqual(matrix[0], (1, 1))
        exact = affine.local({"x": 5, "y": -10, "width": 2560, "height": 30},
                             (1.001, 1.001), (0, 0), (0.5, 0.5), 0)
        for interval, value in zip(matrix, exact, strict=True):
            self.assertLessEqual(interval[0], value)
            self.assertGreaterEqual(interval[1], value)

    def test_current_local_matrix_is_independent_of_modifier_values(self):
        current = "ScaleX[1.0] ScaleY[2.0] TranslateX[3.0] TranslateY[-4.0] Pivot[0.25,0.75]"
        first = render_record(current, "Scale:[x:1.001000 y:2], Translate:[x:3 y:-4], Pivot:[x:0.25 y:0.75]")
        second = render_record(current, "Scale:[x:8 y:9], Translate:[x:100 y:200], Pivot:[x:0.1 y:0.2]")
        self.assertEqual(current_local_matrix(first), current_local_matrix(second))

    def test_rounded_current_spatial_fields_cannot_claim_the_default_two_dimensional_path(self):
        node = component(2, "Text", "RectT (5.00, -10.00) - [2560.00 x 30.00]")
        for field in ("RotationX[0.0]", "RotationY[0.0]", "TranslateZ[0.0]", "PivotZ[0.0]",
                      "ScaleZ[1.0]", "Quaternion[0.0 0.0 0.0 1.0]"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "unsupported"):
                verify_render_vectors(render_record(field), node)

    def test_raw_scale_outside_current_printed_range_is_rejected(self):
        render = render_record("ScaleX[1.0] ScaleY[1.0]", "Scale:[x:1.2 y:1.2]")
        node = component(2, "Text", "RectT (5.00, -10.00) - [2560.00 x 30.00]",
                         properties={"scale(x,y)": "1.200000,1.200000"})
        with self.assertRaisesRegex(ValueError, "current_render_vector_mismatch:Scale"):
            verify_render_vectors(render, node)

    def test_higher_current_print_precision_rejects_same_raw_scale(self):
        for current in ("ScaleX[1] ScaleY[1]", "ScaleX[1.000000] ScaleY[1.000000]"):
            with self.subTest(current=current), self.assertRaisesRegex(ValueError, "current_render_vector_mismatch"):
                verify_current_vector(render_record(current), "Scale", "1.001000,1.001000", [0, 1])

    def test_modifier_conflict_is_rejected_even_when_current_matches_raw(self):
        render = render_record("ScaleX[1.0] ScaleY[1.0]", "Scale:[x:1.200000 y:1.200000]")
        node = component(2, "Text", "RectT (5.00, -10.00) - [2560.00 x 30.00]",
                         properties={"scale(x,y)": "1.001000,1.001000"})
        with self.assertRaisesRegex(ValueError, "scene_render_vector_mismatch:Scale"):
            verify_render_vectors(render, node)

    def test_duplicate_axis_is_rejected_even_when_both_values_match(self):
        for name in ("Scale", "Translate"):
            for axis in ("X", "Y"):
                current = f"{name}{axis}[1.0] {name}{axis}[1.0]"
                with self.subTest(current=current), self.assertRaisesRegex(ValueError, "ambiguous_current"):
                    current_vector_intervals(render_record(current), name)

    def test_nonfinite_empty_truncated_and_multivalue_axis_are_rejected(self):
        for name in ("Scale", "Translate"):
            for text in ("nan", "inf", "-inf", "1e309", "", "1 2", "1,2", "1.0tail"):
                current = name + "X[" + text + "]"
                with self.subTest(current=current), self.assertRaises(ValueError):
                    current_vector_intervals(render_record(current), name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                current_vector_intervals(render_record(name + "X[1.0"), name)

    def test_legacy_combined_current_vector_is_rejected(self):
        for name in ("Scale", "Translate"):
            for current in (name + "[1 2]", name + "[x:1 y:2]", name + "[1,2]"):
                with self.subTest(current=current), self.assertRaisesRegex(ValueError, "invalid_current.*format"):
                    current_vector_intervals(render_record(current), name)

    def test_current_pivot_requires_comma_and_does_not_use_modifier_typed_fields(self):
        render = render_record("Pivot[0.25, 0.75]", "Pivot:[x:0.1 y:0.9]")
        state = current_vector_intervals(render, "Pivot")
        self.assertEqual(state["parameters"], [(0.245, 0.255), (0.745, 0.755)])
        self.assertEqual(state["present"], [True, True])
        verify_current_vector(render, "Pivot", "x:0.250000 y:0.750000", [0, 1])
        for text in ("0.25 0.75", "x:0.25 y:0.75", "0.25", "0.25,inf", "0.25,0.75,1"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "invalid_current_render_vector:Pivot"):
                current_vector_intervals(render_record("Pivot[" + text + "]"), "Pivot")

    def test_pivot_duplicate_is_rejected_and_omission_keeps_default_range(self):
        state = current_vector_intervals(render_record(modifier="Pivot:[x:0.1 y:0.9]"), "Pivot")
        expected = [(0.5 - ROSEN_FLOAT_EPSILON, 0.5 + ROSEN_FLOAT_EPSILON)] * 2
        self.assertEqual(state["parameters"], expected)
        self.assertEqual(state["matrix"], expected)
        with self.assertRaisesRegex(ValueError, "ambiguous_current_render_vector:Pivot"):
            current_vector_intervals(render_record("Pivot[0.5,0.5] Pivot[0.5,0.5]"), "Pivot")

    def test_normalization_preserves_node_raw_fields_and_native_precise_output(self):
        args = rounded_scale_fixture()
        before = copy.deepcopy(args)
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        leaf = result["children"][0]["children"][0]
        original = before[0]["children"][0]["children"][0]
        self.assertEqual(leaf["attributes"], original["attributes"])
        self.assertEqual(leaf["source_attributes"], original["source_attributes"])
        self.assertEqual(leaf["raw"], original["raw"])
        self.assertEqual(leaf["geometry"]["native_window_rect"], "[13.72,9.99],[2576.28,40.02]")
        self.assertAlmostEqual(leaf["geometry"]["screen_bounds"]["width"], 2562.56, places=10)
        self.assertAlmostEqual(leaf["geometry"]["screen_bounds"]["x"], 113.72, places=10)
        self.assertIsInstance(leaf["geometry"]["screen_bounds"]["width"], float)
        self.assertEqual(result["coordinate_validation"]["frame_nodes"], 3)
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 3)
        self.assertEqual(args, before)

    def test_native_conflict_still_rejects_node_and_preserves_failed_branch(self):
        args = rounded_scale_fixture()
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[13.72,9.99],[2577.28,40.02]"
        with self.assertLogs(level="ERROR"):
            result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        leaf = result["children"][0]["children"][0]
        self.assertEqual(leaf["attributes"]["ID"], "2")
        self.assertEqual(leaf["geometry"]["status"], "failed")
        self.assertEqual(leaf["geometry"]["reason"], "native_geometry_local_projection_mismatch")

    def test_raw_geometry_conflict_is_not_hidden_by_coarse_current_scale(self):
        args = rounded_scale_fixture()
        args[0]["children"][0]["children"][0]["attributes"]["scale(x,y)"] = "1.020000,1.020000"
        args[3]["render_tree"] = args[3]["render_tree"].replace("1.001000", "1.020000")
        with self.assertLogs(level="ERROR"):
            result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        self.assertIn("native_geometry_local_projection_mismatch",
                      [issue["code"] for issue in result["coordinate_validation"]["issues"]])

    def test_render_parent_conflict_is_not_hidden_by_coarse_current_scale(self):
        args = rounded_scale_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "          | CANVAS_NODE[102]", "        | CANVAS_NODE[102]")
        with self.assertLogs(level="ERROR"):
            result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        self.assertIn("empty_render_subtree_parent_mismatch:102",
                      [issue["code"] for issue in result["coordinate_validation"]["issues"]])


if __name__ == "__main__":
    unittest.main()
