"""@brief 关联原生 ArkUI 已变换矩形与渲染树，验证整屏坐标和裁剪来源。"""

import copy
import logging
import math
import re
import struct

from har_capture import affine
from har_capture.arkui import node_property_text

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
RRECT_DUMP = re.compile(
    rf"rect:\[x:({NUMBER}) y:({NUMBER}) width:({NUMBER}) height:({NUMBER})\] "
    rf"radius:\[\[({NUMBER}) ({NUMBER})\] \[({NUMBER}) ({NUMBER})\] "
    rf"\[({NUMBER}) ({NUMBER})\] \[({NUMBER}) ({NUMBER})\]\]")
CLIP_TO_BOUNDS = 1 << 0
ROSEN_FLOAT_EPSILON = 2 ** -23
RECT = re.compile(rf"^\[({NUMBER}),\s*({NUMBER})\],\[({NUMBER}),\s*({NUMBER})\]$")
LOCAL_RECT = re.compile(rf"^RectT \(({NUMBER}),\s*({NUMBER})\) - \[({NUMBER}) x ({NUMBER})\]$")
RENDER_NODE = re.compile(r"^( *)\| (\w+_NODE)\[(\d+)\]")
CLIENT_NODE = re.compile(r"^\s*(?:\|\s*)?(\w+Node)\[(\d+)\].*$", re.MULTILINE)
NON_LAYOUT = {"JsView", "IfElse", "ForEach", "LazyForEach", "Repeat", "SyntaxItem", "NodeSlot", "Span", "__Recycle__"}
UNSUPPORTED = re.compile(r"Skew|Persp|Matrix|ClipBounds|CustomClip|ClipR[Rr]ect|Mask|SDF|Attraction|Spherize")
ZERO_ONLY = {"TranslateZ", "PivotZ"}
IDENTITY_PROJECTION = (affine.IDENTITY, 0.0)
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
    "render_parent_transform": "https://github.com/openharmony/graphic_graphic_2d/blob/"
                               "c19cedf62fefde6bf184aa815feeaf0346aa4d49/"
                               "rosen/modules/render_service_base/src/property/rs_properties.cpp#L545-L552",
    "render_frame_clip": "https://github.com/openharmony/graphic_graphic_2d/blob/"
                         "c19cedf62fefde6bf184aa815feeaf0346aa4d49/"
                         "rosen/modules/render_service_base/src/drawable/rs_property_drawable.cpp#L257-L276",
    "render_draw_content": "https://github.com/openharmony/graphic_graphic_2d/blob/"
                           "c19cedf62fefde6bf184aa815feeaf0346aa4d49/"
                           "rosen/modules/render_service_base/src/feature/dynamic_layer_skip/"
                           "rs_dynamic_layer_skip_controller.cpp#L87-L155",
    "window_scale_retention": "https://github.com/openharmony/window_window_manager/blob/"
                              "8b0d8fe1b973d865904c942c286b3d2a3b9b08e7/"
                              "window_scene/session/host/src/layout_controller.cpp#L253-L259",
    "window_float_comparison": "https://github.com/openharmony/window_window_manager/blob/"
                               "8b0d8fe1b973d865904c942c286b3d2a3b9b08e7/"
                               "window_scene/interfaces/include/ws_common.h#L780-L783",
    "window_scale_printing": "https://github.com/openharmony/window_window_manager/blob/"
                             "8b0d8fe1b973d865904c942c286b3d2a3b9b08e7/"
                             "window_scene/session_manager/src/scene_session_manager.cpp#L8588-L8594",
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


def node_key(node_id: str, tag: str, parent_id: str | None, children: list) -> str:
    """@brief 为官方未分配标识的回收容器及动画图片建立父域身份。 @param node_id 原生标识。
    @param tag 原生类型。 @param parent_id 父身份。 @param children 已观察直接子节点的类型和标识。
    @return 唯一关联键，回收容器同时使用完整有序子节点身份。
    """
    if node_id == "-1" and tag == "__Recycle__":
        if any(not isinstance(child_tag, str) or not child_tag or not re.fullmatch(r"\d+", child_id)
               for child_tag, child_id in children):
            logging.error("ArkUI recycle identity requires allocated direct child identities")
            raise ValueError("unverified_recycle_child_identity")
        anchor = ",".join(child_tag + ":" + child_id for child_tag, child_id in children)
        return str(parent_id) + "/__Recycle__" + ("[" + anchor + "]" if children else "")
    if node_id == "-1" and tag == "Image":
        return str(parent_id) + "/Image"
    return node_id


def raw_node_key(node: dict, parent_id: str | None) -> str:
    """@brief 从完整原文节点构造身份，官方明确排除的 Overlay 不充当原生子节点锚点。
    @param node 原始组件。 @param parent_id 实际父身份。 @return 关联键，原始字段不变。
    """
    attrs = node["attributes"]
    tag = attrs["type"].split(",", 1)[0].strip()
    if str(attrs["ID"]) != "-1" or tag != "__Recycle__":
        return node_key(str(attrs["ID"]), tag, parent_id, [])
    children = [(child["attributes"]["type"].split(",", 1)[0].strip(), str(child["attributes"]["ID"]))
                for child in node["children"] if child["attributes"].get("Description") != "OverlayNode"]
    return node_key(str(attrs["ID"]), tag, parent_id, children)


def native_node_key(node: dict, parent_id: str | None) -> str:
    """@brief 从未过滤的原生直接子节点构造身份，不按遍历序号区分同父回收容器。
    @param node 原生组件。 @param parent_id 实际父身份。 @return 双树共同使用的关联键。
    """
    if str(node["$ID"]) != "-1" or node["$type"] != "__Recycle__":
        return node_key(str(node["$ID"]), node["$type"], parent_id, [])
    children = [(child["$type"], str(child["$ID"])) for child in node.get("$children", [])]
    return node_key(str(node["$ID"]), node["$type"], parent_id, children)


def verify_recycle_ownership(node: dict, native: dict, parent_header: str | None) -> None:
    """@brief 双树锚点还须核对完整父标题，不靠相同子标识重新绑定回收对象。
    @param node 完整原文容器。 @param native 对应原生记录。 @param parent_header 实际原文父标题。
    """
    attrs = node["attributes"]
    if str(attrs["ID"]) != "-1" or attrs["type"].split(",", 1)[0].strip() != "__Recycle__":
        return
    if attrs.get("Parent") != parent_header or "FrameRect" in attrs or "$rect" in native:
        logging.error("ArkUI recycle ownership conflicts with its raw/native parent or layout kind")
        raise ValueError("recycle_parent_or_layout_mismatch")
    header = node.get("source_header", attrs["type"])
    for child in node["children"]:
        if (child["attributes"].get("Description") != "OverlayNode"
                and child["attributes"].get("Parent") != header):
            logging.error("ArkUI recycle child has conflicting parent evidence")
            raise ValueError("recycle_child_parent_mismatch")


def frame_key(node: dict) -> str:
    """@brief 帧的未分配标识必须有明确父标识，之后还需与真实双树父链交叉核验。
    @param node 原始布局帧。 @return 原生关联键，原始 ID 不变。
    """
    attrs = node["attributes"]
    node_id, tag = str(attrs["ID"]), attrs["type"].split(",", 1)[0].strip()
    parent = attrs.get("LastParentId")
    if node_id == "-1" and tag == "Image" and (not isinstance(parent, str) or not re.fullmatch(r"\d+", parent)):
        raise ValueError("undefined_image_parent_identity_missing")
    if node_id == "-1" and tag == "Image" and not re.fullmatch(
            r"ImageAnimator," + re.escape(parent) + r",-?\d+", attrs.get("Parent", "")):
        raise ValueError("undefined_image_parent_identity_mismatch")
    return node_key(node_id, tag, parent, [])


def subtree_index(root: dict) -> tuple[dict, dict]:
    """@brief 建立原生节点及父节点索引。 @param root 原生树。 @return 两个索引。"""
    nodes, parents = {}, {}
    pending = [(root, None)]
    while pending:
        node, parent_id = pending.pop()
        if (str(node["$ID"]) == "-1" and node["$type"] == "Image"
                and (parent_id not in nodes or nodes[parent_id]["$type"] != "ImageAnimator")):
            raise ValueError("undefined_image_parent_type_mismatch")
        node_id = native_node_key(node, parent_id)
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
    rows = list(CLIENT_NODE.finditer(node_property_text(node)))
    if len(rows) != 1:
        raise ValueError("missing_or_ambiguous_client_render_node")
    row = rows[0]
    return {"id": row[2], "lazy": "lazyLoad[1]" in row[0] and "nodeState[LAZY_LOAD]" in row[0]}


def modifier_groups(line: str) -> list:
    """@brief 按官方逐修饰器的进程归属分组，保留材质内部原文。 @param line 渲染原行。
    @return 进程标识及自身序列化内容。
    """
    marker = "OtherModifiers:["
    if "Properties: Bounds[" not in line or " Frame[" not in line:
        raise ValueError("render_modifier_evidence_missing")
    if marker not in line:
        return []
    content = line.split(marker, 1)[1].rstrip().rstrip(" ,\t")
    boundary = re.search(r"\], (?:sortedChildren:|Children list needs update)", content)
    if boundary:
        content = content[:boundary.start()]
    elif content.endswith("]"):
        content = content[:-1]
    else:
        raise ValueError("invalid_render_modifier_serialization")
    owners = list(re.finditer(r"(?:^|,\s*)pid:(\d+)->", content))
    if not owners:
        return [(None, content)]
    if content[:owners[0].start()].strip():
        raise ValueError("invalid_render_modifier_owner")
    return [(int(owner[1]), content[owner.end():owners[index + 1].start() if index + 1 < len(owners)
                                   else len(content)]) for index, owner in enumerate(owners)]


def modifier_items(content: str, owned: bool) -> list:
    """@brief 只读取修饰器自身字段，不将嵌套绘制材质当作几何属性。 @param content 自身序列化原文。
    @param owned 是否有明确进程边界。 @return 平级字段和值。
    """
    items, position = [], 0
    while position < len(content):
        while position < len(content) and content[position] in " ,\t":
            position += 1
        if position == len(content):
            break
        match = re.match(r"(\w+):\[", content[position:])
        if match is None:
            raise ValueError("invalid_render_modifier_serialization")
        name = match[1]
        start = position + match.end()
        if name == "ClipRrect":
            # 官方 RRect 属性转储没有闭合最外层方括号，按明确结构读取，不补齐其他损坏字段。
            value = RRECT_DUMP.match(content, start)
            if value is None:
                raise ValueError("invalid_render_modifier_serialization:ClipRrect")
            list(map(number, value.groups()))
            items.append((name, value[0]))
            position = value.end()
            continue
        if owned and name in {"MaterialNGFilter", "MaterialShader"}:
            items.append((name, content[start:]))
            break
        depth, end = 1, start
        while end < len(content) and depth:
            depth += (content[end] == "[") - (content[end] == "]")
            end += 1
        if depth:
            raise ValueError("invalid_render_modifier_serialization")
        items.append((name, content[start:end - 1]))
        position = end
    return items


def modifier_values(line: str) -> dict:
    """@brief 提取实际顶层渲染属性，不展开材质对象。 @param line 渲染原行。 @return 名称到值列表。"""
    values = {}
    for owner, content in modifier_groups(line):
        for name, value in modifier_items(content, owner is not None):
            values.setdefault(name, []).append(value)
    return values


def verify_modifiers(render: dict) -> dict:
    """@brief 独立验证当前属性和修饰器变换并拒绝未知裁剪。 @param render 渲染节点。 @return 已验证修饰器。"""
    values = modifier_values(render["line"])
    verify_inactive_render_rrect(render, values)
    for key, entries in values.items():
        if key in {"AttractionFraction", "AttractionDstpoint"}:
            fraction = values.get("AttractionFraction", ["0"])
            if all(number(value) == 0 for value in fraction):
                continue
        allowed_masks = {"ShadowMask", "BackgroundBlurMaskColor", "BehindWindowFilterMaskColor",
                         "ForegroundBlurMaskColor"}
        if UNSUPPORTED.search(key) and key not in allowed_masks and key not in {"CustomClipToFrame", "ClipRrect"}:
            raise ValueError("unsupported_render_modifier:" + key)
        if key in ZERO_ONLY and any(abs(number(value)) > 0.000001 for value in entries):
            raise ValueError("unsupported_render_transform:" + key)
    verify_render_spatial_transform(render, values)
    if "animationInfo:" in render["line"] or re.search(r"\banimation:[1-9]", render["line"]):
        raise ValueError("active_render_animation")
    return values


def current_drawable_status(render: dict) -> int:
    """@brief 只读取真实顶层绘制状态及后续缓存字段，拒绝名字和命令中的伪字段。
    @param render 同一渲染节点原行。 @return 唯一当前绘制状态位。
    """
    pattern = (r"\[|\]|(?P<boundary>\b(?:DrawCmdModifiers|OtherModifiers):)"
               r"|(?:^|,\s*)(?P<field>drawableVecStatus|opincRootCacheNull|layerPartCacheNull):"
               r"\s*(?P<value>[^\s,\[\]]+)")
    depth, fields = 0, []
    for match in re.finditer(pattern, render["line"]):
        if match[0] in {"[", "]"}:
            depth += 1 if match[0] == "[" else -1
            if depth < 0:
                raise ValueError("invalid_current_render_clip_status")
        elif match["boundary"]:
            if depth == 0:
                break
        elif depth == 0:
            fields.append((match["field"], match["value"], match.start(), match.end()))
    if (depth or [item[0] for item in fields] != ["drawableVecStatus", "opincRootCacheNull", "layerPartCacheNull"]
            or re.fullmatch(r"\d+", fields[0][1]) is None
            or any(item[1] not in {"true", "false"} for item in fields[1:])
            or any(render["line"][first[3]:second[2]].strip(" ,\t")
                   for first, second in zip(fields, fields[1:]))):
        raise ValueError("invalid_current_render_clip_status")
    return int(fields[0][1])


def verify_inactive_render_rrect(render: dict, values: dict) -> None:
    """@brief 空形状候选必须另有当前裁剪关闭的状态位，打印零本身不证明裁剪未生效。
    @param render 当前渲染记录。 @param values 已完整解析的修饰器集合。
    @see docs/render-modifiers.md 官方序列化与当前裁剪状态来源。
    """
    entries = values.get("ClipRrect")
    if entries is None:
        return
    if len(set(entries)) != 1:
        raise ValueError("ambiguous_render_clip:ClipRrect")
    match = RRECT_DUMP.fullmatch(entries[0])
    if match is None:
        raise ValueError("invalid_render_modifier_serialization:ClipRrect")
    numbers = list(map(number, match.groups()))
    if numbers[2] > 0 and numbers[3] > 0:
        raise ValueError("unsupported_render_modifier:ClipRrect")
    # GetClipToRRect 是此状态位的或条件；当前关闭证据与修饰器形状独立，不能用后者替代。
    if current_drawable_status(render) & CLIP_TO_BOUNDS or current_clip_flags(render)["ClipToBounds"]:
        raise ValueError("unverified_current_render_clip:ClipRrect")


def scalar_modifier(values: dict, name: str, default: float) -> float:
    """@brief 读取唯一有限标量修饰器。 @param values 自身修饰器。 @param name 名称。
    @param default 官方默认值。 @return 有限值。
    """
    entries = values.get(name)
    if entries is None:
        return default
    if len(set(entries)) != 1 or re.fullmatch(NUMBER, entries[0]) is None:
        raise ValueError("ambiguous_render_scalar:" + name)
    return number(entries[0])


def verify_render_spatial_transform(render: dict, values: dict) -> None:
    """@brief 仅证明不改变二维矩形的相机状态及零尺寸叶旋转，保留其他三维拒收。
    @param render 当前节点。 @param values 自身修饰器。
    """
    verify_spatial_values(render, values)
    current = {}
    for name, value in re.findall(r"(\w+)\[([^\]]*)\]", render_current_properties(render)):
        allowed_masks = {"ShadowMask", "BackgroundBlurMaskColor", "BehindWindowFilterMaskColor",
                         "ForegroundBlurMaskColor"}
        if UNSUPPORTED.search(name) and name not in allowed_masks and name != "CustomClipToFrame":
            raise ValueError("unsupported_current_render_modifier:" + name)
        current.setdefault(name, []).append(value)
    verify_spatial_values(render, current)
    for name in ("TranslateZ", "PivotZ", "ScaleZ", "Quaternion"):
        if name in current:
            raise ValueError("unsupported_current_render_transform:" + name)
    # 当前字段只在非默认值时输出；一位小数的 0.0 不能证明未进入三维旋转路径。
    if "RotationX" in current or "RotationY" in current:
        verify_zero_spatial_rotation(render, values)


def verify_spatial_values(render: dict, values: dict) -> None:
    """@brief 对当前属性或修饰器的完整三维值执行相同二维适用条件。 @param render 同一实际节点。
    @param values 一层真实属性，缺省沿用官方变换默认值。
    """
    rotations = [scalar_modifier(values, name, 0) for name in ("RotationX", "RotationY")]
    scalar_modifier(values, "CameraDistance", 0)
    for name in ZERO_ONLY:
        if scalar_modifier(values, name, 0) != 0:
            raise ValueError("unsupported_render_transform:" + name)
    if scalar_modifier(values, "ScaleZ", 1) != 1:
        raise ValueError("unsupported_render_transform:ScaleZ")
    quaternion = values.get("Quaternion")
    if quaternion is not None and (len(set(quaternion)) != 1
                                  or list(map(number, re.findall(NUMBER, quaternion[0]))) != [0, 0, 0, 1]):
        raise ValueError("unsupported_render_transform:Quaternion")
    applicable = values.get("PositionZApplicableCamera3d", ["false"])
    if any(value.lower() != "false" for value in applicable) and scalar_modifier(values, "PositionZ", 0) != 0:
        raise ValueError("unsupported_render_transform:PositionZ")
    if any(value != 0 for value in rotations):
        verify_zero_spatial_rotation(render, values)


def verify_zero_spatial_rotation(render: dict, values: dict) -> None:
    """@brief 非默认三维旋转只保留已有的零尺寸无绘制叶例外，不把打印零当二维证明。
    @param render 当前渲染节点。 @param values 独立核验的修饰器集合。
    """
    boxes = current_render_boxes(render)
    zero = all(len(value.split()) == 4 and all(number(part) == 0 for part in value.split()[2:]) for value in boxes)
    clip = re.findall(r"ClipTo(?:Bounds|Frame)\[([^\]]+)\]", render_current_properties(render))
    if (not zero or render["children_count"] or scalar_modifier(values, "CameraDistance", 0) != 0
            or not render_has_no_draw_content(render)):
        raise ValueError("unsupported_render_transform:RotationX_or_Y")
    if any(value.lower() != "false" for value in clip):
        raise ValueError("unsupported_render_spatial_clip")


def screen_rect(rect: dict, mapping: dict) -> dict:
    """@brief 按已核验 SCB 浮点父链映射窗口局部矩形。 @param rect 原生框。
    @param mapping 精确映射。 @return 整屏框，系统原生屏幕矩形不再重复缩放。
    """
    if mapping["coordinate_space"] == "scene":
        return dict(rect)
    return affine.bounds(mapping["affine"], rect)


def local_projection(node: dict, parent: tuple) -> tuple:
    """@brief 按官方局部绘制框合成二维仿射及四角包围框。 @param node 原始节点。
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
    if len(scale) != 2 or len(translate) != 2 or len(pivot) != 2:
        raise ValueError("invalid_local_axis_transform")
    verify_raw_spatial_transform(node, (width, height))
    rotation = number(attrs.get("Rotation", "0"))
    # 前向投影不求逆；有限零缩放须保留塌缩矩阵，并继续投影所有后代。
    local = affine.local({"x": x, "y": y, "width": width, "height": height}, scale, translate, pivot, rotation)
    matrix = affine.compose(parent[0], local)
    magnitude = max(*(abs(value) for value in parent[0][:4]), *(abs(value) for value in matrix[:4]), 1)
    tolerance = parent[1] + 0.011 * magnitude
    rect = affine.bounds(matrix, {"x": 0, "y": 0, "width": width, "height": height})
    return (matrix, tolerance), rect


def verify_raw_spatial_transform(node: dict, size: tuple) -> None:
    """@brief 非平面旋转仅接受不改变位置的零尺寸叶节点。 @param node 原始节点。
    @param size 局部有符号尺寸。
    """
    attrs = node["attributes"]
    rotations = [number(attrs.get(name, "0")) for name in ("RotationX", "RotationY")]
    if any(value != 0 for value in rotations) and (size != (0, 0) or node["children"]):
        raise ValueError("unsupported_raw_spatial_transform")
    if any(number(attrs.get(name, "0")) != 0 for name in ("TranslateZ", "PivotZ")):
        raise ValueError("unsupported_raw_spatial_transform")


def scene_projection(node: dict, native: tuple, parent: tuple) -> tuple:
    """@brief 按完整原生身份和父关系验证一层场景投影。 @param node 原始场景节点。
    @param native 原生节点及父索引。 @param parent 父标识、仿射变换和完整原文父标题。
    @return 标识、变换及可选矩形。
    """
    nodes, parents = native
    parent_id, projection, parent_header = parent
    attrs = node["attributes"]
    tag = attrs["type"].split(",", 1)[0]
    node_id = raw_node_key(node, parent_id)
    actual = nodes.get(node_id)
    if actual is None or actual["$type"] != tag or parents[node_id] != parent_id:
        raise ValueError("scene_native_identity_or_parent_mismatch:" + node_id)
    if attrs.get("compid") and actual.get("compid") != attrs["compid"]:
        raise ValueError("scene_native_component_mismatch:" + node_id)
    verify_recycle_ownership(node, actual, parent_header)
    if "FrameRect" not in attrs:
        if "$rect" in actual:
            raise ValueError("scene_frame_geometry_missing:" + node_id)
        return node_id, projection, None
    if "$rect" not in actual:
        raise ValueError("scene_native_frame_missing:" + node_id)
    if frame_key(node) != node_id:
        raise ValueError("scene_native_identity_or_parent_mismatch:" + node_id)
    projection, rect = local_projection(node, projection)
    observed = native_rect(actual["$rect"])
    if any(abs(observed[key] - rect[key]) > projection[1] + 0.011 for key in rect):
        raise ValueError("scene_native_projection_mismatch:" + node_id)
    return node_id, projection, rect


def scene_records(scene: dict) -> dict:
    """@brief 合成场景祖先及完整会话子树，保留每个真实渲染节点的精确矩形。
    @param scene 本批已核验来源。 @return 原生索引、轴变换和渲染关联。
    """
    native = subtree_index(scene["native"])
    parent = (None, IDENTITY_PROJECTION, None)
    records, rectangles, matrices = {}, {}, {}
    path = scene["path"]
    for node in path[:-1]:
        node_id, projection, rect = scene_projection(node, native, parent)
        if rect is not None:
            link = client_render(node)
            if link["id"] in records:
                raise ValueError("ambiguous_scene_render_mapping")
            records[link["id"]] = (node, projection)
            rectangles[link["id"]] = rect
            matrices[link["id"]] = projection[0]
        parent = (node_id, projection, node["attributes"]["type"])
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
            matrices[link["id"]] = projection[0]
        pending.extend((child, (node_id, projection, node["attributes"]["type"])) for child in node["children"])
    return {"native": native[0], "parents": native[1], "nodes": records,
            "rectangles": rectangles, "matrices": matrices,
            "parent_projection": session_parent}


def printed_number_radius(value: str) -> float:
    """@brief 读取显式小数文本的舍入半宽，整数文本按精确整数处理。 @param value 原始数值。
    @return 该打印精度的半宽，不作为几何校验阈值。
    """
    mantissa, _, exponent = value.lower().partition("e")
    if "." not in mantissa:
        return 0.0
    return 0.5 * 10 ** (int(exponent or "0") - len(mantissa.split(".", 1)[1]))


def verify_negative_render_clip(render: dict) -> None:
    """@brief 拒绝负尺寸节点自身的生效或未知裁剪，保留正尺寸祖先裁剪。 @param render 当前渲染节点。"""
    values = verify_modifiers(render)
    if any(current_clip_flags(render).values()):
        raise ValueError("unsupported_negative_render_clip")
    for name, entries in values.items():
        if "clip" in name.lower() and (name not in {"ClipToBounds", "ClipToFrame"}
                                       or any(entry.lower() != "false" for entry in entries)):
            raise ValueError("unsupported_negative_render_clip")


def verify_mixed_render_bounds(local: list, boxes: tuple) -> None:
    """@brief 单轴负尺寸仅接受明确的 -1 及另一正轴，并核对 RS 两个局部框的有符号值。
    @param local 原始局部绘制框。 @param boxes 当前 RS Bounds 和 Frame。
    """
    if not ((local[2] == -1 and local[3] > 0) or (local[3] == -1 and local[2] > 0)):
        raise ValueError("invalid_render_bounds")
    for box in boxes:
        if (len(box) != 4 or any(first * second <= 0 for first, second in zip(box[2:], local[2:]))
                or any(abs(first - second) > 0.11 for first, second in zip(box, local, strict=True))):
            raise ValueError("invalid_render_bounds")


def render_has_no_draw_content(render: dict) -> bool:
    """@brief 只接受唯一且明确的自身及子树无绘制标记，不使用缓存跳过状态。 @param render 渲染节点。
    @return 两个官方绘制内容标记是否均为零。
    """
    header = render["line"].partition("Properties:")[0]
    return (re.findall(r"\bhasDrawContent:\s*(\d+),", header) == ["0"]
            and re.findall(r"\bhasChildrenDrawContent:\s*(\d+),", header) == ["0"])


def component_render_bounds(render: dict, local: list, empty_subtree: bool) -> tuple[list | None, bool]:
    """@brief 区分真实局部矩形和已证明无绘制的节点，不把未生效 Bounds 用作屏幕坐标。
    @param render 已挂载节点。 @param local 原始局部框。 @param empty_subtree 已核验完整空子树。
    @return 可比较矩形及无绘制状态。
    """
    current_bounds, current_frame = current_render_boxes(render)
    no_draw = render_has_no_draw_content(render) and (render["children_count"] == 0 or empty_subtree)
    if current_bounds.split() == ["-inf"] * 4 and current_frame.split() == ["-inf"] * 4:
        if local[2:] != [0, 0] or not no_draw:
            raise ValueError("non_finite_geometry")
        return None, True
    bounds = list(map(number, current_bounds.split()))
    frame = list(map(number, current_frame.split()))
    if len(bounds) != 4 or len(frame) != 4:
        raise ValueError("invalid_render_bounds")
    if min(bounds[2:] + local[2:]) < 0:
        verify_negative_render_clip(render)
        same_signs = all((first < 0) == (second < 0) for first, second in zip(bounds[2:], local[2:]))
        if not no_draw or not same_signs:
            raise ValueError("invalid_render_bounds")
        if max(bounds[2:] + local[2:]) > 0:
            verify_mixed_render_bounds(local, (bounds, frame))
            return bounds, True
    return bounds, no_draw and max(local[2:]) <= 0


def verify_render_geometry(render: dict, node: dict, empty_subtree: bool) -> bool:
    """@brief 校验真实 RS 节点与同一原始组件的局部框及变换打印区间。 @param render 渲染节点。
    @param node 原始组件。 @param empty_subtree 已核验完整空子树。 @return 是否为明确无绘制的节点。
    """
    attrs = node["attributes"]
    match = LOCAL_RECT.fullmatch(attrs["PaintRect without transform"])
    expected = list(map(number, match.groups()))
    bounds, no_draw = component_render_bounds(render, expected, empty_subtree)
    if bounds is not None and any(abs(a - b) > 0.11 for a, b in zip(bounds, expected, strict=True)):
        raise ValueError("scene_render_bounds_mismatch:" + render["id"])
    verify_render_vectors(render, node)
    return no_draw


def verify_render_vectors(render: dict, node: dict) -> None:
    """@brief 独立核对原始与 RS 变换，不因当前局部框尚未准备而略过。 @param render 实际渲染记录。
    @param node 已关联的原始组件。
    """
    attrs = node["attributes"]
    values = verify_modifiers(render)
    vectors = (("Scale", attrs.get("scale(x,y)", "1,1")),
               ("Translate", attrs.get("translate(x,y)", "0,0")),
               ("Pivot", attrs.get("Center", "x:0.5 y:0.5")))
    scales = list(map(number, attrs.get("scale(x,y)", "1,1").split(",")))
    rotated = any(number(attrs.get(name, "0")) != 0 for name in ("Rotation", "RotationX", "RotationY"))
    for name, original in vectors:
        raw = re.findall(NUMBER, original)
        axes = [index for index in range(2) if name != "Pivot" or scales[index] != 1 or rotated]
        entries = values.get(name)
        if entries is None:
            expected_default = [1, 1] if name == "Scale" else [0.5, 0.5] if name == "Pivot" else [0, 0]
            if any(number(raw[index]) != expected_default[index] for index in axes):
                raise ValueError("scene_render_vector_missing:" + name)
            verify_current_vector(render, name, original, axes)
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
        verify_current_vector(render, name, original, axes)
    for name in ("Rotation", "RotationX", "RotationY", "CameraDistance"):
        verify_render_scalar(render, attrs, name)


def verify_current_vector(render: dict, name: str, original: str, axes: list) -> None:
    """@brief 当前属性按官方打印区间独立匹配原文，不从修饰器集合补齐。 @param render 当前记录。
    @param name 向量名。 @param original 原始字段。 @param axes 真正影响几何的分量。
    """
    raw = re.findall(NUMBER, original)
    if len(raw) != 2:
        raise ValueError("invalid_current_render_vector:" + name)
    current = current_vector_intervals(render, name)
    for index in axes:
        low, high = printed_interval(raw[index])
        other_low, other_high = current["parameters"][index]
        if low > other_high + 1e-12 or high < other_low - 1e-12:
            reason = "mismatch" if current["present"][index] else "missing"
            raise ValueError("current_render_vector_" + reason + ":" + name)


def printed_interval(value: str) -> tuple:
    """@brief 把实际数值文本转成其十进制舍入区间。 @param value 原始打印文本。 @return 数值上下界。"""
    center, radius = number(value), printed_number_radius(value)
    return center - radius, center + radius


def current_vector_intervals(render: dict, name: str) -> dict:
    """@brief 区分当前逐轴字段、轴心字段及官方默认省略条件，不解析修饰器集合。
    @param render 当前渲染记录。 @param name Scale、Translate 或 Pivot。 @return 参数和有效变换区间。
    """
    properties = render_current_properties(render)
    default = {"Scale": 1.0, "Translate": 0.0, "Pivot": 0.5}[name]
    fields = [name] if name == "Pivot" else [name + "X", name + "Y"]
    if name != "Pivot" and re.search(r"\b" + name + r"\[", properties):
        raise ValueError("invalid_current_render_vector_format:" + name)
    texts = []
    for field in fields:
        starts = re.findall(r"\b" + field + r"\[", properties)
        values = re.findall(r"\b" + field + r"\[([^\]]*)\]", properties)
        if len(starts) != len(values) or len(values) > 1:
            raise ValueError("ambiguous_current_render_vector:" + name)
        if not values:
            texts.extend([None, None] if name == "Pivot" else [None])
            continue
        pattern = rf"\s*({NUMBER})\s*,\s*({NUMBER})\s*" if name == "Pivot" else rf"\s*({NUMBER})\s*"
        match = re.fullmatch(pattern, values[0])
        if match is None:
            raise ValueError("invalid_current_render_vector:" + name)
        texts.extend(match.groups())
    parameters = [printed_interval(text) if text is not None else
                  (default - ROSEN_FLOAT_EPSILON, default + ROSEN_FLOAT_EPSILON) for text in texts]
    # 两轴均按 ROSEN_EQ 省略时，官方几何实现不会执行这组 Scale 或 Translate 操作。
    applied = ([(default, default)] * 2 if name != "Pivot" and all(text is None for text in texts)
               else parameters)
    return {"parameters": parameters, "matrix": applied, "present": [text is not None for text in texts]}


def verify_render_scalar(render: dict, attrs: dict, name: str) -> None:
    """@brief 核对原文、待应用值及已生效旋转，避免相同包围框掩盖角度漂移。 @param render 渲染记录。
    @param attrs 自身原始属性。 @param name 标量名称。
    """
    values = modifier_values(render["line"])
    original = attrs.get(name, "0")
    if not isinstance(original, str) or re.fullmatch(NUMBER, original) is None:
        raise ValueError("invalid_raw_transform:" + name)
    entries = values.get(name, ["0"])
    if len(set(entries)) != 1 or re.fullmatch(NUMBER, entries[0]) is None:
        raise ValueError("ambiguous_render_scalar:" + name)
    radius = printed_number_radius(original) + printed_number_radius(entries[0])
    if abs(number(original) - number(entries[0])) > radius + 1e-12:
        raise ValueError("scene_render_scalar_mismatch:" + name)
    current = re.findall(r"\b" + name + r"\[([^\]]+)\]", render_current_properties(render))
    if name == "CameraDistance":
        return
    if not current and number(original) == 0:
        return
    if len(current) != 1 or re.fullmatch(NUMBER, current[0]) is None:
        raise ValueError("current_render_scalar_missing:" + name)
    radius = printed_number_radius(original) + printed_number_radius(current[0])
    if abs(number(original) - number(current[0])) > radius + 1e-12:
        raise ValueError("current_render_scalar_mismatch:" + name)


def render_current_properties(render: dict) -> str:
    """@brief 只读取已经生效的 Properties 段，排除绘制命令与修饰器集合。 @param render 渲染记录。
    @return 当前属性原文。
    """
    current = re.split(r"RSUIContextToken:|DrawCmdModifiers|OtherModifiers:", render["line"], maxsplit=1)[0]
    if current.count("Properties:") != 1:
        raise ValueError("render_modifier_evidence_missing")
    return current.split("Properties:", 1)[1]


def render_sequence(render: dict, field: str) -> int | None:
    """@brief 读取唯一的官方准备状态字段，缺失或重复不构成证明。 @param render 渲染记录。
    @param field 明确字段名。 @return 唯一整数或空。
    """
    header = render["line"].partition("Properties:")[0]
    values = re.findall(r"\b" + re.escape(field) + r":\s*(\d+)(?=,|\s|$)", header)
    return int(values[0]) if len(values) == 1 else None


def current_render_boxes(render: dict) -> tuple[str, str]:
    """@brief 当前 Properties 必须各有唯一 Bounds 与 Frame，不从后续段落补齐或忽略冲突。
    @param render 实际渲染记录。 @return 当前两个框的原文。
    """
    properties = render_current_properties(render)
    bounds = re.findall(r"\bBounds\[([^\]]+)\]", properties)
    frame = re.findall(r"\bFrame\[([^\]]+)\]", properties)
    if len(bounds) != 1 or len(frame) != 1:
        raise ValueError("missing_or_ambiguous_current_render_bounds")
    return bounds[0], frame[0]


def owned_render_modifier(render: dict, name: str, process_id: int) -> str | None:
    """@brief 修饰器属性必须唯一且显式属于当前应用进程，不能借用其他进程的同名属性。
    @param render 渲染记录。 @param name 属性名。 @param process_id 已核验进程。 @return 唯一原值或空。
    """
    matches = [(owner, value) for owner, content in modifier_groups(render["line"])
               for field, value in modifier_items(content, owner is not None) if field == name]
    return matches[0][1] if len(matches) == 1 and matches[0][0] == process_id else None


def zero_alpha_source(node_id: str, context: dict) -> dict | None:
    """@brief 仅用同一节点的原文、原生数值及当前 RS 属性证明已准备的透明祖先。
    @param node_id 原生身份。 @param context 完整同批来源。 @return 透明祖先身份或空。
    """
    node = context["raw_frames"].get(node_id)
    native = context["native"][node_id]
    if node is None or type(native.get("opacity")) not in (int, float) or native["opacity"] != 0:
        return None
    alpha = node["attributes"].get("Alpha")
    if not isinstance(alpha, str) or not re.fullmatch(NUMBER, alpha) or number(alpha) != 0:
        return None
    render = context["render"].get(context["links"][node_id]["id"])
    if render is None or "Alpha has difference" in node_property_text(node):
        return None
    current = re.findall(r"\bAlpha\[([^\]]+)\]", render_current_properties(render))
    owned = owned_render_modifier(render, "Alpha", context["process_id"])
    pending = [owned] if owned is not None else []
    if (len(current) != 1 or len(pending) != 1
            or any(not re.fullmatch(NUMBER, value) or number(value) != 0 for value in current + pending)):
        return None
    if (render_sequence(render, "IsSubTreeSkipped") != 1 or not render_has_no_draw_content(render)
            or any((render_sequence(render, key) or 0) <= 0 for key in ("PrepareSeq", "PostPrepareSeq", "VsyncId"))):
        return None
    if native["$type"] != node["attributes"]["type"].split(",", 1)[0]:
        raise ValueError("transparent_ancestor_identity_mismatch")
    verify_render_geometry(render, node, False)
    return {"node_id": node_id, "render_node_id": render["id"]}


def zero_alpha_ancestor(node: dict, context: dict) -> dict | None:
    """@brief 沿真实原生和 RS 父链查证透明祖先，拒绝共享转场、动画及身份绕行。
    @param node 当前原始组件。 @param context 完整同批来源。 @return 已证明祖先或空。
    """
    node_id = frame_key(node)
    if node_id in context["overlays"]:
        return None
    traversed = []
    while node_id is not None:
        raw = context["raw_frames"].get(node_id)
        if raw is not None:
            render = context["render"].get(context["links"][node_id]["id"])
            if render is not None:
                traversed.append((raw, render))
            proof = zero_alpha_source(node_id, context)
            if proof is not None:
                for original, record in traversed:
                    if "SharedTransitionParam:" in record["line"] or "RSAnimationManager:" in record["line"]:
                        raise ValueError("transparent_subtree_transition_or_animation")
                    verify_modifiers(record)
                    if record["id"] != context["root_render_id"]:
                        verify_component_render_parent(original, record, context)
                return proof
        node_id = context["parents"][node_id]
    return None


def transparent_render_state(render: dict, node: dict, process_id: int) -> str | None:
    """@brief 已证透明祖先下区分逻辑框与未应用的当前状态，不替换当前属性。
    @param render 实际渲染记录。 @param node 原始组件。 @param process_id 已核验进程。
    @return 未准备或未应用状态，当前框一致时为空。
    """
    boxes = current_render_boxes(render)
    local = list(map(number, LOCAL_RECT.fullmatch(node["attributes"]["PaintRect without transform"]).groups()))
    if min(local[2:]) < 0 or max(local[2:]) == 0:
        return None
    infinite = [value.split() == ["-inf"] * 4 for value in boxes]
    if not any(infinite):
        finite = [list(map(number, value.split())) for value in boxes]
        if any(len(value) != 4 or min(value[2:]) < 0 for value in finite):
            raise ValueError("transparent_render_current_bounds_invalid")
        if any(abs(a - b) > 0.11 for a, b in zip(*finite, strict=True)):
            raise ValueError("transparent_render_current_bounds_conflict")
        if all(abs(a - b) <= 0.11 for a, b in zip(finite[0], local, strict=True)):
            return None
    elif not all(infinite):
        raise ValueError("transparent_render_current_bounds_conflict")
    sequence = [render_sequence(render, key) for key in ("PrepareSeq", "PostPrepareSeq", "VsyncId")]
    if (not (all(value == 0 for value in sequence) or all(value is not None and value > 0 for value in sequence))
            or not render_has_no_draw_content(render)):
        raise ValueError("transparent_render_preparation_unverified")
    values = verify_modifiers(render)
    for field in ("Bounds", "Frame"):
        entry = owned_render_modifier(render, field, process_id)
        match = re.fullmatch(rf"x:({NUMBER}) y:({NUMBER}) z:({NUMBER}) w:({NUMBER})", entry) if entry else None
        if match is None or any(abs(number(a) - b) > 0.11 for a, b in zip(match.groups(), local, strict=True)):
            raise ValueError("transparent_render_pending_bounds_mismatch:" + field)
    flags = re.findall(r"ClipTo(?:Bounds|Frame)\[([^\]]+)\]", render_current_properties(render))
    if any(value.lower() != "false" for value in flags):
        raise ValueError("unsupported_unprepared_render_clip")
    for name, entries in values.items():
        if "clip" in name.lower() and (name not in {"ClipToBounds", "ClipToFrame"}
                                       or any(entry.lower() != "false" for entry in entries)):
            raise ValueError("unsupported_unprepared_render_clip")
    verify_render_vectors(render, node)
    return "unprepared" if sequence == [0, 0, 0] else "unapplied"


def float32(value: float) -> float:
    """@brief 按官方 float 参数与减法的 IEEE 754 精度取值。 @param value 数值。 @return 单精度数值。"""
    return struct.unpack("<f", struct.pack("<f", value))[0]


def wms_scale_interval(value: float) -> tuple[float, float]:
    """@brief 恢复官方六位有效数字、八字符输出在解析为数值后的打印区间。 @param value 已解析正比例。
    @return 可能原生 float 值的闭区间，不作为屏幕几何误差。
    """
    exponent = math.floor(math.log10(value))
    unit = 10 ** (exponent - 5)
    lower_unit = unit / 10 if value == 10 ** exponent else unit
    lower, upper = value - lower_unit / 2, value + unit / 2
    # 小于 0.1 的定点输出可能被截为六位小数；解析后尾部零消失，必须保留该有向截断区间。
    if -4 <= exponent < -1 and float(format(value, ".6f")) == value:
        upper = value + 0.000001 - unit / 2
    return lower, upper


def float32_interval_near_equal(interval: tuple[float, float], expected: float) -> bool:
    """@brief 在已知打印区间内执行官方 float NearEqual 的闭区间判断。 @param interval 原存储值范围。
    @param expected 当前归一化比例。 @return 是否存在满足官方保留规则的可表示 float。
    """
    lower, upper = interval
    low_bits = struct.unpack("<I", struct.pack("<f", lower))[0]
    high_bits = struct.unpack("<I", struct.pack("<f", upper))[0]
    low_bits += int(float32(lower) < lower)
    high_bits -= int(float32(upper) > upper)
    if low_bits > high_bits:
        return False
    expected = float32(expected)
    expected_bits = struct.unpack("<I", struct.pack("<f", expected))[0]
    nearest_bits = min(max(expected_bits, low_bits), high_bits)
    nearest = struct.unpack("<f", struct.pack("<I", nearest_bits))[0]
    # ws_common.h 的 float 重载使用 <= 0.001f；不能替换为 MathHelper 中不同的严格比较。
    return abs(float32(nearest - expected)) <= float32(0.001)


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
        interval = wms_scale_interval(transform["scale_" + axis])
        if not any(float32_interval_near_equal(interval, value / box[dimension]) for value in sizes):
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
    if any(scalar_modifier(values, name, 0) != 0 for name in ("Rotation", "RotationX", "RotationY")):
        raise ValueError("unsupported_render_screen_transform")
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
    local, _rect = local_projection(node, IDENTITY_PROJECTION)
    if local[0] != affine.IDENTITY:
        raise ValueError(code)
    if link["id"] in detached:
        # 未挂载节点没有当前生效的 Bounds，不参与投影；仍拒绝与恒等变换冲突的修饰器。
        values = verify_modifiers(detached[link["id"]])
        try:
            verify_render_vectors(detached[link["id"]], node)
        except ValueError as error:
            raise ValueError(code + ":" + str(error)) from error
        if render_vector(values, "Scale", (1, 1)) != [1, 1]:
            raise ValueError(code)
        if render_vector(values, "Translate", (0, 0)) != [0, 0]:
            raise ValueError(code)


def verify_scene_ancestors(records: dict, ancestors: list, render: dict, detached: dict) -> None:
    """@brief 按实际 RS 顺序核验场景帧身份和原生父链，不只比较祖先集合。 @param records 场景索引。
    @param ancestors 当前渲染链。 @param render 已挂载索引。 @param detached 未挂载索引。
    """
    matched = []
    frames = {frame_key(item[0]): item for item in records["nodes"].values()}
    for index, item in enumerate(ancestors):
        if item["kind"] in ("RS_NODE", "SCREEN_NODE"):
            continue
        if index == 0 and item["id"] not in records["nodes"]:
            continue
        if item["id"] not in records["nodes"]:
            raise ValueError("unmapped_scene_render_ancestor:" + item["id"])
        record = records["nodes"][item["id"]]
        verify_render_geometry(item, record[0], False)
        matched.append(frame_key(record[0]))
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


def application_viewport(records: dict, ancestors: list) -> tuple:
    """@brief 取应用根的实际场景父坐标系，包含会话内子 Surface 的完整变换。 @param records 场景索引。
    @param ancestors 实际 RS 祖先链。 @return 渲染标识、节点与完整变换，父级裁剪独立保留。
    """
    for ancestor in ancestors[1:]:
        record = records["nodes"].get(ancestor["id"])
        if record is None:
            continue
        node, projection = record
        # 子节点通过实际父矩阵映射，父子尺寸不同不产生缩放；生效范围由独立裁剪链决定。
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
    projection = records["parent_projection"] if window["is_system"] else IDENTITY_PROJECTION
    viewport_id, viewport_node, viewport = ((session_id, node, records["nodes"][session_id][1])
                                           if window["is_system"] else application_viewport(records, ancestors))
    mapping = {"affine": viewport[0], "projection": projection, "canvas": canvas,
               "coordinate_space": "scene" if window["is_system"] else "window"}
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
    native = context["native"][frame_key(node)]
    actual = native_rect(native["$rect"])
    if any(abs(actual[key] - expected[key]) > transform[1] + 0.011 for key in actual):
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
    links, rectangles, projections, matrices, overlays, frames = {}, {}, {}, {}, set(), {}
    pending = [(raw, mapping["projection"], False, None, None, None)]
    while pending:
        node, projection, overlay, parent_key, parent_id, parent_header = pending.pop()
        attrs = node["attributes"]
        node_id = raw_node_key(node, parent_key)
        verify_recycle_ownership(node, native.get(node_id, {}), parent_header)
        overlay = overlay or attrs.get("Description") == "OverlayNode"
        if overlay and "FrameRect" in attrs and attrs.get("LastParentId") != parent_id:
            raise ValueError("overlay_parent_evidence_mismatch:" + node_id)
        if overlay and "FrameRect" not in attrs and node_id not in native:
            # 官方结构节点不输出 LastParentId，Children 已核对其当前 GetParent()。
            if attrs.get("Description") != "Children":
                raise ValueError("overlay_non_layout_parent_unverified:" + node_id)
            overlays.add(node_id)
        if "FrameRect" in attrs:
            if (frame_key(node) != node_id or node_id in frames
                    or (node_id != attrs["ID"] and native.get(parent_key, {}).get("$type") != "ImageAnimator")):
                raise ValueError("raw_frame_identity_or_parent_mismatch:" + node_id)
            frames[node_id] = node
            projection, rect = local_projection(node, projection)
            if node_id not in native or "$rect" not in native[node_id]:
                if not overlay:
                    raise ValueError("native_geometry_frame_missing:" + node_id)
                overlays.add(node_id)
            projections[node_id] = rect
        pending.extend((child, projection, overlay, node_id, attrs["ID"], node.get("source_header", attrs["type"]))
                       for child in node["children"])
        if "FrameRect" not in attrs:
            continue
        link = links[node_id] = client_render(node)
        if link["id"] in rectangles:
            raise ValueError("ambiguous_render_geometry_mapping")
        matrix = (projection[0] if mapping["coordinate_space"] == "scene"
                  else affine.compose(mapping["affine"], projection[0]))
        matrices[link["id"]] = matrix
        local = list(map(number, LOCAL_RECT.fullmatch(attrs["PaintRect without transform"]).groups()))
        rectangles[link["id"]] = affine.bounds(matrix, {"x": 0, "y": 0, "width": local[2], "height": local[3]})
    native_frames = {node_id for node_id, node in native.items() if "$rect" in node}
    raw_frames = set(links) - overlays
    if native_frames != raw_frames:
        native_only = ",".join(sorted(native_frames - raw_frames))
        raw_only = ",".join(sorted(raw_frames - native_frames))
        raise ValueError(f"native_geometry_frame_set_mismatch:native_only={native_only};raw_only={raw_only}")
    return {"links": links, "rectangles": rectangles, "projections": projections,
            "matrices": matrices, "overlays": overlays, "raw_frames": frames}


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


def current_clip_flags(render: dict) -> dict:
    """@brief 只从当前宿主属性读取唯一矩形裁剪开关。 @param render 当前渲染记录。
    @return 各裁剪名称的明确布尔值，缺省为关闭。
    """
    properties = render_current_properties(render)
    result = {}
    for name in ("ClipToBounds", "ClipToFrame"):
        entries = re.findall(r"\b" + name + r"\[([^\]]+)\]", properties)
        if len(entries) > 1 or any(value.lower() not in {"true", "false"} for value in entries):
            raise ValueError("ambiguous_render_clip:" + name)
        result[name] = bool(entries) and entries[0].lower() == "true"
    return result


def current_corner_radii(render: dict, values: dict) -> list:
    """@brief 圆角裁剪使用当前形状，并独立核对存在的修饰器圆角。 @param render 当前记录。
    @param values 自身修饰器集合。 @return 当前四角半径。
    """
    current = re.findall(r"\bCornerRadius\[([^\]]+)\]", render_current_properties(render))
    if len(current) > 1:
        raise ValueError("ambiguous_current_corner_radius")
    printed = re.findall(NUMBER, current[0]) if current else ["0"] * 4
    radii = list(map(number, printed))
    if len(radii) != 4 or min(radii) < 0:
        raise ValueError("invalid_render_corner_radius")
    entries = values.get("CornerRadius")
    if entries is not None:
        if len(set(entries)) != 1:
            raise ValueError("ambiguous_render_corner_radius")
        pending = re.findall(NUMBER, entries[0])
        if len(pending) != 4:
            raise ValueError("invalid_render_corner_radius")
        for first, second in zip(printed, pending, strict=True):
            radius = printed_number_radius(first) + printed_number_radius(second) if current else 0
            if abs(number(first) - number(second)) > radius + 1e-12:
                raise ValueError("current_render_corner_radius_mismatch")
    return radii


def render_clip(render: dict, values: dict, rectangles: dict, matrices: dict, zero_proofs: set) -> dict | None:
    """@brief 解析实际生效的矩形及圆角裁剪。 @param render 渲染节点。 @param values 修饰器。
    @param rectangles 整屏矩形。 @param matrices 完整仿射。 @param zero_proofs 已完成的空子树证明。
    @return 裁剪记录或空。
    """
    flags = current_clip_flags(render)
    bounds_clip, frame_clip = flags["ClipToBounds"], flags["ClipToFrame"]
    if not bounds_clip and not frame_clip:
        return None
    if render["id"] not in rectangles:
        raise ValueError("unmapped_render_clip_node:" + render["id"])
    if frame_clip:
        boxes = current_render_boxes(render)
        if boxes[0] != boxes[1]:
            raise ValueError("unsupported_distinct_frame_clip")
    radii = current_corner_radii(render, values)
    boxes = current_render_boxes(render)
    zero_pending = all(value.split() == ["-inf"] * 4 for value in boxes)
    if zero_pending:
        screen = rectangles[render["id"]]
        if (render["id"] not in zero_proofs or screen["width"] != 0 or screen["height"] != 0
                or not render_has_no_draw_content(render)):
            raise ValueError("unsupported_unapplied_render_clip")
        bounds = [0, 0, 0, 0]
    else:
        bounds = render_bounds(render)
    local = {"x": 0, "y": 0, "width": bounds[2], "height": bounds[3]}
    matrix = matrices[render["id"]]
    return {"source": "RenderService.RSTree", "render_node_id": render["id"],
            "kind": "rounded_rectangle" if any(radii) else "rectangle", "bounds": rectangles[render["id"]],
            "corner_radius_local": radii,
            "bounds_source": "ArkUI.native_zero_area" if zero_pending else "RenderService.current_bounds",
            "local_bounds": local, "local_to_screen": list(matrix),
            "corners": [list(point) for point in affine.corners(matrix, local)],
            "mode": "bounds" if bounds_clip else "frame"}


def content_clip(render: dict, values: dict, rectangles: dict, matrices: dict) -> dict | None:
    """@brief 转换官方内容裁剪矩形，保留内容与子节点作用范围。 @param render 渲染节点。
    @param values 修饰器。 @param rectangles 整屏框。 @param matrices 完整局部到屏幕仿射。 @return 内容裁剪或空。
    """
    entries = values.get("CustomClipToFrame")
    if entries is None:
        return None
    if len(set(entries)) != 1 or render["id"] not in rectangles:
        raise ValueError("ambiguous_content_clip")
    local = list(map(number, re.findall(NUMBER, entries[0])))
    boxes = current_render_boxes(render)
    if len(local) != 4 or boxes[0] != boxes[1]:
        raise ValueError("unsupported_content_clip_frame")
    bounds = list(map(number, boxes[0].split()))
    if len(bounds) != 4 or min(bounds[2:]) <= 0 or local[2] < local[0] or local[3] < local[1]:
        raise ValueError("invalid_content_clip_rectangle")
    matrix = matrices[render["id"]]
    local_rect = {"x": local[0], "y": local[1], "width": local[2] - local[0], "height": local[3] - local[1]}
    return {"source": "RenderService.RSTree.CustomClipToFrame", "render_node_id": render["id"],
            "kind": "rectangle", "bounds": affine.bounds(matrix, local_rect), "local_bounds_ltrb": local,
            "local_to_screen": list(matrix),
            "corners": [list(point) for point in affine.corners(matrix, local_rect)],
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
    verify_render_transform_chain(ancestors, context)
    for render in ancestors:
        if render["kind"] == "RS_NODE":
            continue
        values = verify_modifiers(render)
        clip = render_clip(render, values, context["rectangles"], context["matrices"], context["zero_clip_proofs"])
        if clip:
            clips.append(clip)
        custom = content_clip(render, values, context["rectangles"], context["matrices"])
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
    bounds = list(map(number, current_render_boxes(render)[0].split()))
    if len(bounds) != 4 or min(bounds[2:]) < 0:
        raise ValueError("invalid_render_bounds")
    return bounds


def verify_render_transform_chain(ancestors: list, context: dict) -> None:
    """@brief 用实际渲染父链核对完整复合变换，防止相同包围框掩盖旋转或父链变化。
    @param ancestors 当前节点至根的渲染链。 @param context 原生坐标证据。
    """
    mapping = context["mapping"]
    matrix = affine.interval_exact(mapping["projection"][0] if mapping["coordinate_space"] == "scene"
                                   else mapping["affine"])
    inside_root, radius = False, 0.0
    for render in reversed(ancestors):
        inside_root = inside_root or render["id"] == context["root_render_id"]
        if not inside_root or render["kind"] == "RS_NODE":
            continue
        node = context["raw_by_render"].get(render["id"])
        if node is None:
            raise ValueError("unmapped_render_transform:" + render["id"])
        if (render["id"] != context["root_render_id"]
                and frame_key(node) not in context["overlays"]):
            verify_component_render_parent(node, render, context)
        verify_render_vectors(render, node)
        logical = render["id"] in context["unapplied_render_ids"] or render["id"] in context["zero_clip_proofs"]
        local = (affine.interval_exact(local_projection(node, IDENTITY_PROJECTION)[0][0]) if logical
                 else current_local_matrix(render))
        radius += 0.11 * max(*(abs(value) for entry in matrix[:4] for value in entry), 1)
        matrix = affine.interval_compose(matrix, local)
        expected = context["matrices"][render["id"]]
        bounds = list(map(number, LOCAL_RECT.fullmatch(node["attributes"]["PaintRect without transform"]).groups()))
        size = ([(value, value) for value in bounds[2:]] if logical else
                [printed_interval(value) for value in current_render_boxes(render)[0].split()[2:]])
        original = affine.corners(expected, {"x": 0, "y": 0, "width": bounds[2], "height": bounds[3]})
        actual = affine.interval_corners(matrix, size)
        magnitude = max(*(abs(value) for entry in matrix for value in entry), *(abs(value) for value in expected), 1)
        arithmetic = math.ulp(magnitude) * len(ancestors) * 16
        if any(value < interval[0] - radius - arithmetic or value > interval[1] + radius + arithmetic
               for first, second in zip(actual, original, strict=True)
               for interval, value in zip(first, second, strict=True)):
            raise ValueError("render_transform_chain_mismatch")


def current_local_matrix(render: dict) -> tuple:
    """@brief 用当前字段的实际打印精度构造区间矩阵，不把舍入值当精确值或借用修饰器。
    @param render 当前记录。 @return 当前局部到父坐标系的二维区间矩阵。
    """
    bounds = [printed_interval(value) for value in current_render_boxes(render)[0].split()]
    if len(bounds) != 4:
        raise ValueError("invalid_current_render_bounds")
    scale = current_vector_intervals(render, "Scale")["matrix"]
    if any(low <= 0 <= high for low, high in scale):
        raise ValueError("unsupported_render_zero_scale")
    entries = re.findall(r"\bRotation\[([^\]]*)\]", render_current_properties(render))
    if len(entries) > 1 or (entries and re.fullmatch(NUMBER, entries[0]) is None):
        raise ValueError("ambiguous_render_scalar:Rotation")
    rotation = printed_interval(entries[0]) if entries else (0.0, 0.0)
    return affine.interval_local(bounds, scale, current_vector_intervals(render, "Translate")["matrix"],
                                 current_vector_intervals(render, "Pivot")["matrix"], rotation)


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
    rotation = scalar_modifier(values, "Rotation", 0)
    matrix = affine.compose(context["matrices"][parent["id"]], affine.local(
        dict(zip(("x", "y", "width", "height"), bounds)), scale, translate, pivot, rotation))
    actual = affine.bounds(matrix, {"x": 0, "y": 0, "width": bounds[2], "height": bounds[3]})
    expected = context["rectangles"][render_id]
    tolerance = 0.21 * max(*(abs(value) for value in context["matrices"][parent["id"]][:4]), 1)
    if any(abs(actual[key] - expected[key]) > tolerance for key in actual):
        raise ValueError("overlay_render_projection_mismatch")


def empty_subtree_frames(root: dict, context: dict) -> dict:
    """@brief 唯一关联已观察到的原始及原生零尺寸帧，不推断隐藏节点的逻辑子节点。 @param root 当前组件。
    @param context 本批来源。 @return 按真实渲染标识索引的已观察帧。
    """
    frames, pending = {}, [root]
    while pending:
        node = pending.pop()
        attrs = node["attributes"]
        pending.extend(node["children"])
        if "FrameRect" not in attrs:
            continue
        node_id = frame_key(node)
        render_id = context["links"][node_id]["id"]
        native = context["native"].get(node_id)
        if (native is None or node_id in context["overlays"]
                or native["$type"] != attrs["type"].split(",", 1)[0]
                or (attrs.get("compid") and native.get("compid") != attrs["compid"])):
            raise ValueError("empty_render_subtree_frame_unverified:" + render_id)
        match = LOCAL_RECT.fullmatch(attrs["PaintRect without transform"])
        local = list(map(number, match.groups())) if match else []
        bounds = native_rect(native["$rect"])
        if len(local) != 4 or local[2:] != [0, 0] or any(bounds[key] != 0 for key in ("width", "height")):
            raise ValueError("empty_render_subtree_nonzero_geometry:" + render_id)
        frames[render_id] = node
    return frames


def actual_render_subtree(root_id: str, nodes: dict) -> list[dict]:
    """@brief 验证实际渲染子树与打印的完整子节点计数，不用原始组件树猜测挂载关系。 @param root_id 根标识。
    @param nodes 正式渲染索引。 @return 按实际父关系遍历的完整子树。
    """
    children = {}
    for node in nodes.values():
        children.setdefault(node["parent"], []).append(node["id"])
    result, pending, seen = [], [root_id], set()
    while pending:
        node_id = pending.pop()
        if node_id in seen or node_id not in nodes:
            raise ValueError("invalid_render_parent_chain")
        seen.add(node_id)
        node = nodes[node_id]
        descendants = children.get(node_id, [])
        declared = re.findall(r"\bsortedChildren:\s*(\d+)\(", node["line"])
        if (node["children_count"] != len(descendants)
                or (declared != [str(len(descendants))] if descendants else declared not in ([], ["0"]))):
            raise ValueError("empty_render_subtree_children_unverified:" + node_id)
        result.append(node)
        pending.extend(reversed(descendants))
    return result


def verify_component_render_parent(node: dict, render: dict, context: dict) -> None:
    """@brief 穿过非布局节点及已证明省略的恒等帧，核对实际 RS 父节点。 @param node 原始帧。
    @param render 已挂载帧。 @param context 原生父链和唯一渲染关联。
    """
    parent = context["parents"][frame_key(node)]
    while parent is not None:
        expected = context["links"].get(parent)
        if expected is not None:
            if expected["id"] in context["render"]:
                if render["parent"] != expected["id"]:
                    raise ValueError("empty_render_subtree_parent_mismatch:" + render["id"])
                return
            record = (context["raw_frames"][parent], None)
            verify_omitted_scene_frame(record, context["render"], context["detached_render"])
        parent = context["parents"][parent]
    raise ValueError("empty_render_subtree_parent_mismatch:" + render["id"])


def verify_empty_render_bounds(render: dict, node: dict) -> None:
    """@brief 空帧只接受一致的未生效双框或有限零尺寸双框，不忽略独立 Frame 冲突。 @param render 渲染帧。
    @param node 已核验原始零尺寸帧。
    """
    boxes = current_render_boxes(render)
    if all(value.split() == ["-inf"] * 4 for value in boxes):
        return
    local = LOCAL_RECT.fullmatch(node["attributes"]["PaintRect without transform"])
    expected = list(map(number, local.groups()))
    for value in boxes:
        bounds = list(map(number, value.split()))
        if len(bounds) != 4 or bounds[2:] != [0, 0]:
            raise ValueError("empty_render_subtree_nonzero_render_geometry:" + render["id"])
        if any(abs(first - second) > 0.11 for first, second in zip(bounds, expected, strict=True)):
            raise ValueError("empty_render_subtree_frame_mismatch:" + render["id"])


def verify_empty_render_modifiers(render: dict, node: dict, context: dict) -> None:
    """@brief 验证空帧局部变换及有独立来源的零面积矩形裁剪，不以无穷框作为几何。 @param render 渲染帧。
    @param node 原始帧。 @param context 已核验的完整来源。
    """
    verify_render_geometry(render, node, True)
    verify_empty_render_bounds(render, node)
    values = verify_modifiers(render)
    for name, entries in values.items():
        if "clip" not in name.lower():
            continue
        if name not in {"ClipToBounds", "ClipToFrame"}:
            raise ValueError("unsupported_empty_render_clip:" + render["id"])
        if all(entry.lower() == "false" for entry in entries):
            continue
        verify_zero_area_clip(render, node, context, name)
    flags = re.findall(r"\b(ClipToBounds|ClipToFrame)\[([^\]]+)\]", render_current_properties(render))
    for name, value in flags:
        if value.lower() != "false":
            verify_zero_area_clip(render, node, context, name)


def verify_zero_area_clip(render: dict, node: dict, context: dict, name: str) -> None:
    """@brief 完整空子树须核验当前矩形裁剪标记及同进程客户端零框。 @param render 当前记录。
    @param node 已验证零帧。 @param context 本批来源。 @param name 矩形裁剪名称。
    """
    current = re.findall(r"\b" + name + r"\[([^\]]+)\]", render_current_properties(render))
    owned = owned_render_modifier(render, name, context["process_id"])
    if (render["id"] not in context["render"] or [value.lower() for value in current] != ["true"]
            or owned is None or owned.lower() != "true"):
        raise ValueError("unsupported_empty_render_clip:" + render["id"])
    context["client_zero_clip_sources"][render["id"]] = verify_zero_client_bounds(node, render, context["process_id"])


def verify_zero_client_bounds(node: dict, render: dict, process_id: int) -> dict:
    """@brief 唯一客户端自身行必须有同进程相同上下文及真实零双框。 @param node 已关联的零帧。
    @param render 同身份真实 RS 记录。 @param process_id 已核验进程。 @return 独立客户端来源身份。
    """
    rows = list(CLIENT_NODE.finditer(node_property_text(node)))
    if len(rows) != 1:
        raise ValueError("empty_render_clip_client_identity_missing")
    row = rows[0][0]
    tokens = re.findall(r"\btoken:\s*(\d+)", row)
    contexts = re.findall(r"\brsUIContext token is\s+(\d+)", row)
    header = re.split(r"DrawCmdModifiers|OtherModifiers:", render["line"], maxsplit=1)[0]
    service_tokens = re.findall(r"\bRSUIContextToken:\s*\[\s*(\d+)\s*\]", header)
    if (len(tokens) != 1 or tokens != contexts or tokens != service_tokens
            or int(tokens[0]) >> 32 != process_id or rows[0][2] != render["id"]):
        raise ValueError("empty_render_clip_client_context_mismatch")
    local = list(map(number, LOCAL_RECT.fullmatch(node["attributes"]["PaintRect without transform"]).groups()))
    for name in ("Bounds", "Frame"):
        entries = re.findall(r"\b" + name + r"\[([^\]]+)\]", row)
        if len(entries) != 1:
            raise ValueError("empty_render_clip_client_bounds_missing")
        values = list(map(number, entries[0].split()))
        if len(values) != 4 or any(abs(a - b) > 0.11 for a, b in zip(values, local, strict=True)):
            raise ValueError("empty_render_clip_client_bounds_mismatch")
    return {"render_node_id": rows[0][2], "ui_context_token": tokens[0], "process_id": process_id,
            "render_service_ui_context_token": service_tokens[0],
            "bounds": local, "frame": local, "source": "ArkUI.client_render_own_row"}


def empty_frame_partitions(frames: dict, actual: list, context: dict) -> tuple:
    """@brief 区分实际挂载、明确未挂载及有懒加载身份的未物化帧。 @param frames 已观察零尺寸帧。
    @param actual 实际挂载子树。 @param context 本批来源。 @return 未挂载记录与未物化标识。
    """
    mounted = {item["id"] for item in actual}
    if not mounted.issubset(frames):
        raise ValueError("empty_render_subtree_membership_mismatch:" + actual[0]["id"])
    detached_ids = set(frames) - mounted
    detached, unmaterialized = {}, []
    for render_id in sorted(detached_ids):
        if render_id in context["render"]:
            raise ValueError("empty_render_subtree_attachment_unverified:" + render_id)
        if render_id not in context["detached_render"]:
            node = frames[render_id]
            if not client_render(node)["lazy"]:
                raise ValueError("empty_render_subtree_attachment_unverified:" + render_id)
            verify_omitted_scene_frame((node, None), context["render"], context["detached_render"])
            unmaterialized.append(render_id)
            continue
        record = context["detached_render"][render_id]
        if record["parent"] is not None and record["parent"] not in detached_ids:
            raise ValueError("empty_render_subtree_detached_parent_mismatch:" + render_id)
        subtree = actual_render_subtree(render_id, context["detached_render"])
        if any(item["id"] not in detached_ids for item in subtree):
            raise ValueError("empty_render_subtree_membership_mismatch:" + render_id)
        detached[render_id] = record
    return detached, unmaterialized


def verify_detached_empty_parent(node: dict, render: dict, context: dict) -> None:
    """@brief 保留未挂载森林根边界，非根记录仍核对原生帧父链。 @param node 原始帧。
    @param render 明确未挂载帧。 @param context 原生父链及挂载来源。
    """
    if render["parent"] is None:
        return
    parent = context["parents"][frame_key(node)]
    while parent is not None:
        expected = context["links"].get(parent)
        if expected is not None:
            if expected["id"] in context["render"] or expected["id"] in context["detached_render"]:
                if render["parent"] != expected["id"]:
                    raise ValueError("empty_render_subtree_detached_parent_mismatch:" + render["id"])
                return
            record = (context["raw_frames"][parent], None)
            verify_omitted_scene_frame(record, context["render"], context["detached_render"])
        parent = context["parents"][parent]
    raise ValueError("empty_render_subtree_detached_parent_mismatch:" + render["id"])


def empty_render_subtree(node: dict, context: dict) -> tuple:
    """@brief 仅对完整原始、原生和实际 RS 空子树生成无绘制证明，失败仍绑定当前组件。 @param node 当前帧。
    @param context 本批来源。 @return 挂载、未挂载及未物化标识。
    """
    root_id = context["links"][frame_key(node)]["id"]
    render = context["render"].get(root_id)
    match = LOCAL_RECT.fullmatch(node["attributes"]["PaintRect without transform"])
    local = list(map(number, match.groups())) if match else []
    if (render is None or len(local) != 4 or local[2:] != [0, 0] or not render_has_no_draw_content(render)):
        return [], [], []
    bounds = current_render_boxes(render)
    if any(value.split() != ["-inf"] * 4 for value in bounds):
        return [], [], []
    pending, frame_child = list(node["children"]), False
    while pending and not frame_child:
        child = pending.pop()
        frame_child = "FrameRect" in child["attributes"]
        pending.extend(child["children"])
    if render["children_count"] == 0 and not frame_child:
        return [], [], []
    frames = empty_subtree_frames(node, context)
    actual = actual_render_subtree(root_id, context["render"])
    detached, unmaterialized = empty_frame_partitions(frames, actual, context)
    for item in actual:
        if not render_has_no_draw_content(item):
            raise ValueError("empty_render_subtree_draw_content_unverified:" + item["id"])
        verify_component_render_parent(frames[item["id"]], item, context)
        verify_empty_render_modifiers(item, frames[item["id"]], context)
    for item in detached.values():
        if not render_has_no_draw_content(item):
            raise ValueError("empty_render_subtree_draw_content_unverified:" + item["id"])
        verify_detached_empty_parent(frames[item["id"]], item, context)
        verify_empty_render_modifiers(item, frames[item["id"]], context)
    return [item["id"] for item in actual], list(detached), unmaterialized


def frame_geometry(node: dict, context: dict, inherited: list) -> dict:
    """@brief 生成来自原生几何的已验证框。 @param node 原始节点。 @param context 上下文。
    @param inherited 祖先裁剪。 @return 带证据的几何。
    """
    node_id = frame_key(node)
    native = context["native"].get(node_id, {})
    link = context["links"][node_id]
    no_draw, no_draw_nodes, no_draw_detached, no_draw_unmaterialized = False, [], [], []
    bounds = context["rectangles"][link["id"]]
    if node_id in context["overlays"]:
        verify_overlay_render(node_id, context)
    hidden = native.get("visible") == "false" or native.get("active") == "false"
    detached = link["id"] in context["detached_render"]
    render = context["render"].get(link["id"])
    transparent = zero_alpha_ancestor(node, context) if render is not None else None
    pending_state = transparent_render_state(render, node, context["process_id"]) if transparent else None
    if pending_state:
        context["unapplied_render_ids"].add(link["id"])
    unmaterialized = render is None and not detached and link["lazy"]
    if render is not None and not pending_state:
        no_draw_nodes, no_draw_detached, no_draw_unmaterialized = empty_render_subtree(node, context)
        no_draw = verify_render_geometry(context["render"][link["id"]], node, bool(no_draw_nodes))
        context["zero_clip_proofs"].update(no_draw_nodes or ([link["id"]] if no_draw else []))
    if detached:
        clips = []
    else:
        clips = inherited if hidden and link["id"] not in context["render"] else frame_clips(link, context, inherited)
    clipped = None if detached else bounds
    for clip in clips:
        clipped = intersection(clipped, clip["bounds"])
    rendered = not (hidden or detached or no_draw or transparent or unmaterialized)
    state = {"transparent": transparent, "pending_state": pending_state, "unmaterialized": unmaterialized}
    return {"status": "verified", "screen_bounds": bounds, "rectangular_clip_bounds": clipped,
            "clip_chain": clips, "clipping_status": "not_applicable" if detached else "verified",
            "rendered": rendered, "native_window_rect": native.get("$rect"),
            "render_state": "rendered" if rendered else "not_rendered",
            "render_state_source": ("RenderService.zero_alpha_ancestor" if transparent else
                                    "RenderService.allNodeNotOnTree" if detached else
                                    "ArkUI.lazy_unmaterialized" if unmaterialized else
                                    "RenderService.no_draw_content" if no_draw else "ArkUI_and_RenderService"),
            "source": geometry_source(node_id, context, state,
                                      (no_draw_nodes or ([link["id"]] if no_draw else []),
                                       no_draw_detached, no_draw_unmaterialized))}


def geometry_source(node_id: str, context: dict, state: dict, no_draw: tuple) -> dict:
    """@brief 保存完整来源、未应用状态及三个互斥挂载集合。 @param node_id 当前身份。
    @param context 同批上下文。 @param state 已核验的状态。 @param no_draw 挂载、未挂载和未物化标识。
    @return 几何来源。
    """
    link = context["links"][node_id]
    overlay = node_id in context["overlays"]
    original_id = str(context["raw_frames"][node_id]["attributes"]["ID"])
    return {**({"zero_alpha_ancestor": state["transparent"],
                "render_geometry_state": state["pending_state"] or "applied"} if state["transparent"] else {}),
            **({"zero_area_clip_client": context["client_zero_clip_sources"][link["id"]]}
               if link["id"] in context["client_zero_clip_sources"] else {}),
            "method": "ArkUI.PaintRectAncestorComposition",
            "commands": ["WindowManagerService.-default.-all", "WindowManagerService.-allInfoWithParamConfig",
                         "RenderService.RSTree", "RenderService.allNodeNotOnTree"], "node_id": original_id,
            **({"node_key": node_id} if node_id != original_id else {}),
            "native_rect_cross_checked": not overlay, "window_mapping": context["window_mapping"],
            "overlay_render_cross_checked": overlay,
            "native_traversal_omission": ("UINode.DumpSimplifyTreeWithParamConfig.excludes_OverlayNode"
                                         if overlay else None),
            "render_node_id": link["id"], "no_draw_render_node_ids": no_draw[0],
            "no_draw_detached_render_node_ids": no_draw[1], "no_draw_unmaterialized_render_node_ids": no_draw[2],
            "render_node_materialized": link["id"] in context["render"] or link["id"] in context["detached_render"],
            "render_node_on_screen_tree": link["id"] in context["render"]}


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
    return {"native": native, "parents": parents, "mapping": mapping, "process_id": window["process_id"],
            "zero_clip_proofs": set(),
            "unapplied_render_ids": set(),
            "client_zero_clip_sources": {},
            **node_evidence, "render": render, "detached_render": detached,
            "rectangles": {**scene["rectangles"], **node_evidence["rectangles"]},
            "matrices": {**scene["matrices"], **node_evidence["matrices"]},
            "raw_by_render": {**{key: record[0] for key, record in scene["nodes"].items()},
                              **{link["id"]: node_evidence["raw_frames"][key] for key, link in links.items()}},
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
            node_id = raw_node_key(node, parent_id)
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
