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


def parse_forest(text: str) -> list[dict]:
    """@brief 完整解析同级根森林，所有根使用相同缩进且子节点严格增加两空格。
    @param text 未经改写的原文。 @return 保留原始顺序、属性和行号的全部根。
    """
    roots = []
    root_indent = None
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
        if root_indent is None:
            root_indent = indent
        if indent == root_indent:
            roots.append(current)
            stack.clear()
            stack.append((indent, current))
        else:
            attach_node(stack, current, indent)
    if not roots:
        fail("ArkUI dump contains no component tree")
    finish_node(current, property_lines)
    return roots


def parse_arkui(text: str) -> dict:
    """@brief 要求 ArkUI 窗口或已选择子树恰有一个根，不把其他根丢弃或合成父节点。
    @param text 未经改写的原文。 @return 唯一组件树。
    """
    roots = parse_forest(text)
    if len(roots) != 1:
        fail(f"ArkUI dump must contain exactly one component tree; found {len(roots)}")
    return roots[0]


def count_arkui_nodes(root: dict) -> int:
    """@brief 迭代统计组件数。 @param root 组件树根节点。 @return 节点数量。"""
    count = 0
    pending = [root]
    while pending:
        node = pending.pop()
        count += 1
        pending.extend(node["children"])
    return count
