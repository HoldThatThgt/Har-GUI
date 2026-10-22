"""@brief 按系统会话标识从 ArkUI 多根原文中提取对应窗口的组件子树。"""

import re

from har_capture.hdc import fail

NODE_HEADER = re.compile(rb"^( *)\|-> (\S(?:.*?\S)?) childSize:(\d+)\s*$")
IDENTITY_PROPERTY = re.compile(rb"^\s*\|?\s*(AccessibilityId|compid|ID):\s*(.*?)\s*$")


def index_nodes(raw: bytes) -> tuple[list[bytes], list[dict]]:
    """@brief 索引节点标题的原始位置。 @param raw 未改写的采集字节。 @return 原始行与节点索引。"""
    lines = raw.splitlines(keepends=True)
    nodes = []
    offset = 0
    for line_index, line in enumerate(lines):
        match = NODE_HEADER.fullmatch(line.rstrip(b"\r\n"))
        if match:
            nodes.append({
                "line_index": line_index,
                "offset": offset,
                "indent": len(match[1]),
                "tag": match[2].split(b",", 1)[0],
            })
        offset += len(line)
    return lines, nodes


def read_identity(lines: list[bytes], start: int, end: int) -> dict:
    """@brief 读取节点自身的关联属性。 @param lines 原始行。 @param start 起点。 @param end 终点。
    @return 属性值列表，重复项不合并。
    """
    attributes = {}
    for line in lines[start + 1:end]:
        match = IDENTITY_PROPERTY.fullmatch(line.rstrip(b"\r\n"))
        if match:
            attributes.setdefault(match[1], []).append(match[2])
    return attributes


def find_target(lines: list[bytes], nodes: list[dict], window_id: int, accessibility_id: int) -> tuple[int, int]:
    """@brief 找到唯一会话节点。 @param lines 原始行。 @param nodes 节点索引。 @param window_id 窗口标识。
    @param accessibility_id 会话提供的无障碍节点标识。 @return 节点索引与元素标识。
    """
    candidates = []
    expected_id = str(accessibility_id).encode("ascii")
    expected_component = f"session{window_id}".encode("ascii")
    for index, node in enumerate(nodes):
        if node["tag"] != b"WindowScene":
            continue
        end = nodes[index + 1]["line_index"] if index + 1 < len(nodes) else len(lines)
        attributes = read_identity(lines, node["line_index"], end)
        ids = attributes.get(b"AccessibilityId", [])
        components = attributes.get(b"compid", [])
        if expected_id not in ids or expected_component not in components:
            continue
        node_ids = attributes.get(b"ID", [])
        if ids != [expected_id] or components != [expected_component]:
            fail(f"Ambiguous ArkUI identity properties for window {window_id}")
        if len(node_ids) != 1 or not re.fullmatch(rb"-?\d+", node_ids[0]):
            fail(f"Missing or invalid ArkUI node ID for window {window_id}")
        candidates.append((index, int(node_ids[0])))
    if len(candidates) != 1:
        fail(f"Expected one ArkUI WindowScene for window {window_id}, found {len(candidates)}")
    return candidates[0]


def node_slice(raw: bytes, lines: list[bytes], nodes: list[dict], index: int) -> tuple[bytes, dict]:
    """@brief 使用已建索引截取节点完整原文，避免为共享来源重复建索引。 @param raw 多根原文。
    @param lines 原始行。 @param nodes 节点索引。 @param index 目标索引。 @return 子树字节与原始位置。
    """
    target = nodes[index]
    end_offset = len(raw)
    end_line = len(lines)
    for node in nodes[index + 1:]:
        if node["indent"] <= target["indent"]:
            end_offset = node["offset"]
            end_line = node["line_index"]
            break
    selection = {
        "byte_offset": target["offset"],
        "byte_length": end_offset - target["offset"],
        "start_line": target["line_index"] + 1,
        "end_line": end_line,
    }
    return raw[target["offset"]:end_offset], selection


def select_system_tree(raw: bytes, window: dict, session: dict) -> tuple[bytes, dict]:
    """@brief 精确截取系统窗口的原始组件子树。 @param raw 多根原文。 @param window 窗口信息。
    @param session 会话信息。 @return 原始字节切片和来源位置，行号从一开始且结束行包含在内。
    """
    lines, nodes = index_nodes(raw)
    window_id, accessibility_id = window["window_id"], session["ui_node_id"]
    index, node_id = find_target(lines, nodes, window_id, accessibility_id)
    subtree, selection = node_slice(raw, lines, nodes, index)
    selection.update(accessibility_id=accessibility_id, component_id=f"session{window_id}", node_id=node_id)
    return subtree, selection
