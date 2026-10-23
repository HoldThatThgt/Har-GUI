"""@brief 使用二维仿射矩阵合成局部变换并计算四角包围框。"""

import logging
import math


IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _finite(values: tuple) -> tuple:
    """@brief 验证输入和运算结果均为有限数值。 @param values 数值序列。 @return 浮点数序列。"""
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as error:
        logging.error("Affine values must be finite numbers")
        raise ValueError("invalid_affine_number") from error
    if not all(math.isfinite(value) for value in result):
        logging.error("Affine values must be finite numbers")
        raise ValueError("non_finite_affine_number")
    return result


def _rotation(degrees: float) -> tuple:
    """@brief 对整直角使用精确三角值，其余角度保留实际三角值。 @param degrees 角度。 @return 余弦和正弦。"""
    angle = degrees % 360.0
    if angle % 90.0 == 0:
        return ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(angle / 90.0)]
    radians = math.radians(angle)
    return math.cos(radians), math.sin(radians)


def compose(outer: tuple, inner: tuple) -> tuple:
    """@brief 合成先应用内层再应用外层的变换。 @param outer 外层矩阵。 @param inner 内层矩阵。
    @return 按 a、b、c、d、e、f 排列的合成矩阵。
    """
    a, b, c, d, e, f = _finite(outer)
    g, h, i, j, k, l = _finite(inner)
    return _finite((a * g + c * h, b * g + d * h, a * i + c * j, b * i + d * j,
                    a * k + c * l + e, b * k + d * l + f))


def apply(matrix: tuple, x: float, y: float) -> tuple:
    """@brief 将矩阵应用于一个点。 @param matrix 仿射矩阵。 @param x 横坐标。 @param y 纵坐标。
    @return 变换后的横纵坐标。
    """
    a, b, c, d, e, f = _finite(matrix)
    x, y = _finite((x, y))
    return _finite((a * x + c * y + e, b * x + d * y + f))


def corners(matrix: tuple, rect: dict) -> tuple:
    """@brief 按起点、横向角、对角、纵向角的顺序变换四角，保留原始有符号尺寸。
    @param matrix 仿射矩阵。 @param rect 含 x、y、width、height 的矩形。 @return 四个点。
    """
    x, y, width, height = _finite((rect["x"], rect["y"], rect["width"], rect["height"]))
    points = ((x, y), (x + width, y), (x + width, y + height), (x, y + height))
    return tuple(apply(matrix, px, py) for px, py in points)


def bounds(matrix: tuple, rect: dict) -> dict:
    """@brief 仅在变换四角后求非负尺寸的轴对齐包围框。 @param matrix 仿射矩阵。
    @param rect 含 x、y、width、height 的矩形。 @return 同字段的包围框。
    """
    xs, ys = zip(*corners(matrix, rect))
    x, y, width, height = _finite((min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
    return {"x": x, "y": y, "width": width, "height": height}


def local(rect: dict, scale: tuple, translate: tuple, pivot: tuple, rotation_degrees: float) -> tuple:
    """@brief 按有符号尺寸确定轴心，依次应用轴心偏移、缩放、旋转及布局平移。
    @param rect 局部布局矩形。 @param scale 两轴比例。 @param translate 两轴平移。
    @param pivot 两轴相对轴心。 @param rotation_degrees 平面旋转角度。 @return 局部内容到父坐标的矩阵。
    """
    x, y, width, height = _finite((rect["x"], rect["y"], rect["width"], rect["height"]))
    sx, sy = _finite(scale)
    tx, ty = _finite(translate)
    px, py = _finite(pivot)
    rotation_degrees, = _finite((rotation_degrees,))
    cosine, sine = _rotation(rotation_degrees)
    a, b, c, d = cosine * sx, sine * sx, -sine * sy, cosine * sy
    px, py = width * px, height * py
    return _finite((a, b, c, d, x + px + tx - a * px - c * py, y + py + ty - b * px - d * py))
