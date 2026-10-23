"""@brief 验证仿射矩阵的合成顺序、旋转、反射及有符号尺寸。"""

import math
import unittest

from har_capture.affine import IDENTITY, apply, bounds, compose, corners, local


class AffineTests(unittest.TestCase):
    def test_identity_preserves_points_and_composition(self):
        matrix = (2, 3, -4, 5, 6, -7)
        self.assertEqual(apply(IDENTITY, -2, 8), (-2, 8))
        self.assertEqual(compose(IDENTITY, matrix), matrix)
        self.assertEqual(compose(matrix, IDENTITY), matrix)

    def test_composition_applies_inner_before_outer(self):
        translate = (1, 0, 0, 1, 10, 20)
        rotate = (0, 1, -1, 0, 0, 0)
        self.assertEqual(apply(compose(translate, rotate), 2, 3), (7, 22))
        self.assertEqual(apply(compose(rotate, translate), 2, 3), (-23, 12))

    def test_nested_nonuniform_scale_and_child_rotation_preserve_corner_order(self):
        parent = local({"x": 10, "y": 20, "width": 100, "height": 80}, (2, 3), (0, 0), (0, 0), 90)
        child = local({"x": 4, "y": 5, "width": 6, "height": 8}, (0.5, 2), (1, -1), (0.5, 0.25), 90)
        content = {"x": 0, "y": 0, "width": 6, "height": 8}
        matrix = compose(parent, child)
        self.assertEqual(corners(matrix, content), ((-3.5, 44), (-12.5, 44), (-12.5, 12), (-3.5, 12)))
        self.assertEqual(bounds(matrix, content), {"x": -12.5, "y": 12, "width": 9, "height": 32})

    def test_arbitrary_angle_uses_actual_trigonometry(self):
        matrix = local({"x": 10, "y": 20, "width": 4, "height": 2}, (1, 1), (0, 0), (0, 0), 30)
        point = apply(matrix, 4, 0)
        result = bounds(matrix, {"x": 0, "y": 0, "width": 4, "height": 2})
        self.assertAlmostEqual(point[0], 10 + 2 * math.sqrt(3))
        self.assertAlmostEqual(point[1], 22)
        self.assertAlmostEqual(result["x"], 9)
        self.assertAlmostEqual(result["y"], 20)
        self.assertAlmostEqual(result["width"], 1 + 2 * math.sqrt(3))
        self.assertAlmostEqual(result["height"], 2 + math.sqrt(3))

    def test_exact_quarter_turns_have_exact_matrix_coefficients(self):
        expected = {0: (1, 0, 0, 1), 90: (0, 1, -1, 0), 180: (-1, 0, 0, -1), 270: (0, -1, 1, 0)}
        rect = {"x": 0, "y": 0, "width": 7, "height": 3}
        for degrees in (-450, -360, -270, -180, -90, 0, 90, 180, 270, 360, 450):
            with self.subTest(degrees=degrees):
                matrix = local(rect, (1, 1), (0, 0), (0, 0), degrees)
                self.assertEqual(matrix, expected[degrees % 360] + (0, 0))

    def test_angle_near_quarter_turn_is_not_snapped(self):
        rect = {"x": 0, "y": 0, "width": 1, "height": 1}
        matrix = local(rect, (1, 1), (0, 0), (0, 0), 90.0000001)
        self.assertLess(matrix[0], 0)
        self.assertNotEqual(matrix[0], 0)

    def test_pivot_and_translation_follow_scaled_content(self):
        matrix = local({"x": 10, "y": 20, "width": 8, "height": 4}, (2, 0.5), (3, -2), (0.25, 0.75), 0)
        self.assertEqual(apply(matrix, 2, 3), (15, 21))
        self.assertEqual(bounds(matrix, {"x": 0, "y": 0, "width": 8, "height": 4}),
                         {"x": 11, "y": 19.5, "width": 16, "height": 2})

    def test_reflection_preserves_corner_direction_before_bounds(self):
        matrix = local({"x": 10, "y": 20, "width": 8, "height": 4}, (-2, 0.5), (3, -2), (0.25, 0.75), 0)
        rect = {"x": 0, "y": 0, "width": 8, "height": 4}
        self.assertEqual(corners(matrix, rect), ((19, 19.5), (3, 19.5), (3, 21.5), (19, 21.5)))
        self.assertEqual(bounds(matrix, rect), {"x": 3, "y": 19.5, "width": 16, "height": 2})

    def test_mixed_negative_sentinel_is_used_before_pivot_and_rotation(self):
        rect = {"x": 10, "y": 20, "width": -1, "height": 8}
        matrix = local(rect, (-2, 3), (2, -1), (0.5, 0.25), 90)
        content = {"x": 0, "y": 0, "width": -1, "height": 8}
        self.assertEqual(corners(matrix, content), ((17.5, 20), (17.5, 22), (-6.5, 22), (-6.5, 20)))
        self.assertEqual(bounds(matrix, content), {"x": -6.5, "y": 20, "width": 24, "height": 2})
        self.assertEqual(rect, {"x": 10, "y": 20, "width": -1, "height": 8})

    def test_signed_rect_corner_order_is_preserved(self):
        rect = {"x": 3, "y": 5, "width": -1, "height": 2}
        self.assertEqual(corners(IDENTITY, rect), ((3, 5), (2, 5), (2, 7), (3, 7)))
        self.assertEqual(bounds(IDENTITY, rect), {"x": 2, "y": 5, "width": 1, "height": 2})

    def test_nonfinite_inputs_are_logged_and_rejected(self):
        rect = {"x": 0, "y": 0, "width": 2, "height": 3}
        calls = (
            lambda: compose((math.inf, 0, 0, 1, 0, 0), IDENTITY),
            lambda: apply(IDENTITY, math.nan, 0),
            lambda: corners(IDENTITY, {**rect, "height": -math.inf}),
            lambda: bounds((1, 0, 0, 1, math.inf, 0), rect),
            lambda: local(rect, (1, 1), (0, 0), (0.5, 0.5), math.nan),
        )
        for call in calls:
            with self.subTest(call=call), self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(ValueError, "non_finite_affine_number"):
                    call()

    def test_finite_inputs_with_overflowing_results_are_logged_and_rejected(self):
        calls = (
            lambda: compose((1e308, 0, 0, 1, 0, 0), (1e308, 0, 0, 1, 0, 0)),
            lambda: apply((1e308, 0, 0, 1, 0, 0), 1e308, 0),
            lambda: bounds((2, 0, 0, 1, 0, 0), {"x": -5e307, "y": 0, "width": 1e308, "height": 1}),
        )
        for call in calls:
            with self.subTest(call=call), self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(ValueError, "non_finite_affine_number"):
                    call()


if __name__ == "__main__":
    unittest.main()
