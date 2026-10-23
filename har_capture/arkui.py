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


def property_content(line: str) -> str:
    """@brief 移除本层属性包装，不移除远端组件树自己的层级。 @param line 本层文本。 @return 属性正文。"""
    content = line.strip()
    return content[1:].lstrip() if content.startswith("|") else content


def embedded_end(lines: list[tuple[str, str, int]], start: int, prefix: str) -> int:
    """@brief 按官方属性包装和树缩进确定远端响应边界。 @param lines 原文、当前层文本及全局行号。
    @param start 起始标记位置。 @param prefix 当前属性包装。 @return 嵌入块后的本层位置。
    """
    root_indent = None
    for index in range(start + 1, len(lines)):
        view = lines[index][1]
        if not view.startswith(prefix):
            return index
        content = view[len(prefix):]
        if content.rstrip() == "UI Extension info:" or content.startswith("------------start print rsNode"):
            return index
        match = NODE_HEADER.fullmatch(content.rstrip("\r\n"))
        if match is not None and root_indent is None:
            root_indent = len(match["indent"])
        if (root_indent is not None and content.strip() and not NODE_MARKER.match(content)
                and len(content) - len(content.lstrip(" ")) < root_indent + 2):
            return index
    return len(lines)


def embedded_tree(lines: list[tuple[str, str, int]], start: int, outer: str) -> tuple[dict, int]:
    """@brief 保留独立 UIExtension 响应的完整原文、前导和树森林。 @param lines 当前宿主原始行。
    @param start 标记位置。 @param outer 已移除的外层包装。 @return 嵌入块及后续位置。
    """
    _, view, number = lines[start]
    prefix = view[:view.index("UI Extension info:")]
    end = embedded_end(lines, start, prefix)
    block = lines[start:end]
    roots = parse_source_lines([row[0] for row in block], number - 1, outer + prefix, allow_empty=True)
    return {"source_line": number, "raw": "".join(row[0] for row in block), "roots": roots}, end


def node_property_text(node: dict) -> str:
    """@brief 依据明确嵌入块的全局行号读取本层原文，不修改完整原始文本。
    @param node 已解析节点。 @return 不包含独立远端响应的本层属性原文。
    """
    lines = node["raw"].splitlines(keepends=True)
    result, previous = [], 0
    for block in node.get("embedded_trees", []):
        start = block["source_line"] - node["source_line"] - 1
        end = start + len(block["raw"].splitlines(keepends=True))
        if start < previous or end > len(lines) or "".join(lines[start:end]) != block["raw"]:
            fail("Embedded ArkUI source range differs from its node raw text")
        result.extend(lines[previous:start])
        previous = end
    return "".join(result + lines[previous:])


def finish_node(node: dict, lines: list[tuple[str, str, int]], prefix: str) -> None:
    """@brief 保存全部原文，只将当前层属性写入宿主，并独立解析嵌入树。
    @param node 当前节点。 @param lines 原文、当前层文本及全局行号。 @param prefix 外层包装。
    """
    node["raw"] = "".join(row[0] for row in lines)
    index = 0
    while index < len(lines):
        content = property_content(lines[index][1])
        if content == "UI Extension info:":
            block, index = embedded_tree(lines, index, prefix)
            node.setdefault("embedded_trees", []).append(block)
            continue
        if NODE_MARKER.match(content):
            fail(f"Embedded ArkUI tree has no UI Extension info marker at line {lines[index][2]}")
        index += 1
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


def source_rows(lines: list[str], first_line: int, prefix: str) -> list[tuple[str, str, int]]:
    """@brief 保留原始行并建立当前包装层的解析视图。 @param lines 同一来源原始行。
    @param first_line 首行前全局行号。 @param prefix 明确外层包装。 @return 原文、当前层文本及全局行号。
    """
    rows = []
    for number, original in enumerate(lines, first_line + 1):
        if not original.startswith(prefix):
            fail(f"Invalid embedded ArkUI property prefix at line {number}")
        rows.append((original, original[len(prefix):], number))
    return rows


def parse_source_lines(lines: list[str], first_line: int, prefix: str, allow_empty: bool = False) -> list[dict]:
    """@brief 在独立包装层内解析森林，原文及全局行号始终来自同一输入。
    @param lines 原始行。 @param first_line 首行前的全局行号。 @param prefix 要移除的外层包装。
    @param allow_empty 官方远端响应是否可仅含诊断文字。 @return 当前层全部根。
    """
    roots = []
    root_indent = None
    current = None
    property_lines = []
    stack = []
    rows = source_rows(lines, first_line, prefix)
    skip_until = 0
    for position, (line, view, line_number) in enumerate(rows):
        if current is not None and property_content(view) == "UI Extension info:" and position >= skip_until:
            local = view[:view.index("UI Extension info:")]
            skip_until = embedded_end(rows, position, local)
        if position < skip_until:
            property_lines.append((line, view, line_number))
            continue
        match = NODE_HEADER.fullmatch(view.rstrip("\r\n"))
        if not match:
            if NODE_MARKER.match(view):
                fail(f"Malformed ArkUI node header at line {line_number}")
            if current is not None:
                property_lines.append((line, view, line_number))
            continue
        if current is not None:
            finish_node(current, property_lines, prefix)
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
    if not roots and not allow_empty:
        fail("ArkUI dump contains no component tree")
    if current is not None:
        finish_node(current, property_lines, prefix)
    return roots


def parse_forest(text: str) -> list[dict]:
    """@brief 完整解析同级根森林，所有根使用相同缩进且子节点严格增加两空格。
    @param text 未经改写的原文。 @return 保留原始顺序、属性和行号的全部根。
    """
    return parse_source_lines(text.splitlines(keepends=True), 0, "")


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
