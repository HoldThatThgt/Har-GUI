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
    "scene_window": "https://github.com/openharmony/arkui_ace_engine/blob/"
                    "21b150e7bb1d5308846f8e5be8ef0482fb433315/"
                    "frameworks/core/components_ng/pattern/window_scene/scene/window_scene_layout_manager.cpp",
    "render_envelope": "https://github.com/openharmony/graphic_graphic_2d/blob/"
                       "c19cedf62fefde6bf184aa815feeaf0346aa4d49/"
                       "rosen/modules/render_service_base/src/common/rs_obj_abs_geometry.cpp",
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
        parent_id = stack[-1][1] if stack else None
        nodes[node_id] = {"id": node_id, "kind": kind, "line": line,
                          "parent": parent_id, "children_count": 0}
        if parent_id is not None:
            nodes[parent_id]["children_count"] += 1
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


def screen_rect(rect: dict, mapping: dict) -> dict:
    """@brief 按已核验 SCB 浮点父链映射窗口局部矩形。 @param rect 原生框。
    @param mapping 精确映射。 @return 整屏框，系统原生屏幕矩形不再重复缩放。
    """
    if mapping["coordinate_space"] == "scene":
        return dict(rect)
    result = {}
    for index, (axis, dimension) in enumerate((("x", "width"), ("y", "height"))):
        scale = mapping["affine"][index + 2]
        first = mapping["affine"][index] + rect[axis] * scale
        last = first + rect[dimension] * scale
        result[axis], result[dimension] = min(first, last), abs(last - first)
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
    if len(scale) != 2 or len(translate) != 2 or len(pivot) != 2 or any(value == 0 for value in scale):
        raise ValueError("invalid_local_axis_transform")
    px, py, psx, psy, error = parent
    tx = px + psx * (x + width * pivot[0] * (1 - scale[0]) + translate[0])
    ty = py + psy * (y + height * pivot[1] * (1 - scale[1]) + translate[1])
    sx, sy = psx * scale[0], psy * scale[1]
    tolerance = error + 0.011 * max(abs(psx), abs(psy), abs(sx), abs(sy), 1)
    rect = {"x": min(tx, tx + width * sx), "y": min(ty, ty + height * sy),
            "width": abs(width * sx), "height": abs(height * sy)}
    return (tx, ty, sx, sy, tolerance), rect


def scene_projection(node: dict, native: tuple, parent: tuple) -> tuple:
    """@brief 按完整原生身份和父关系验证一层场景投影。 @param node 原始场景节点。
    @param native 原生节点及父索引。 @param parent 父标识和轴变换。 @return 标识、变换及可选矩形。
    """
    nodes, parents = native
    parent_id, projection = parent
    attrs = node["attributes"]
    tag = attrs["type"].split(",", 1)[0]
    node_id = node_key(str(attrs["ID"]), tag, parent_id)
    actual = nodes.get(node_id)
    if actual is None or actual["$type"] != tag or parents[node_id] != parent_id:
        raise ValueError("scene_native_identity_or_parent_mismatch:" + node_id)
    if attrs.get("compid") and actual.get("compid") != attrs["compid"]:
        raise ValueError("scene_native_component_mismatch:" + node_id)
    if "FrameRect" not in attrs:
        if "$rect" in actual:
            raise ValueError("scene_frame_geometry_missing:" + node_id)
        return node_id, projection, None
    if "$rect" not in actual:
        raise ValueError("scene_native_frame_missing:" + node_id)
    projection, rect = local_projection(node, projection)
    observed = native_rect(actual["$rect"])
    if any(abs(observed[key] - rect[key]) > projection[4] + 0.011 for key in rect):
        raise ValueError("scene_native_projection_mismatch:" + node_id)
    return node_id, projection, rect


def scene_records(scene: dict) -> dict:
    """@brief 合成场景祖先及完整会话子树，保留每个真实渲染节点的精确矩形。
    @param scene 本批已核验来源。 @return 原生索引、轴变换和渲染关联。
    """
    native = subtree_index(scene["native"])
    parent = (None, (0, 0, 1, 1, 0))
    records, rectangles, directions = {}, {}, {}
    path = scene["path"]
    for node in path[:-1]:
        node_id, projection, rect = scene_projection(node, native, parent)
        if rect is not None:
            link = client_render(node)
            if link["id"] in records:
                raise ValueError("ambiguous_scene_render_mapping")
            records[link["id"]] = (node, projection)
            rectangles[link["id"]] = rect
            directions[link["id"]] = tuple(math.copysign(1, value) for value in projection[2:4])
        parent = (node_id, projection)
    session_parent = parent[1]
    pending = [(path[-1], parent)]
    while pending:
        node, parent = pending.pop()
        node_id, projection, rect = scene_projection(node, native, parent)
        if rect is not None:
            link = client_render(node)
            if link["id"] in records:
                raise ValueError("ambiguous_scene_render_mapping")
            records[link["id"]] = (node, projection)
            rectangles[link["id"]] = rect
            directions[link["id"]] = tuple(math.copysign(1, value) for value in projection[2:4])
        pending.extend((child, (node_id, projection)) for child in node["children"])
    return {"native": native[0], "parents": native[1], "nodes": records,
            "rectangles": rectangles, "directions": directions,
            "parent_projection": session_parent}


def printed_number_radius(value: str) -> float:
    """@brief 读取显式小数文本的舍入半宽，整数文本按精确整数处理。 @param value 原始数值。
    @return 该打印精度的半宽，不作为几何校验阈值。
    """
    mantissa, _, exponent = value.lower().partition("e")
    if "." not in mantissa:
        return 0.0
    return 0.5 * 10 ** (int(exponent or "0") - len(mantissa.split(".", 1)[1]))


def component_render_bounds(render: dict, local: list) -> tuple[list | None, bool]:
    """@brief 区分真实局部矩形和明确无绘制的空叶节点，不把未生效 Bounds 用作屏幕坐标。
    @param render 已挂载节点。 @param local 原始局部框。 @return 可比较矩形及无绘制状态。
    """
    match = re.search(r"Properties: Bounds\[([^\]]+)\] Frame\[([^\]]+)\]", render["line"])
    if match is None:
        raise ValueError("render_modifier_evidence_missing")
    no_draw = (render["children_count"] == 0 and "hasDrawContent: 0," in render["line"]
               and "hasChildrenDrawContent: 0," in render["line"])
    if match[1].split() == ["-inf"] * 4 and match[2].split() == ["-inf"] * 4:
        if local[2:] != [0, 0] or not no_draw:
            raise ValueError("non_finite_geometry")
        return None, True
    bounds = list(map(number, match[1].split()))
    if len(bounds) != 4:
        raise ValueError("invalid_render_bounds")
    if min(bounds[2:] + local[2:]) < 0:
        same_signs = all((first < 0) == (second < 0) for first, second in zip(bounds[2:], local[2:]))
        if not no_draw or max(bounds[2:] + local[2:]) > 0 or not same_signs:
            raise ValueError("invalid_render_bounds")
    return bounds, no_draw and max(local[2:]) <= 0


def verify_render_geometry(render: dict, node: dict) -> bool:
    """@brief 校验真实 RS 节点与同一原始组件的局部框及变换打印区间。 @param render 渲染节点。
    @param node 原始组件。 @return 是否为明确无绘制的空叶节点。
    """
    attrs = node["attributes"]
    match = LOCAL_RECT.fullmatch(attrs["PaintRect without transform"])
    expected = list(map(number, match.groups()))
    bounds, no_draw = component_render_bounds(render, expected)
    if bounds is not None and any(abs(a - b) > 0.11 for a, b in zip(bounds, expected, strict=True)):
        raise ValueError("scene_render_bounds_mismatch:" + render["id"])
    values = verify_modifiers(render)
    vectors = (("Scale", attrs.get("scale(x,y)", "1,1")),
               ("Translate", attrs.get("translate(x,y)", "0,0")),
               ("Pivot", attrs.get("Center", "x:0.5 y:0.5")))
    scales = list(map(number, attrs.get("scale(x,y)", "1,1").split(",")))
    for name, original in vectors:
        raw = re.findall(NUMBER, original)
        axes = [index for index in range(2) if name != "Pivot" or scales[index] != 1]
        entries = values.get(name)
        if entries is None:
            expected_default = [1, 1] if name == "Scale" else [0.5, 0.5] if name == "Pivot" else [0, 0]
            if any(number(raw[index]) != expected_default[index] for index in axes):
                raise ValueError("scene_render_vector_missing:" + name)
            continue
        if len(set(entries)) != 1:
            raise ValueError("ambiguous_scene_render_vector:" + name)
        printed = re.findall(NUMBER, entries[0])
        if len(raw) != 2 or len(printed) != 2:
            raise ValueError("invalid_scene_render_vector:" + name)
        for index in axes:
            first, second = raw[index], printed[index]
            radius = printed_number_radius(first) + printed_number_radius(second)
            if abs(number(first) - number(second)) > radius + 1e-12:
                raise ValueError("scene_render_vector_mismatch:" + name)
    return no_draw


def verify_window_envelope(window: dict, node: dict, screen: dict) -> None:
    """@brief 将 WMS 整数外包口径与原生两位小数边界交叉验证。 @param window 原始 WMS 窗口。
    @param node 原始会话节点。 @param screen 独立原生屏幕矩形。
    """
    match = LOCAL_RECT.fullmatch(node["attributes"]["PaintRect without transform"])
    local = list(map(number, match.groups()))
    box, transform = window["bounding_box"], window["transform"]
    if window.get("orientation", 0) != 0:
        raise ValueError("unsupported_window_orientation")
    if min(transform["scale_x"], transform["scale_y"]) <= 0:
        raise ValueError("unsupported_window_scale")
    for index, (axis, dimension) in enumerate((("x", "width"), ("y", "height"))):
        if abs(local[index + 2] - box[dimension]) > 0.02:
            raise ValueError("scene_window_size_mismatch")
        # 原生 ToBounds 只打印两位小数；在其明确精度区间内核验整数外包，不能裸 ceil 六位缩放乘积。
        origins = {math.floor(screen[axis] - 0.005), math.floor(screen[axis] + 0.005)}
        if box[axis] not in origins:
            raise ValueError("scene_wms_outer_origin_mismatch:" + axis)
        edge = screen[axis] + screen[dimension]
        sizes = {math.ceil(edge - 0.005) - box[axis], math.ceil(edge + 0.005) - box[axis]}
        projected = box[dimension] * transform["scale_" + axis]
        if not any(abs(projected - value) <= 0.11 for value in sizes):
            raise ValueError("scene_wms_outer_scale_mismatch:" + axis)


def scene_display(records: dict, ancestors: list, window: dict, canvas: dict) -> tuple[str, str]:
    """@brief 用精确 DisplayNode 身份关联真实屏幕、显示器和画布。 @param records 场景索引。
    @param ancestors 实际渲染祖先。 @param window 目标窗口。 @param canvas 画布。 @return 显示器及屏幕标识。
    """
    screens = [node for node in ancestors if node["kind"] == "SCREEN_NODE"]
    displays = [node for node in ancestors if node["kind"] == "LOGICAL_DISPLAY_NODE"]
    if len(screens) != 1 or len(displays) != 1:
        raise ValueError("scene_display_identity_missing_or_ambiguous")
    screen, display = screens[0], displays[0]
    match = re.search(r"screenId\[(\d+)\]", screen["line"])
    if match is None or int(match[1]) != window["display_id"] or display["parent"] != screen["id"]:
        raise ValueError("scene_display_identity_mismatch")
    record = records["nodes"].get(display["id"])
    if record is None or record[0]["attributes"]["type"].split(",", 1)[0] != "Screen":
        raise ValueError("scene_display_node_unmapped")
    for node in (screen, display):
        expected = [0, 0, canvas["width"], canvas["height"]]
        if any(abs(a - b) > 0.11 for a, b in zip(render_bounds(node), expected, strict=True)):
            raise ValueError("scene_display_canvas_mismatch")
    values = verify_modifiers(screen)
    if render_vector(values, "Scale", (1, 1)) != [1, 1] or render_vector(values, "Translate", (0, 0)) != [0, 0]:
        raise ValueError("unsupported_scene_screen_transform")
    return display["id"], screen["id"]


def verify_omitted_scene_frame(record: tuple, render: dict, detached: dict) -> None:
    """@brief 省略帧必须有明确未挂载或懒加载证据且局部变换为恒等。 @param record 场景帧。
    @param render 已挂载索引。 @param detached 未挂载索引。
    """
    node, _projection = record
    link = client_render(node)
    code = "scene_render_frame_ancestor_missing:" + str(node["attributes"]["ID"])
    if link["id"] in render or (link["id"] not in detached and not link["lazy"]):
        raise ValueError(code)
    local, _rect = local_projection(node, (0, 0, 1, 1, 0))
    if local[:4] != (0, 0, 1, 1):
        raise ValueError(code)
    if link["id"] in detached:
        # 未挂载节点没有当前生效的 Bounds，不参与投影；仍拒绝与恒等变换冲突的修饰器。
        values = verify_modifiers(detached[link["id"]])
        if render_vector(values, "Scale", (1, 1)) != [1, 1]:
            raise ValueError(code)
        if render_vector(values, "Translate", (0, 0)) != [0, 0]:
            raise ValueError(code)


def verify_scene_ancestors(records: dict, ancestors: list, render: dict, detached: dict) -> None:
    """@brief 按实际 RS 顺序核验场景帧身份和原生父链，不只比较祖先集合。 @param records 场景索引。
    @param ancestors 当前渲染链。 @param render 已挂载索引。 @param detached 未挂载索引。
    """
    matched = []
    frames = {str(item[0]["attributes"]["ID"]): item for item in records["nodes"].values()}
    for index, item in enumerate(ancestors):
        if item["kind"] in ("RS_NODE", "SCREEN_NODE"):
            continue
        if index == 0 and item["id"] not in records["nodes"]:
            continue
        if item["id"] not in records["nodes"]:
            raise ValueError("unmapped_scene_render_ancestor:" + item["id"])
        record = records["nodes"][item["id"]]
        verify_render_geometry(item, record[0])
        matched.append(str(record[0]["attributes"]["ID"]))
    for child, expected in zip(matched, matched[1:] + [None]):
        parent = records["parents"][child]
        while parent is not None and parent != expected:
            if parent in frames:
                verify_omitted_scene_frame(frames[parent], render, detached)
            parent = records["parents"][parent]
        if parent != expected:
            raise ValueError("scene_render_parent_order_mismatch")


def scene_window_identity(root: dict, window: dict, scene: dict) -> dict:
    """@brief 交叉核验系统根、目标会话及显示器归属。 @param root 当前根。
    @param window 原始窗口。 @param scene 完整场景证据。 @return 目标会话节点。
    """
    if not isinstance(scene, dict) or not scene.get("path"):
        raise ValueError("missing_scene_coordinate_evidence")
    if type(window.get("is_system")) is not bool:
        raise ValueError("scene_window_identity_mismatch")
    if scene["source"]["display_id"] != window["display_id"]:
        raise ValueError("scene_display_identity_mismatch")
    node = scene["path"][-1]
    attrs = node["attributes"]
    if (attrs["type"].split(",", 1)[0] != "WindowScene" or attrs.get("compid") != f"session{window['window_id']}"
            or attrs.get("AccessibilityId") != str(window["ui_node_id"])):
        raise ValueError("scene_session_identity_mismatch")
    if window["is_system"] and (root["attributes"].get("ID") != attrs["ID"]
                                or window["process_id"] != scene["source"]["process_id"]):
        raise ValueError("scene_system_root_identity_mismatch")
    return node


def application_viewport(records: dict, ancestors: list, native_bounds: dict) -> tuple:
    """@brief 取应用根的实际场景父坐标系，包含会话内子 Surface 的完整变换。 @param records 场景索引。
    @param ancestors 实际 RS 祖先链。 @param native_bounds 应用原生视口。 @return 渲染标识、节点与完整变换。
    """
    for ancestor in ancestors[1:]:
        record = records["nodes"].get(ancestor["id"])
        if record is None:
            continue
        node, projection = record
        match = LOCAL_RECT.fullmatch(node["attributes"]["PaintRect without transform"])
        local = list(map(number, match.groups()))
        if any(abs(local[index + 2] - native_bounds[key]) > 0.02
               for index, key in enumerate(("width", "height"))):
            raise ValueError("scene_application_viewport_size_mismatch")
        return ancestor["id"], node, projection
    raise ValueError("scene_application_viewport_missing")


def scene_mapping(root: dict, window: dict, canvas: dict, evidence: dict) -> dict:
    """@brief 只按完整 SCB 来源建立精确屏幕映射，不以 WMS 近似值代替。 @param root 当前组件根。
    @param window 窗口身份。 @param canvas 画布。 @param evidence 原始几何及完整场景来源。 @return 映射及索引。
    """
    scene = evidence.get("scene")
    node = scene_window_identity(root, window, scene)
    attrs = node["attributes"]
    records = scene_records(scene)
    session_id = client_render(node)["id"]
    render = render_index(evidence["render_tree"])
    detached = detached_render_index(evidence["render_detached_tree"])
    ancestors = render_ancestors(client_render(root)["id"], render)
    if session_id not in {item["id"] for item in ancestors} or render[session_id]["kind"] != "SURFACE_NODE":
        raise ValueError("scene_render_window_mismatch")
    names = re.findall(r"nodeName: \[([^\]]*)\]", render[session_id]["line"])
    if names != [f"session{window['window_id']}"]:
        raise ValueError("scene_render_session_identity_mismatch")
    display_id, screen_id = scene_display(records, ancestors, window, canvas)
    verify_scene_ancestors(records, ancestors, render, detached)
    observed = native_rect(records["native"][str(attrs["ID"])]["$rect"])
    verify_window_envelope(window, node, observed)
    native_root = select_native_root(root, evidence["arkui_geometry"])
    native_bounds = native_rect(native_root["$rect"])
    if not window["is_system"] and any(abs(native_bounds[key] - window["bounding_box"][key]) > 0.02
                                       for key in ("width", "height")):
        raise ValueError("native_root_window_size_mismatch")
    projection = records["parent_projection"] if window["is_system"] else (0, 0, 1, 1, 0)
    viewport_id, viewport_node, affine = ((session_id, node, records["nodes"][session_id][1])
                                         if window["is_system"]
                                         else application_viewport(records, ancestors, native_bounds))
    direction = projection if window["is_system"] else affine
    mapping = {"affine": affine, "projection": projection, "canvas": canvas,
               "coordinate_space": "scene" if window["is_system"] else "window",
               "axis_directions": tuple(math.copysign(1, value) for value in direction[2:4])}
    source = {"method": "SceneBoard.ArkUI.PaintRectAncestorComposition", "source": scene["source"],
              "session_node_id": str(attrs["ID"]), "session_render_node_id": session_id,
              "viewport_node_id": str(viewport_node["attributes"]["ID"]), "viewport_render_node_id": viewport_id,
              "display_render_node_id": display_id, "screen_render_node_id": screen_id,
              "coordinate_space": mapping["coordinate_space"]}
    return {**records, "mapping": mapping, "source": source, "render": render, "detached_render": detached}


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
    links, rectangles, projections, directions, overlays = {}, {}, {}, {}, set()
    pending = [(raw, mapping["projection"], False, None)]
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
        directions[link["id"]] = tuple(math.copysign(1, value) * mapping["axis_directions"][index]
                                      for index, value in enumerate(projection[2:4]))
        if mapping["coordinate_space"] == "scene":
            directions[link["id"]] = tuple(math.copysign(1, value) for value in projection[2:4])
    native_frames = {node_id for node_id, node in native.items() if "$rect" in node}
    raw_frames = set(links) - overlays
    if native_frames != raw_frames:
        native_only = ",".join(sorted(native_frames - raw_frames))
        raw_only = ",".join(sorted(raw_frames - native_frames))
        raise ValueError(f"native_geometry_frame_set_mismatch:native_only={native_only};raw_only={raw_only}")
    return {"links": links, "rectangles": rectangles, "projections": projections,
            "directions": directions, "overlays": overlays}


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


def render_clip(render: dict, values: dict, rectangles: dict, directions: dict) -> dict | None:
    """@brief 解析实际生效的矩形及圆角裁剪。 @param render 渲染节点。 @param values 修饰器。
    @param rectangles 已关联整屏矩形。 @param directions 原生父链轴方向。 @return 裁剪记录或空。
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
            "local_axis_directions": dict(zip(("x", "y"), directions[render["id"]])),
            "mode": "bounds" if bounds_clip else "frame"}


def content_clip(render: dict, values: dict, rectangles: dict, directions: dict) -> dict | None:
    """@brief 转换官方内容裁剪矩形，保留内容与子节点作用范围。 @param render 渲染节点。
    @param values 修饰器。 @param rectangles 整屏框。 @param directions 原生父链轴方向。 @return 内容裁剪或空。
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
    if len(bounds) != 4 or min(bounds[2:]) <= 0 or local[2] < local[0] or local[3] < local[1]:
        raise ValueError("invalid_content_clip_rectangle")
    screen = rectangles[render["id"]]
    rect = {}
    for index, (axis, dimension) in enumerate((("x", "width"), ("y", "height"))):
        scale = directions[render["id"]][index] * screen[dimension] / bounds[index + 2]
        origin = screen[axis] + (screen[dimension] if scale < 0 else 0)
        first, last = origin + local[index] * scale, origin + local[index + 2] * scale
        rect[axis], rect[dimension] = min(first, last), abs(last - first)
    return {"source": "RenderService.RSTree.CustomClipToFrame", "render_node_id": render["id"],
            "kind": "rectangle", "bounds": rect, "local_bounds_ltrb": local,
            "mode": "content_and_children"}


def frame_clips(link: dict, context: dict, inherited: list) -> list:
    """@brief 使用真实渲染祖先链验证裁剪。 @param link 组件关联。 @param context 本批上下文。
    @param inherited 非独立渲染节点的祖先裁剪。 @return 裁剪链。
    """
    if link["id"] not in context["render"]:
        if link["lazy"]:
            return inherited
        raise ValueError("active_render_node_missing:" + link["id"])
    clips = list(context["window_clips"])
    ancestors = render_ancestors(link["id"], context["render"])
    if context["root_render_id"] not in {node["id"] for node in ancestors}:
        raise ValueError("render_node_window_mismatch")
    verify_render_directions(ancestors, context)
    for render in ancestors:
        if render["kind"] == "RS_NODE":
            continue
        values = verify_modifiers(render)
        clip = render_clip(render, values, context["rectangles"], context["directions"])
        if clip:
            clips.append(clip)
        custom = content_clip(render, values, context["rectangles"], context["directions"])
        if custom:
            clips.append(custom)
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


def verify_render_directions(ancestors: list, context: dict) -> None:
    """@brief 用实际渲染父链交叉验证反射方向，防止同包围框掩盖方向变化。
    @param ancestors 当前节点至根的渲染链。 @param context 原生坐标证据。
    """
    direction, inside_root = list(context["mapping"]["axis_directions"]), False
    for render in reversed(ancestors):
        inside_root = inside_root or render["id"] == context["root_render_id"]
        if not inside_root or render["kind"] == "RS_NODE":
            continue
        scale = render_vector(verify_modifiers(render), "Scale", (1, 1))
        if any(value == 0 for value in scale):
            raise ValueError("unsupported_render_zero_scale")
        direction = [previous * math.copysign(1, value) for previous, value in zip(direction, scale)]
        expected = context["directions"].get(render["id"])
        if expected is not None and tuple(direction) != expected:
            raise ValueError("render_axis_direction_mismatch")


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
        parent_direction = context["directions"][parent["id"]][index]
        parent_scale = parent_direction * parent_screen[dimension] / parent_bounds[index + 2]
        position = bounds[index] + bounds[index + 2] * pivot[index] * (1 - scale[index]) + translate[index]
        origin = parent_screen[axis] + (parent_screen[dimension] if parent_scale < 0 else 0)
        first = origin + position * parent_scale
        size = bounds[index + 2] * scale[index] * parent_scale
        observed = min(first, first + size)
        tolerance = 0.21 * max(abs(parent_scale), 1)
        direction = math.copysign(1, scale[index] * parent_scale)
        if scale[index] == 0 or direction != context["directions"][render_id][index]:
            raise ValueError("overlay_render_axis_direction_mismatch")
        if abs(observed - expected[axis]) > tolerance or abs(abs(size) - expected[dimension]) > tolerance:
            raise ValueError("overlay_render_projection_mismatch")


def frame_geometry(node: dict, context: dict, inherited: list) -> dict:
    """@brief 生成来自原生几何的已验证框。 @param node 原始节点。 @param context 上下文。
    @param inherited 祖先裁剪。 @return 带证据的几何。
    """
    node_id = str(node["attributes"].get("ID"))
    native = context["native"].get(node_id, {})
    link = context["links"][node_id]
    no_draw = False
    bounds = context["rectangles"][link["id"]]
    if node_id in context["overlays"]:
        verify_overlay_render(node_id, context)
    hidden = native.get("visible") == "false" or native.get("active") == "false"
    detached = link["id"] in context["detached_render"]
    if detached:
        clips = []
    else:
        clips = inherited if hidden and link["id"] not in context["render"] else frame_clips(link, context, inherited)
    if link["id"] in context["render"]:
        no_draw = verify_render_geometry(context["render"][link["id"]], node)
    clipped = None if detached else bounds
    for clip in clips:
        clipped = intersection(clipped, clip["bounds"])
    return {"status": "verified", "screen_bounds": bounds, "rectangular_clip_bounds": clipped,
            "clip_chain": clips, "clipping_status": "not_applicable" if detached else "verified",
            "rendered": not (hidden or detached or no_draw), "native_window_rect": native.get("$rect"),
            "render_state": "not_rendered" if hidden or detached or no_draw else "rendered",
            "render_state_source": ("RenderService.allNodeNotOnTree" if detached else
                                    "RenderService.no_draw_leaf" if no_draw else "ArkUI_and_RenderService"),
            "source": {"method": "ArkUI.PaintRectAncestorComposition",
                       "commands": ["WindowManagerService.-element", "WindowManagerService.-allInfo",
                                    "RenderService.RSTree", "RenderService.allNodeNotOnTree"], "node_id": node_id,
                       "native_rect_cross_checked": node_id not in context["overlays"],
                       "window_mapping": context["window_mapping"],
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
    scene = scene_mapping(root, window, canvas, evidence)
    mapping = scene["mapping"]
    node_evidence = source_nodes(root, native, mapping)
    links = node_evidence["links"]
    if not window["is_system"] and any(link["id"] in scene["nodes"] for link in links.values()):
        raise ValueError("scene_application_render_identity_overlap")
    root_id = str(root["attributes"].get("ID"))
    if root_id not in links:
        raise ValueError("root_frame_geometry_missing")
    render, detached = scene["render"], scene["detached_render"]
    linked_ids = {link["id"] for link in links.values()}
    if linked_ids.intersection(render).intersection(detached):
        raise ValueError("render_attachment_state_changed")
    window_bounds = scene["rectangles"][scene["source"]["session_render_node_id"]]
    return {"native": native, "parents": parents, "mapping": mapping,
            **node_evidence, "render": render, "detached_render": detached,
            "rectangles": {**scene["rectangles"], **node_evidence["rectangles"]},
            "directions": {**scene["directions"], **node_evidence["directions"]},
            "window_mapping": scene["source"],
            "root_render_id": links[root_id]["id"], "window_clips": [
                {"source": "canvas", "kind": "rectangle", "bounds": {"x": 0, "y": 0, **canvas}},
                {"source": "SceneBoard.WindowScene", "kind": "rectangle", "bounds": window_bounds}]}


def normalize_nodes(root: dict, context: dict) -> tuple[list, dict]:
    """@brief 验证所有节点而不丢弃失败分支。 @param root 复制后的树。 @param context 上下文。
    @return 错误和计数。
    """
    issues, counts = [], {"frame_nodes": 0, "verified_frame_nodes": 0, "non_layout_nodes": 0}
    pending = [(root, None, context["window_clips"], context["mapping"]["projection"])]
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
