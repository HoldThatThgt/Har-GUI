"""@brief 解析 ArkUI 原始文本组件树，保留节点属性和原始属性文本。"""

import re

from har_capture.hdc import fail

NODE_HEADER = re.compile(r"^(?P<indent> *)\|-> (?P<tag>\S(?:.*?\S)?) childSize:(?P<count>\d+)\s*$")
NODE_MARKER = re.compile(r"^[ \t]*\|->")


def create_node(match: re.Match, line_number: int) -> dict:
    """@brief 创建节点。 @param match 节点标题。 @param line_number 原始行号。 @return 节点。"""
    return {
        "attributes": {"type": match["tag"]},
        "source_attributes": {},
        "children": [],
        "raw": "",
        "source_line": line_number,
        "declared_child_count": int(match["count"]),
    }


def append_property(attributes: dict, key: str, value: str) -> None:
    """@brief 保留同名属性的所有值。 @param attributes 属性字典。 @param key 名称。 @param value 值。"""
    if key not in attributes:
        attributes[key] = value
    elif isinstance(attributes[key], list):
        attributes[key].append(value)
    else:
        attributes[key] = [attributes[key], value]


def finish_node(node: dict, lines: list[str]) -> None:
    """@brief 保存原文并解析属性。 @param node 当前节点。 @param lines 本节点原始属性行。"""
    node["raw"] = "".join(lines)
    for line in lines:
        content = line.strip()
        if content.startswith("|"):
            content = content[1:].lstrip()
        if ":" not in content:
            continue
        key, value = content.split(":", 1)
        key = key.strip()
        if key:
            append_property(node["source_attributes"], key, value.strip())
    node["attributes"].update({key: value for key, value in node["source_attributes"].items() if key != "type"})


def attach_node(stack: list[tuple[int, dict]], node: dict, indent: int) -> None:
    """@brief 按两空格层级连接节点。 @param stack 祖先栈。 @param node 节点。 @param indent 缩进。"""
    while stack and indent <= stack[-1][0]:
        stack.pop()
    if not stack:
        fail(f"Multiple ArkUI roots or invalid root indentation at line {node['source_line']}")
    if indent != stack[-1][0] + 2:
        fail(f"Invalid ArkUI node indentation at line {node['source_line']}")
    stack[-1][1]["children"].append(node)
    stack.append((indent, node))


def parse_arkui(text: str) -> dict:
    """@brief 解析单根 ArkUI 文本树。 @param text 未经改写的原文。 @return 单根组件树。"""
    root = None
    current = None
    property_lines = []
    stack = []
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        match = NODE_HEADER.fullmatch(line.rstrip("\r\n"))
        if not match:
            if NODE_MARKER.match(line):
                fail(f"Malformed ArkUI node header at line {line_number}")
            if current is not None:
                property_lines.append(line)
            continue
        if current is not None:
            finish_node(current, property_lines)
        current = create_node(match, line_number)
        property_lines = []
        indent = len(match["indent"])
        if root is None:
            root = current
            stack.append((indent, current))
        else:
            attach_node(stack, current, indent)
    if root is None:
        fail("ArkUI dump contains no component tree")
    finish_node(current, property_lines)
    return root


def count_arkui_nodes(root: dict) -> int:
    """@brief 迭代统计组件数。 @param root 组件树根节点。 @return 节点数量。"""
    count = 0
    pending = [root]
    while pending:
        node = pending.pop()
        count += 1
        pending.extend(node["children"])
    return count
