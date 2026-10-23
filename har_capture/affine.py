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
        return ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(angle / 90.0) % 4]
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


def _interval(value: tuple) -> tuple:
    """@brief 验证有限且有序的数值闭区间。 @param value 上下界。 @return 已核验区间。"""
    result = _finite(value)
    if len(result) != 2 or result[0] > result[1]:
        logging.error("Affine interval must have ordered finite endpoints")
        raise ValueError("invalid_affine_interval")
    return result


def _interval_sum(values: tuple) -> tuple:
    """@brief 对区间求和并向外取整，保留浮点运算误差。 @param values 区间序列。 @return 和区间。"""
    low = sum(value[0] for value in values)
    high = sum(value[1] for value in values)
    return _finite((math.nextafter(low, -math.inf), math.nextafter(high, math.inf)))


def _interval_product(first: tuple, second: tuple) -> tuple:
    """@brief 用全部端点组合求乘积区间并向外取整。 @param first 首区间。 @param second 次区间。
    @return 乘积区间。
    """
    values = _finite(tuple(a * b for a in first for b in second))
    return _finite((math.nextafter(min(values), -math.inf), math.nextafter(max(values), math.inf)))


def _interval_negative(value: tuple) -> tuple:
    """@brief 对区间取相反数。 @param value 已验证区间。 @return 相反数区间。"""
    return -value[1], -value[0]


def interval_exact(matrix: tuple) -> tuple:
    """@brief 将已核验精确矩阵包装成零宽区间。 @param matrix 六分量矩阵。 @return 区间矩阵。"""
    return tuple((value, value) for value in _finite(matrix))


def interval_compose(outer: tuple, inner: tuple) -> tuple:
    """@brief 按同一仿射乘法传播各字段的打印区间。 @param outer 外层矩阵。 @param inner 内层矩阵。
    @return 包含全部相容变换的区间矩阵。
    """
    a, b, c, d, e, f = tuple(_interval(value) for value in outer)
    g, h, i, j, k, l = tuple(_interval(value) for value in inner)
    product, total = _interval_product, _interval_sum
    return (total((product(a, g), product(c, h))), total((product(b, g), product(d, h))),
            total((product(a, i), product(c, j))), total((product(b, i), product(d, j))),
            total((product(a, k), product(c, l), e)), total((product(b, k), product(d, l), f)))


def _rotation_intervals(degrees: tuple) -> tuple:
    """@brief 由端点及区间内三角函数极值求完整旋转范围。 @param degrees 角度区间。
    @return 余弦及正弦区间。
    """
    low, high = _interval(degrees)
    if high - low >= 360:
        return (-1.0, 1.0), (-1.0, 1.0)
    angles = [low, high, *(index * 90 for index in range(math.ceil(low / 90), math.floor(high / 90) + 1))]
    cosines, sines = zip(*(_rotation(angle) for angle in angles))
    return ((max(-1.0, math.nextafter(min(cosines), -math.inf)),
             min(1.0, math.nextafter(max(cosines), math.inf))),
            (max(-1.0, math.nextafter(min(sines), -math.inf)),
             min(1.0, math.nextafter(max(sines), math.inf))))


def interval_local(rect: tuple, scale: tuple, translate: tuple, pivot: tuple, rotation: tuple) -> tuple:
    """@brief 按官方字段打印区间构造局部变换，不用精确原文或修饰器替代当前属性。
    @param rect 局部框区间。 @param scale 两轴比例区间。 @param translate 两轴位移区间。
    @param pivot 两轴相对轴心区间。 @param rotation 平面旋转角区间。 @return 当前局部区间矩阵。
    """
    x, y, width, height = tuple(_interval(value) for value in rect)
    sx, sy = tuple(_interval(value) for value in scale)
    tx, ty = tuple(_interval(value) for value in translate)
    px, py = tuple(_interval(value) for value in pivot)
    cosine, sine = _rotation_intervals(rotation)
    product, total, negative = _interval_product, _interval_sum, _interval_negative
    a, b, c, d = product(cosine, sx), product(sine, sx), negative(product(sine, sy)), product(cosine, sy)
    px, py = product(width, px), product(height, py)
    one_minus_a, one_minus_d = total(((1.0, 1.0), negative(a))), total(((1.0, 1.0), negative(d)))
    return (a, b, c, d, total((x, tx, product(one_minus_a, px), negative(product(c, py)))),
            total((y, ty, negative(product(b, px)), product(one_minus_d, py))))


def interval_corners(matrix: tuple, size: tuple) -> tuple:
    """@brief 将当前局部宽高的打印区间投影到四个屏幕角点区间。 @param matrix 当前区间矩阵。
    @param size 宽高区间。 @return 四个角点的横纵区间。
    """
    a, b, c, d, e, f = tuple(_interval(value) for value in matrix)
    width, height = tuple(_interval(value) for value in size)
    zero = (0.0, 0.0)
    product, total = _interval_product, _interval_sum
    return tuple((total((product(a, x), product(c, y), e)), total((product(b, x), product(d, y), f)))
                 for x, y in ((zero, zero), (width, zero), (width, height), (zero, height)))
