"""@brief 用确定性采样证明区间矩阵包含精确变换及其角点，覆盖反射和旋转极值。"""

import math
import random
import unittest

from har_capture import affine


def sample_intervals(intervals, generator, sample):
    """@brief 同时采样上下端点、中点及固定种子的内部点。"""
    if sample == 0:
        return tuple(low for low, high in intervals)
    if sample == 1:
        return tuple(high for low, high in intervals)
    if sample == 2:
        return tuple((low + high) / 2 for low, high in intervals)
    return tuple(generator.uniform(low, high) for low, high in intervals)


def exact_local(parameters):
    """@brief 用既有精确仿射接口计算同一组具体参数，避免重写矩阵公式。"""
    rect, scale, translate, pivot, rotation = parameters
    bounds = dict(zip(("x", "y", "width", "height"), rect, strict=True))
    return affine.local(bounds, scale, translate, pivot, rotation[0])


def transform_ranges(rotation):
    """@brief 提供非对称布局、反射、平移和轴心范围，保持各参数互相独立。"""
    return (((-8.5, 9.25), (-20.2, -17.5), (3.25, 2560.5), (-1.25, 81.75)),
            ((-3.25, -0.5), (0.25, 2.75)), ((-12.5, 17.75), (-22.25, 5.5)),
            ((-0.25, 0.75), (0.125, 1.25)), (rotation,))


def bounded_local(parameters):
    """@brief 将角度区间交给生产区间接口，不提前代入采样值。"""
    rect, scale, translate, pivot, rotation = parameters
    return affine.interval_local(rect, scale, translate, pivot, rotation[0])


class AffineIntervalTests(unittest.TestCase):
    def assert_contains(self, intervals, values):
        for index, (interval, value) in enumerate(zip(intervals, values, strict=True)):
            self.assertLessEqual(interval[0], value, (index, interval, value))
            self.assertGreaterEqual(interval[1], value, (index, interval, value))

    def test_exact_matrix_wrapper_preserves_values_without_print_uncertainty(self):
        matrix = (1.001, -2.5, 0.125, 3.75, -1280.001, 512.25)
        self.assertEqual(affine.interval_exact(matrix), tuple((value, value) for value in matrix))
        self.assertEqual(affine.interval_exact(affine.IDENTITY),
                         ((1, 1), (0, 0), (0, 0), (1, 1), (0, 0), (0, 0)))

    def test_local_contains_deterministic_samples_across_rotation_extrema(self):
        rotations = ((89.95, 90.05), (179.95, 180.05), (359.95, 360.05), (-90.05, -89.95),
                     (-0.000001, 0.000001), (-450.1, -449.9), (12, 389), (-720, 720))
        generator = random.Random(20261023)
        for rotation in rotations:
            parameters = transform_ranges(rotation)
            matrix = bounded_local(parameters)
            for sample in range(320):
                sampled = tuple(sample_intervals(group, generator, sample) for group in parameters)
                with self.subTest(rotation=rotation, sample=sample):
                    self.assert_contains(matrix, exact_local(sampled))

    def test_local_contains_exact_cardinal_angles_with_reflected_and_signed_sizes(self):
        generator = random.Random(139)
        for angle in (-720, -450, -360, -180, -90, 0, 90, 180, 270, 360, 450, 720):
            parameters = transform_ranges((angle - 0.01, angle + 0.01))
            matrix = bounded_local(parameters)
            for sample in range(40):
                sampled = tuple(sample_intervals(group, generator, sample) for group in parameters[:-1])
                with self.subTest(angle=angle, sample=sample):
                    self.assert_contains(matrix, exact_local((*sampled, (angle,))))

    def test_zero_width_parameter_intervals_contain_exact_arithmetic(self):
        generator = random.Random(256256)
        for sample in range(600):
            values = (tuple(generator.uniform(-3000, 3000) for _ in range(4)),
                      tuple(generator.uniform(-4, 4) for _ in range(2)),
                      tuple(generator.uniform(-100, 100) for _ in range(2)),
                      tuple(generator.uniform(-1, 2) for _ in range(2)),
                      (generator.choice((0, 90, 180, 360, 1e-9, -1e-9, 45.123)),))
            parameters = tuple(tuple((value, value) for value in group) for group in values)
            with self.subTest(sample=sample):
                self.assert_contains(bounded_local(parameters), exact_local(values))

    def test_composition_and_corners_contain_nested_sampled_transforms(self):
        generator = random.Random(1392560)
        parameters = [transform_ranges(rotation) for rotation in
                      ((89.95, 90.05), (179.9, 180.1), (359.9, 360.1), (-1e-6, 1e-6))]
        interval_matrix = affine.interval_exact((1.5, 0, 0, 0.75, 100, -50))
        for transform in parameters:
            interval_matrix = affine.interval_compose(interval_matrix, bounded_local(transform))
        points = affine.interval_corners(interval_matrix, parameters[-1][0][2:])
        for sample in range(600):
            exact_matrix = (1.5, 0, 0, 0.75, 100, -50)
            for transform in parameters:
                sampled = tuple(sample_intervals(group, generator, sample) for group in transform)
                exact_matrix = affine.compose(exact_matrix, exact_local(sampled))
            rect = {"x": 0, "y": 0, "width": sampled[0][2], "height": sampled[0][3]}
            with self.subTest(sample=sample):
                self.assert_contains(interval_matrix, exact_matrix)
                for interval_point, exact_point in zip(points, affine.corners(exact_matrix, rect), strict=True):
                    self.assert_contains(interval_point, exact_point)

    def test_compose_contains_independent_matrix_and_size_samples(self):
        generator = random.Random(4242)
        for case in range(100):
            matrices = []
            for _ in range(2):
                centers = [generator.uniform(-100, 100) for _ in range(6)]
                matrices.append(tuple((value - 0.005, value + 0.005) for value in centers))
            size = ((-1.1, -0.9), (2559.95, 2560.05))
            combined = affine.interval_compose(*matrices)
            points = affine.interval_corners(combined, size)
            for sample in range(16):
                exact = affine.compose(*(sample_intervals(matrix, generator, sample) for matrix in matrices))
                width, height = sample_intervals(size, generator, sample)
                rect = {"x": 0, "y": 0, "width": width, "height": height}
                with self.subTest(case=case, sample=sample):
                    self.assert_contains(combined, exact)
                    for interval_point, exact_point in zip(points, affine.corners(exact, rect), strict=True):
                        self.assert_contains(interval_point, exact_point)

    def test_invalid_interval_endpoints_are_logged_and_rejected(self):
        matrix = affine.interval_exact(affine.IDENTITY)
        invalid = ((2, 1), (math.nan, 1), (0, math.inf), (-math.inf, 0))
        for interval in invalid:
            calls = (lambda: affine.interval_compose((interval, *matrix[1:]), matrix),
                     lambda: affine.interval_corners(matrix, (interval, (1, 1))),
                     lambda: affine.interval_local(((0, 0),) * 4, ((1, 1),) * 2,
                                                   ((0, 0),) * 2, ((0.5, 0.5),) * 2, interval))
            for call in calls:
                with self.subTest(interval=interval), self.assertLogs(level="ERROR"), self.assertRaises(ValueError):
                    call()


if __name__ == "__main__":
    unittest.main()
