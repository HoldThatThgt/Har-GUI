"""@brief 关联原生 ArkUI 已变换矩形与渲染树，验证整屏坐标和裁剪来源。"""

import copy
import logging
import math
import re

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
RECT = re.compile(rf"^\[({NUMBER}),\s*({NUMBER})\],\[({NUMBER}),\s*({NUMBER})\]$")
LOCAL_RECT = re.compile(rf"^RectT \(({NUMBER}),\s*({NUMBER})\) - \[({NUMBER}) x ({NUMBER})\]$")
RENDER_NODE = re.compile(r"^( *)\| (\w+_NODE)\[(\d+)\]")
CLIENT_NODE = re.compile(r"^\s*(?:\|\s*)?(\w+Node)\[(\d+)\].*$", re.MULTILINE)
NON_LAYOUT = {"JsView", "IfElse", "ForEach", "LazyForEach", "Repeat", "SyntaxItem", "NodeSlot", "Span", "__Recycle__"}
UNSUPPORTED = re.compile(r"Skew|Persp|Matrix|ClipBounds|CustomClip|ClipRRect|Mask|SDF|Attraction|Spherize")
ZERO_ONLY = {"Rotation", "RotationX", "RotationY", "TranslateZ", "PivotZ", "CameraDistance"}
SOURCES = {
    "arkui": "https://github.com/openharmony/arkui_ace_engine/blob/"
             "21b150e7bb1d5308846f8e5be8ef0482fb433315/frameworks/core/components_ng/base/frame_node.cpp",
    "render": "https://github.com/openharmony/graphic_graphic_2d/blob/"
              "c19cedf62fefde6bf184aa815feeaf0346aa4d49/"
              "rosen/modules/render_service_base/src/pipeline/rs_render_node.cpp",
    "arkui_traversal": "https://github.com/openharmony/arkui_ace_engine/blob/"
                       "21b150e7bb1d5308846f8e5be8ef0482fb433315/"
                       "frameworks/core/components_ng/base/ui_node.cpp",
    "arkui_transform": "https://github.com/openharmony/arkui_ace_engine/blob/"
                       "21b150e7bb1d5308846f8e5be8ef0482fb433315/"
                       "frameworks/core/components_ng/render/adapter/rosen_render_context.cpp",
    "arkui_recycle": "https://github.com/openharmony/arkui_ace_engine/blob/"
                     "21b150e7bb1d5308846f8e5be8ef0482fb433315/"
                     "frameworks/core/components_ng/pattern/recycle_view/recycle_dummy_node.h",
}


def number(value: str) -> float:
    """@brief 验证有限数值。 @param value 源值。 @return 浮点数。"""
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non_finite_geometry")
    return result


def native_rect(value: str) -> dict:
    """@brief 解析原生 ToBounds 矩形。 @param value 官方矩形字符串。 @return 像素矩形。"""
    match = RECT.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("invalid_native_geometry_rect")
    left, top, right, bottom = map(number, match.groups())
    if right < left or bottom < top:
        raise ValueError("negative_native_geometry_size")
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def node_key(node_id: str, tag: str, parent_id: str | None) -> str:
    """@brief 为官方默认负标识回收容器建立父域身份。 @param node_id 原生标识。
    @param tag 原生类型。 @param parent_id 父身份。 @return 唯一关联键。
    """
    if node_id == "-1" and tag == "__Recycle__":
        return str(parent_id) + "/__Recycle__"
    return node_id


def subtree_index(root: dict) -> tuple[dict, dict]:
    """@brief 建立原生节点及父节点索引。 @param root 原生树。 @return 两个索引。"""
    nodes, parents = {}, {}
    pending = [(root, None)]
    while pending:
        node, parent_id = pending.pop()
        node_id = node_key(str(node["$ID"]), node["$type"], parent_id)
        if node_id in nodes:
            raise ValueError("duplicate_native_geometry_id")
        nodes[node_id], parents[node_id] = node, parent_id
        pending.extend((child, node_id) for child in node.get("$children", []))
    return nodes, parents


def select_native_root(raw: dict, source: dict) -> dict:
    """@brief 精确关联应用根或系统会话根。 @param raw 原始树。 @param source 原生几何树。 @return 匹配根。"""
    nodes, _ = subtree_index(source)
    attrs = raw["attributes"]
    node = nodes.get(str(attrs.get("ID")))
    if node is None or node.get("$type") != attrs["type"].split(",", 1)[0]:
        raise ValueError("native_geometry_root_identity_mismatch")
    if attrs.get("compid") and attrs["compid"] != node.get("compid"):
        raise ValueError("native_geometry_root_component_mismatch")
    return node


def render_index(text: str) -> dict:
    """@brief 解析带完整修饰器证据的正式渲染树。 @param text RSTree 响应。 @return 渲染节点索引。"""
    nodes, stack = {}, []
    if "WARNING: exceed max depth" in text:
        raise ValueError("truncated_render_tree")
    for line in text.splitlines():
        match = RENDER_NODE.match(line)
        if match is None:
            continue
        depth, kind, node_id = len(match[1]), match[2], match[3]
        while stack and depth <= stack[-1][0]:
            stack.pop()
        if node_id in nodes:
            raise ValueError("duplicate_render_node_id")
        nodes[node_id] = {"id": node_id, "kind": kind, "line": line,
                          "parent": stack[-1][1] if stack else None}
        stack.append((depth, node_id))
    if not nodes:
        raise ValueError("missing_render_tree")
    return nodes


def detached_render_index(text: str) -> dict:
    """@brief 仅从官方未挂载段读取独立证据，允许明确空集合。 @param text 独立响应。
    @return 未挂载节点索引。
    """
    marker = "-- Render Node Not On Tree"
    if marker not in text:
        raise ValueError("missing_detached_render_tree_section")
    section = text.split(marker, 1)[1]
    if not any(RENDER_NODE.match(line) for line in section.splitlines()):
        if "WARNING: exceed max depth" in section:
            raise ValueError("truncated_detached_render_tree")
        return {}
    return render_index(section)


def client_render(node: dict) -> dict:
    """@brief 提取 ArkUI 自身渲染节点标识。 @param node 原始组件。 @return 渲染关联和优化状态。"""
    rows = list(CLIENT_NODE.finditer(node["raw"]))
    if len(rows) != 1:
        raise ValueError("missing_or_ambiguous_client_render_node")
    row = rows[0]
    return {"id": row[2], "lazy": "lazyLoad[1]" in row[0] and "nodeState[LAZY_LOAD]" in row[0]}


def modifier_values(line: str) -> dict:
    """@brief 提取完整渲染修饰器列表。 @param line 渲染节点原行。 @return 名称到值列表。"""
    marker = "OtherModifiers:["
    if "Properties: Bounds[" not in line or " Frame[" not in line:
        raise ValueError("render_modifier_evidence_missing")
    if marker not in line:
        return {}
    content = line.split(marker, 1)[1].split("], sortedChildren:", 1)[0]
    values = {}
    for name, value in re.findall(r"(\w+):\[([^\]]*)\]", content):
        values.setdefault(name, []).append(value)
    return values


def verify_modifiers(render: dict) -> dict:
    """@brief 拒绝已存在的非轴对齐或未知裁剪变换。 @param render 渲染节点。 @return 已验证修饰器。"""
    values = modifier_values(render["line"])
    for key, entries in values.items():
        if key in {"AttractionFraction", "AttractionDstpoint"}:
            fraction = values.get("AttractionFraction", ["0"])
            if all(number(value) == 0 for value in fraction):
                continue
        allowed_masks = {"ShadowMask", "BackgroundBlurMaskColor", "BehindWindowFilterMaskColor",
                         "ForegroundBlurMaskColor"}
        if UNSUPPORTED.search(key) and key not in allowed_masks and key != "CustomClipToFrame":
            raise ValueError("unsupported_render_modifier:" + key)
        if key in ZERO_ONLY and any(abs(number(value)) > 0.000001 for value in entries):
            raise ValueError("unsupported_render_transform:" + key)
    if "animationInfo:" in render["line"] or re.search(r"\banimation:[1-9]", render["line"]):
        raise ValueError("active_render_animation")
    return values


def transform_context(root: dict, window: dict, canvas: dict) -> dict:
    """@brief 根据已验证根和 WMS 建立整屏映射。 @param root 原生根。 @param window 窗口。
    @param canvas 画布。 @return 映射参数。
    """
    rect = native_rect(root["$rect"])
    box, transform = window["bounding_box"], window["transform"]
    if any(abs(rect[key] - box[key]) > 0.02 for key in ("width", "height")):
        raise ValueError("native_root_window_size_mismatch")
    if window.get("orientation", 0) != 0:
        raise ValueError("unsupported_window_orientation")
    if min(canvas["width"], canvas["height"], rect["width"], rect["height"]) <= 0:
        raise ValueError("invalid_canvas_or_window_size")
    values = {key: number(str(transform[key])) for key in
              ("offset_x", "offset_y", "scale_x", "scale_y", "pivot_x", "pivot_y")}
    if min(values["scale_x"], values["scale_y"]) <= 0:
        raise ValueError("unsupported_window_scale")
    return {"root": rect, "box": box, "transform": values, "canvas": canvas}


def screen_rect(rect: dict, mapping: dict) -> dict:
    """@brief 只对原生窗口矩形应用一次 WMS 变换。 @param rect 原生框。 @param mapping 映射。 @return 整屏框。"""
    result = {}
    root, box, transform = mapping["root"], mapping["box"], mapping["transform"]
    for axis, dimension in (("x", "width"), ("y", "height")):
        scale = transform["scale_" + axis]
        pivot = transform["pivot_" + axis] * box[dimension]
        result[axis] = box[axis] + transform["offset_" + axis] + pivot * (1 - scale)
        result[axis] += (rect[axis] - root[axis]) * scale
        result[dimension] = rect[dimension] * scale
    return result


def local_projection(node: dict, parent: tuple) -> tuple:
    """@brief 按官方局部绘制框合成轴变换及四角包围框。 @param node 原始节点。
    @param parent 父变换及误差界。 @return 当前变换、误差界及窗口矩形。
    """
    attrs = node["attributes"]
    match = LOCAL_RECT.fullmatch(attrs["PaintRect without transform"])
    if match is None:
        raise ValueError("invalid_local_paint_rect")
    x, y, width, height = map(number, match.groups())
    scale = list(map(number, attrs.get("scale(x,y)", "1,1").split(",")))
    translate = list(map(number, attrs.get("translate(x,y)", "0,0").split(",")))
    pivot = list(map(number, re.findall(NUMBER, attrs.get("Center", "x:0.5 y:0.5"))))
    if len(scale) != 2 or len(translate) != 2 or len(pivot) != 2 or min(scale) <= 0:
        raise ValueError("invalid_local_axis_transform")
    px, py, psx, psy, error = parent
    tx = px + psx * (x + width * pivot[0] * (1 - scale[0]) + translate[0])
    ty = py + psy * (y + height * pivot[1] * (1 - scale[1]) + translate[1])
    sx, sy = psx * scale[0], psy * scale[1]
    tolerance = error + 0.011 * max(abs(psx), abs(psy), abs(sx), abs(sy), 1)
    rect = {"x": min(tx, tx + width * sx), "y": min(ty, ty + height * sy),
            "width": abs(width * sx), "height": abs(height * sy)}
    return (tx, ty, sx, sy, tolerance), rect


def verify_native_projection(node: dict, context: dict, parent: tuple) -> tuple:
    """@brief 用独立父链验证原生窗口矩形没有跨时刻漂移。 @param node 节点。 @param context 上下文。
    @param parent 父变换。 @return 当前变换。
    """
    transform, expected = local_projection(node, parent)
    native = context["native"][str(node["attributes"].get("ID"))]
    actual = native_rect(native["$rect"])
    if any(abs(actual[key] - expected[key]) > transform[4] + 0.011 for key in actual):
        raise ValueError("native_geometry_local_projection_mismatch")
    return transform


def intersection(first: dict, second: dict) -> dict:
    """@brief 求两个轴对齐矩形交集。 @param first 第一框。 @param second 第二框。 @return 交集框。"""
    x, y = max(first["x"], second["x"]), max(first["y"], second["y"])
    right = min(first["x"] + first["width"], second["x"] + second["width"])
    bottom = min(first["y"] + first["height"], second["y"] + second["height"])
    return {"x": x, "y": y, "width": max(0, right - x), "height": max(0, bottom - y)}


def source_nodes(raw: dict, native: dict, mapping: dict) -> dict:
    """@brief 建立 ArkUI 到渲染节点的几何索引。 @param raw 原始根。 @param native 原生索引。
    @param mapping 屏幕映射。 @return 节点关联、原生窗口投影和渲染矩形。
    """
    links, rectangles, projections, overlays = {}, {}, {}, set()
    pending = [(raw, (0, 0, 1, 1, 0), False, None)]
    while pending:
        node, projection, overlay, parent_id = pending.pop()
        attrs = node["attributes"]
        node_id = str(attrs.get("ID"))
        overlay = overlay or attrs.get("Description") == "OverlayNode"
        if overlay and "FrameRect" in attrs and attrs.get("LastParentId") != parent_id:
            raise ValueError("overlay_parent_evidence_mismatch:" + node_id)
        if overlay and "FrameRect" not in attrs and node_id not in native:
            # 官方结构节点不输出 LastParentId，Children 已核对其当前 GetParent()。
            if attrs.get("Description") != "Children":
                raise ValueError("overlay_non_layout_parent_unverified:" + node_id)
            overlays.add(node_id)
        if "FrameRect" in attrs:
            projection, rect = local_projection(node, projection)
            if node_id not in native or "$rect" not in native[node_id]:
                if not overlay:
                    raise ValueError("native_geometry_frame_missing:" + node_id)
                overlays.add(node_id)
            projections[node_id] = rect
        pending.extend((child, projection, overlay, node_id) for child in node["children"])
        if "FrameRect" not in attrs:
            continue
        link = client_render(node)
        links[node_id] = link
        if link["id"] in rectangles:
            raise ValueError("ambiguous_render_geometry_mapping")
        rectangles[link["id"]] = screen_rect(projections[node_id], mapping)
    native_frames = {node_id for node_id, node in native.items() if "$rect" in node}
    if native_frames != set(links) - overlays:
        raise ValueError("native_geometry_frame_set_mismatch")
    return {"links": links, "rectangles": rectangles, "projections": projections, "overlays": overlays}


def render_ancestors(node_id: str, nodes: dict) -> list[dict]:
    """@brief 返回无环的渲染祖先链。 @param node_id 起点。 @param nodes 渲染索引。 @return 链。"""
    result, seen = [], set()
    while node_id is not None:
        if node_id in seen or node_id not in nodes:
            raise ValueError("invalid_render_parent_chain")
        seen.add(node_id)
        node = nodes[node_id]
        result.append(node)
        node_id = node["parent"]
    return result


def render_clip(render: dict, values: dict, rectangles: dict) -> dict | None:
    """@brief 解析实际生效的矩形及圆角裁剪。 @param render 渲染节点。 @param values 修饰器。
    @param rectangles 已关联整屏矩形。 @return 裁剪记录或空。
    """
    bounds_clip = "ClipToBounds[True]" in render["line"]
    frame_clip = "ClipToFrame[True]" in render["line"]
    if not bounds_clip and not frame_clip:
        return None
    if render["id"] not in rectangles:
        raise ValueError("unmapped_render_clip_node:" + render["id"])
    if frame_clip:
        match = re.search(r"Bounds\[([^\]]+)\] Frame\[([^\]]+)\]", render["line"])
        if match is None or match[1] != match[2]:
            raise ValueError("unsupported_distinct_frame_clip")
    corner_values = values.get("CornerRadius", [])
    if len(set(corner_values)) > 1:
        raise ValueError("ambiguous_render_corner_radius")
    radii = list(map(number, re.findall(NUMBER, corner_values[0]))) if corner_values else [0.0] * 4
    if len(radii) != 4 or min(radii) < 0:
        raise ValueError("invalid_render_corner_radius")
    return {"source": "RenderService.RSTree", "render_node_id": render["id"],
            "kind": "rounded_rectangle" if any(radii) else "rectangle", "bounds": rectangles[render["id"]],
            "corner_radius_local": radii,
            "mode": "bounds" if bounds_clip else "frame"}


def content_clip(render: dict, values: dict, rectangles: dict) -> dict | None:
    """@brief 转换官方内容裁剪矩形，保留内容与子节点作用范围。 @param render 渲染节点。
    @param values 修饰器。 @param rectangles 整屏框。 @return 内容裁剪或空。
    """
    entries = values.get("CustomClipToFrame")
    if entries is None:
        return None
    if len(set(entries)) != 1 or render["id"] not in rectangles:
        raise ValueError("ambiguous_content_clip")
    local = list(map(number, re.findall(NUMBER, entries[0])))
    match = re.search(r"Bounds\[([^\]]+)\] Frame\[([^\]]+)\]", render["line"])
    if len(local) != 4 or match is None or match[1] != match[2]:
        raise ValueError("unsupported_content_clip_frame")
    bounds = list(map(number, match[1].split()))
    if min(bounds[2:]) <= 0 or local[2] < local[0] or local[3] < local[1]:
        raise ValueError("invalid_content_clip_rectangle")
    screen = rectangles[render["id"]]
    sx, sy = screen["width"] / bounds[2], screen["height"] / bounds[3]
    rect = {"x": screen["x"] + local[0] * sx, "y": screen["y"] + local[1] * sy,
            "width": (local[2] - local[0]) * sx, "height": (local[3] - local[1]) * sy}
    return {"source": "RenderService.RSTree.CustomClipToFrame", "render_node_id": render["id"],
            "kind": "rectangle", "bounds": rect, "local_bounds_ltrb": local,
            "mode": "content_and_children"}


def bind_window_render_clip(render: dict, context: dict) -> dict:
    """@brief 按窗口尺寸和原点校核外部 Surface 裁剪。 @param render 外部渲染祖先。
    @param context 本批上下文。 @return 包含该节点的屏幕矩形索引。
    """
    if not any(key in render["line"] for key in ("ClipToBounds[True]", "ClipToFrame[True]", "CustomClipToFrame")):
        return context["rectangles"]
    match = re.search(r"Properties: Bounds\[([^\]]+)\]", render["line"])
    values = list(map(number, match[1].split())) if match else []
    box = context["mapping"]["box"]
    if len(values) != 4 or any(abs(values[index + 2] - box[key]) > 0.11
                               for index, key in enumerate(("width", "height"))):
        raise ValueError("unmapped_outer_render_clip")
    if not all(abs(values[index] - box[key]) <= 0.11 for index, key in enumerate(("x", "y"))):
        if values[:2] != [0, 0]:
            raise ValueError("outer_render_clip_origin_mismatch")
    return {**context["rectangles"], render["id"]: context["window_clips"][1]["bounds"]}


def frame_clips(link: dict, context: dict, inherited: list) -> list:
    """@brief 使用真实渲染祖先链验证裁剪。 @param link 组件关联。 @param context 本批上下文。
    @param inherited 非独立渲染节点的祖先裁剪。 @return 裁剪链。
    """
    if link["id"] not in context["render"]:
        if link["lazy"]:
            return inherited
        raise ValueError("active_render_node_missing:" + link["id"])
    clips = list(context["window_clips"])
    inside_root = True
    ancestors = render_ancestors(link["id"], context["render"])
    if context["root_render_id"] not in {node["id"] for node in ancestors}:
        raise ValueError("render_node_window_mismatch")
    for render in ancestors:
        if render["kind"] == "RS_NODE":
            continue
        values = verify_modifiers(render)
        rectangles = context["rectangles"] if inside_root else bind_window_render_clip(render, context)
        clip = render_clip(render, values, rectangles)
        if clip:
            clips.append(clip)
        custom = content_clip(render, values, rectangles)
        if custom:
            clips.append(custom)
        if render["id"] == context["root_render_id"]:
            inside_root = False
    return clips


def render_vector(values: dict, name: str, default: tuple) -> list:
    """@brief 解析实际修饰器的轴向量。 @param values 修饰器。 @param name 属性名。
    @param default 官方默认向量。 @return 二维向量。
    """
    entries = values.get(name)
    if entries is None:
        return list(default)
    if len(set(entries)) != 1:
        raise ValueError("ambiguous_render_vector:" + name)
    result = list(map(number, re.findall(NUMBER, entries[0])))
    if len(result) != 2:
        raise ValueError("invalid_render_vector:" + name)
    return result


def render_bounds(render: dict) -> list:
    """@brief 读取真实渲染局部框。 @param render 渲染节点。 @return 局部四元组。"""
    match = re.search(r"Properties: Bounds\[([^\]]+)\]", render["line"])
    bounds = list(map(number, match[1].split())) if match else []
    if len(bounds) != 4 or min(bounds[2:]) < 0:
        raise ValueError("invalid_render_bounds")
    return bounds


def verify_overlay_render(node_id: str, context: dict) -> None:
    """@brief 用实际 RS 父子局部框独立校验官方省略的 Overlay。 @param node_id 原始节点标识。
    @param context 本批证据。
    """
    render_id = context["links"][node_id]["id"]
    node = context["render"].get(render_id)
    if node is None or node["parent"] not in context["rectangles"]:
        raise ValueError("overlay_render_parent_unavailable")
    parent = context["render"][node["parent"]]
    parent_bounds, bounds = render_bounds(parent), render_bounds(node)
    if min(parent_bounds[2:]) <= 0:
        raise ValueError("overlay_render_parent_empty")
    values = verify_modifiers(node)
    scale = render_vector(values, "Scale", (1, 1))
    pivot = render_vector(values, "Pivot", (0.5, 0.5))
    translate = render_vector(values, "Translate", (0, 0))
    parent_screen = context["rectangles"][parent["id"]]
    expected = context["rectangles"][render_id]
    for index, (axis, dimension) in enumerate((("x", "width"), ("y", "height"))):
        parent_scale = parent_screen[dimension] / parent_bounds[index + 2]
        position = bounds[index] + bounds[index + 2] * pivot[index] * (1 - scale[index]) + translate[index]
        observed = parent_screen[axis] + position * parent_scale
        size = bounds[index + 2] * scale[index] * parent_scale
        tolerance = 0.21 * max(abs(parent_scale), 1)
        if abs(observed - expected[axis]) > tolerance or abs(size - expected[dimension]) > tolerance:
            raise ValueError("overlay_render_projection_mismatch")


def frame_geometry(node: dict, context: dict, inherited: list) -> dict:
    """@brief 生成来自原生几何的已验证框。 @param node 原始节点。 @param context 上下文。
    @param inherited 祖先裁剪。 @return 带证据的几何。
    """
    node_id = str(node["attributes"].get("ID"))
    native = context["native"].get(node_id, {})
    bounds = context["rectangles"][context["links"][node_id]["id"]]
    if node_id in context["overlays"]:
        verify_overlay_render(node_id, context)
    hidden = native.get("visible") == "false" or native.get("active") == "false"
    link = context["links"][node_id]
    detached = link["id"] in context["detached_render"]
    if detached:
        clips = []
    else:
        clips = inherited if hidden and link["id"] not in context["render"] else frame_clips(link, context, inherited)
    clipped = None if detached else bounds
    for clip in clips:
        clipped = intersection(clipped, clip["bounds"])
    return {"status": "verified", "screen_bounds": bounds, "rectangular_clip_bounds": clipped,
            "clip_chain": clips, "clipping_status": "not_applicable" if detached else "verified",
            "rendered": not (hidden or detached), "native_window_rect": native.get("$rect"),
            "render_state": "not_rendered" if hidden or detached else "rendered",
            "render_state_source": "RenderService.allNodeNotOnTree" if detached else "ArkUI_and_RenderService",
            "source": {"method": "ArkUI.PaintRectAncestorComposition",
                       "commands": ["WindowManagerService.-element", "WindowManagerService.-allInfo",
                                    "RenderService.RSTree", "RenderService.allNodeNotOnTree"], "node_id": node_id,
                       "native_rect_cross_checked": node_id not in context["overlays"],
                       "overlay_render_cross_checked": node_id in context["overlays"],
                       "native_traversal_omission": ("UINode.DumpSimplifyTree.excludes_OverlayNode"
                                                     if node_id in context["overlays"] else None),
                       "render_node_id": link["id"],
                       "render_node_materialized": link["id"] in context["render"] or detached,
                       "render_node_on_screen_tree": link["id"] in context["render"]}}


def issue_for(node: dict, error: Exception) -> dict:
    """@brief 保存不含业务正文的错误证据。 @param node 原始节点。 @param error 错误。 @return 错误记录。"""
    issue = {"code": str(error), "node_id": str(node.get("attributes", {}).get("ID")),
             "source_line": node.get("source_line")}
    logging.error("ArkUI coordinate validation failed: %s node=%s", issue["code"], issue["node_id"])
    return issue


def prepare_context(root: dict, window: dict, canvas: dict, evidence: dict) -> dict:
    """@brief 建立必要来源并拒绝不一致根。 @param root 原始根。 @param window 窗口。
    @param canvas 画布。 @param evidence 官方几何和渲染证据。 @return 验证上下文。
    """
    native_root = select_native_root(root, evidence["arkui_geometry"])
    native, parents = subtree_index(native_root)
    mapping = transform_context(native_root, window, canvas)
    node_evidence = source_nodes(root, native, mapping)
    links = node_evidence["links"]
    root_id = str(root["attributes"].get("ID"))
    if root_id not in links:
        raise ValueError("root_frame_geometry_missing")
    render = render_index(evidence["render_tree"])
    detached = detached_render_index(evidence["render_detached_tree"])
    linked_ids = {link["id"] for link in links.values()}
    if linked_ids.intersection(render).intersection(detached):
        raise ValueError("render_attachment_state_changed")
    window_bounds = screen_rect(mapping["root"], mapping)
    return {"native": native, "parents": parents, "mapping": mapping,
            **node_evidence, "render": render, "detached_render": detached,
            "root_render_id": links[root_id]["id"], "window_clips": [
                {"source": "canvas", "kind": "rectangle", "bounds": {"x": 0, "y": 0, **canvas}},
                {"source": "WindowManagerService", "kind": "rectangle", "bounds": window_bounds}]}


def normalize_nodes(root: dict, context: dict) -> tuple[list, dict]:
    """@brief 验证所有节点而不丢弃失败分支。 @param root 复制后的树。 @param context 上下文。
    @return 错误和计数。
    """
    issues, counts = [], {"frame_nodes": 0, "verified_frame_nodes": 0, "non_layout_nodes": 0}
    pending = [(root, None, context["window_clips"], (0, 0, 1, 1, 0))]
    while pending:
        node, parent_id, inherited, projection = pending.pop()
        attrs, clips = node["attributes"], inherited
        try:
            node_id = node_key(str(attrs.get("ID")), attrs["type"].split(",", 1)[0], parent_id)
            native = context["native"].get(node_id)
            overlay = node_id in context["overlays"]
            if not overlay and (native is None or native.get("$type") != attrs["type"].split(",", 1)[0]):
                raise ValueError("native_geometry_identity_mismatch")
            if not overlay and context["parents"][node_id] != parent_id:
                raise ValueError("native_geometry_parent_mismatch")
            if "FrameRect" in attrs:
                counts["frame_nodes"] += 1
                projection = (local_projection(node, projection)[0] if overlay
                              else verify_native_projection(node, context, projection))
                node["geometry"] = frame_geometry(node, context, inherited)
                clips = node["geometry"]["clip_chain"]
                counts["verified_frame_nodes"] += 1
            elif attrs["type"].split(",", 1)[0] in NON_LAYOUT and (overlay or "$rect" not in native):
                node["geometry"] = {"status": "not_applicable", "reason": "non_layout_arkui_node"}
                counts["non_layout_nodes"] += 1
            else:
                raise ValueError("layout_geometry_missing")
        except (KeyError, TypeError, ValueError) as error:
            issues.append(issue_for(node, error))
            node["geometry"] = {"status": "failed", "screen_bounds": None, "reason": str(error)}
        pending.extend((child, node_id, clips, projection) for child in node["children"])
    return issues, counts


def normalize_arkui(root: dict, window: dict, canvas: dict, evidence: dict) -> dict:
    """@brief 验证原生 ArkUI 整屏矩形并保留全部原始字段。 @param root 原始树。 @param window WMS 窗口。
    @param canvas 屏幕尺寸。 @param evidence 原生几何及渲染树。 @return 带完整验证结果的树。
    """
    result = copy.deepcopy(root)
    try:
        context = prepare_context(root, window, canvas, evidence)
        issues, counts = normalize_nodes(result, context)
    except (KeyError, TypeError, ValueError) as error:
        issues, counts = [issue_for(root, error)], {}
    result["coordinate_validation"] = {
        "status": "failed" if issues else "verified", "coordinate_system": "screen_pixels",
        "canvas": canvas, "issues": issues, **counts, "sources": SOURCES,
        "bounds_semantics": "axis_aligned_component_bounds_before_clipping",
        "clip_semantics": "clip_chain_preserves_rectangles_and_rounded_corners; not_pixel_occlusion",
        "native_rect_precision_pixels": 0.01,
    }
    return result
