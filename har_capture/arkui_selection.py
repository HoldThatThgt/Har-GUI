"""@brief 按系统会话标识从 ArkUI 多根原文中提取对应窗口的组件子树。"""

import copy
import json
import re
from pathlib import Path

from har_capture.hdc import fail
from har_capture.coordinates import node_key, subtree_index, client_render
from har_capture.arkui import parse_arkui, parse_forest, count_arkui_nodes
from har_capture.metadata import file_info, verify_window_response
from har_capture.geometry_capture import parse_geometry_response, verify_native_application

NODE_HEADER = re.compile(rb"^( *)\|-> (\S(?:.*?\S)?) childSize:(\d+)\s*$")


def index_nodes(raw: bytes) -> tuple[list[bytes], list[dict]]:
    """@brief 索引节点标题的原始位置。 @param raw 未改写的采集字节。 @return 原始行与节点索引。"""
    lines = raw.splitlines(keepends=True)
    local_lines = set()
    pending = parse_forest(raw.decode("utf-8"))
    while pending:
        node = pending.pop()
        local_lines.add(node["source_line"])
        pending.extend(node["children"])
    nodes = []
    offset = 0
    for line_index, line in enumerate(lines):
        match = NODE_HEADER.fullmatch(line.rstrip(b"\r\n"))
        if match and line_index + 1 in local_lines:
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
    node = parse_arkui(b"".join(lines[start:end]).decode("utf-8"))
    attributes = {}
    for key in ("AccessibilityId", "compid", "ID"):
        if key not in node["source_attributes"]:
            continue
        value = node["source_attributes"][key]
        values = value if isinstance(value, list) else [value]
        attributes[key.encode("ascii")] = [item.encode("utf-8") for item in values]
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


def forest_occurrences(roots: list[dict]) -> dict:
    """@brief 索引全部原文节点位置，不合并重复引用。 @param roots 完整森林。 @return 身份到原文位置列表。"""
    result = {}
    for root in roots:
        pending = [(root, None)]
        while pending:
            node, parent = pending.pop()
            attrs = node["attributes"]
            tag = attrs["type"].split(",", 1)[0].strip()
            if not isinstance(attrs.get("ID"), str) or not re.fullmatch(r"-?\d+", attrs["ID"]):
                fail(f"ArkUI full source has an invalid node identity at line {node['source_line']}")
            key = node_key(attrs["ID"], tag, parent)
            result.setdefault(key, []).append({"node": node, "parent": parent, "root_line": root["source_line"]})
            pending.extend((child, key) for child in reversed(node["children"]))
    return result


def cache_identity(value: str) -> list[tuple[str, str, str]]:
    """@brief 严格解析官方缓存三元组，拒绝残余文本。 @param value 缓存原文。 @return 类型、节点及无障碍身份。"""
    if not isinstance(value, str):
        fail("ArkUI cached items must be an explicit string")
    entry = r"\[([^\[\],]+),(-?\d+),(-?\d+)\]"
    rows = re.findall(entry, value)
    if re.fullmatch(r"\[(?:" + entry + r"(?:," + entry + r")*)?\]", value) is None:
        fail("ArkUI cached items contain an invalid identity list")
    if len(rows) != len(set(rows)):
        fail("ArkUI cached items contain duplicate identities")
    return rows


def select_occurrence(key: str, native: dict, parents: dict, occurrences: dict) -> dict:
    """@brief 依照实际父节点选择唯一原文位置。 @param key 节点键。 @param native 原生索引。
    @param parents 原生父索引。 @param occurrences 全部原文位置。 @return 唯一原文记录。
    """
    matches = []
    for record in occurrences.get(key, []):
        attrs = record["node"]["attributes"]
        if attrs["type"].split(",", 1)[0].strip() != native[key]["$type"]:
            continue
        if attrs.get("Description") == "BrokenChildren":
            continue
        if record["parent"] == parents[key]:
            matches.append(record)
        elif record["parent"] is None and parents[key] is not None:
            triple = (native[key]["$type"], attrs["ID"], attrs.get("AccessibilityId"))
            if triple in cache_identity(native[parents[key]].get("$CachedItems", "[]")):
                matches.append(record)
    if len(matches) != 1:
        fail(f"ArkUI native node {key} has {len(matches)} proven raw occurrences")
    return matches[0]


def verify_cached_parent(key: str, selected: dict, native: dict, parents: dict) -> None:
    """@brief 核验分离缓存根在两份原文中的同一所有者。 @param key 节点键。 @param selected 已选原文。
    @param native 原生索引。 @param parents 原生父索引。
    """
    record, parent = selected[key], parents[key]
    if record["parent"] == parent:
        return
    attrs, owner = record["node"]["attributes"], selected[parent]["node"]["attributes"]
    triple = (native[key]["$type"], attrs["ID"], attrs.get("AccessibilityId"))
    raw_cache = cache_identity(owner.get("CachedItems", "[]"))
    native_cache = cache_identity(native[parent].get("$CachedItems", "[]"))
    owner_header = owner["type"]
    if (raw_cache != native_cache or triple not in raw_cache or attrs.get("Parent") != owner_header
            or ("FrameRect" in attrs and attrs.get("LastParentId") != owner["ID"])):
        fail(f"ArkUI cached root {key} has conflicting raw/native ownership")


def verify_broken_reference(key: str, chosen: dict, alias: dict) -> None:
    """@brief 核验官方 BrokenChildren 引用仅重复同一实体。 @param key 节点键。
    @param chosen 实际位置。 @param alias 重复引用。
    """
    source, repeated = chosen["node"], alias["node"]
    ignored = {"Description", "DumpDepth"}
    first = {k: v for k, v in source["attributes"].items() if k not in ignored}
    second = {k: v for k, v in repeated["attributes"].items() if k not in ignored}
    if (repeated["attributes"].get("Description") != "BrokenChildren" or repeated["children"]
            or source["attributes"].get("Description") != "Children" or first != second
            or source["declared_child_count"] != repeated["declared_child_count"]):
        fail(f"ArkUI node {key} has an unproven duplicate raw occurrence")
    if "FrameRect" in source["attributes"]:
        try:
            if client_render(source) != client_render(repeated):
                fail(f"ArkUI node {key} has conflicting render identities in its duplicate reference")
        except ValueError as error:
            fail(f"ArkUI node {key} has an invalid duplicate render identity: {error}")


def verify_selected_children(key: str, selected: dict, native: dict, parents: dict) -> list:
    """@brief 保留每个普通子节点并核验缓存之外的顺序完整性。 @param key 节点键。 @param selected 已选原文。
    @param native 原生索引。 @param parents 原生父索引。 @return 原生键及独立原文 Overlay 节点。
    """
    raw = selected[key]["node"]
    raw_keys = [node_key(c["attributes"]["ID"], c["attributes"]["type"].split(",", 1)[0].strip(), key)
                for c in raw["children"]]
    native_keys = [node_key(str(c["$ID"]), c["$type"], key) for c in native[key].get("$children", [])]
    regular = [child for child in native_keys if selected[child]["parent"] == key]
    if [child for child in raw_keys if child in native] != regular:
        fail(f"ArkUI node {key} has conflicting raw/native child coverage or order")
    for child in native_keys:
        if parents[child] != key:
            fail(f"ArkUI node {child} has conflicting native parent identity")
    order, remaining = [], list(native_keys)
    for child, child_key in zip(raw["children"], raw_keys):
        if child_key not in native:
            if child["attributes"].get("Description") != "OverlayNode":
                fail(f"ArkUI node {child_key} has no complete native source or explicit overlay relationship")
            order.append(child)
            continue
        index = remaining.index(child_key)
        order.extend(remaining[:index + 1])
        del remaining[:index + 1]
    return order + remaining


def copy_overlay_tree(root: dict, parent: str, root_line: int, native: dict) -> dict:
    """@brief 保留完整显式 Overlay 分支，几何仍由既有 RS 校验负责。 @param root 原文分支。
    @param parent 宿主键。 @param root_line 森林根行号。 @param native 原生索引。 @return 带来源的原文分支。
    """
    result = copy.deepcopy(root)
    pending = [(result, parent)]
    while pending:
        node, owner = pending.pop()
        attrs = node["attributes"]
        tag = attrs["type"].split(",", 1)[0].strip()
        key = node_key(attrs["ID"], tag, owner)
        if key in native:
            fail(f"ArkUI overlay node {key} overlaps an independently selected native occurrence")
        node["source_header"], attrs["type"] = attrs["type"], tag
        node["source_association"] = {"forest_root_line": root_line, "raw_parent_id": owner,
                                      "native_parent_id": None, "relationship": "overlay"}
        pending.extend((child, key) for child in node["children"])
    return result


def select_native_tree(roots: list[dict], native_root: dict) -> dict:
    """@brief 从完整森林关联窗口树并记录每个原文位置，缓存连接必须有双源证据。
    @param roots 未过滤原文森林。 @param native_root 当前窗口原生树。 @return 可重新核验的窗口组件树。
    """
    try:
        native, parents = subtree_index(native_root)
    except ValueError as error:
        fail(f"Invalid ArkUI native identity graph: {error}")
    occurrences = forest_occurrences(roots)
    selected = {key: select_occurrence(key, native, parents, occurrences) for key in native}
    output, child_keys = {}, {}
    for key, record in selected.items():
        verify_cached_parent(key, selected, native, parents)
        for alias in occurrences[key]:
            if alias is not record:
                verify_broken_reference(key, record, alias)
        child_keys[key] = verify_selected_children(key, selected, native, parents)
        node = copy.deepcopy({k: v for k, v in record["node"].items() if k != "children"})
        node["source_header"] = node["attributes"]["type"]
        node["attributes"]["type"] = native[key]["$type"]
        node["source_association"] = {"forest_root_line": record["root_line"], "raw_parent_id": record["parent"],
                                      "native_parent_id": parents[key],
                                      "relationship": "child" if record["parent"] == parents[key] else "cached_item"}
        node["children"] = []
        output[key] = node
    for key in native:
        output[key]["children"] = [output[child] if isinstance(child, str) else
                                    copy_overlay_tree(child, key, selected[key]["root_line"], native)
                                    for child in child_keys[key]]
    root_key = node_key(str(native_root["$ID"]), native_root["$type"], None)
    for key, rows in forest_occurrences([output[root_key]]).items():
        if len(rows) != 1:
            fail(f"Selected ArkUI window contains duplicate node identity {key}")
    return output[root_key]


def verified_source_bytes(directory: Path, record: dict) -> bytes:
    """@brief 读取当前目录内摘要一致的来源。 @param directory 证据目录。 @param record 文件记录。 @return 原文。"""
    path = directory / record["file"]
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
        fail("ArkUI source is missing or escapes its capture directory")
    actual = file_info(path)
    if any(actual[key] != record[key] for key in ("bytes", "sha256")):
        fail(f"ArkUI source digest mismatch: {record['file']}")
    return path.read_bytes()


def read_application_tree(directory: Path, window: dict, record: dict) -> dict:
    """@brief 从原始森林与完整原生响应重新计算应用窗口树。 @param directory 证据目录。
    @param window 真实窗口。 @param record 原始索引。 @return 重建的窗口树。
    """
    raw = verified_source_bytes(directory, record["raw"])
    roots = parse_forest(raw.decode("utf-8"))
    forest = json.loads(verified_source_bytes(directory, record["forest"]))
    if forest != roots or sum(count_arkui_nodes(root) for root in roots) != record["forest_node_count"]:
        fail("ArkUI full forest JSON differs from its complete raw source")
    geometry = record["coordinate_evidence"]
    response = verified_source_bytes(directory, geometry["response"]).decode("utf-8")
    verify_window_response(response, window)
    native = parse_geometry_response(response)
    verify_native_application(native, window)
    if native != json.loads(verified_source_bytes(directory, geometry["arkui_geometry"])):
        fail("ArkUI native geometry JSON differs from its official response")
    return select_native_tree(roots, native)


def read_window_arkui(directory: Path, window: dict) -> dict:
    """@brief 核验完整来源与已存窗口树，不修改任何证据。 @param directory 证据目录。
    @param window 当前窗口及双树索引。 @return 已核验窗口树。
    """
    record = window["trees"]["arkui"]
    raw = verified_source_bytes(directory, record["raw"])
    if record["session"]["is_system"]:
        response = verified_source_bytes(directory, record["response"])
        selected, selection = select_system_tree(response, window, record["session"])
        if raw != selected or record["origin"] != {"kind": "source_slice", "selection": selection}:
            fail("ArkUI system tree differs from its complete source selection")
        expected = parse_arkui(raw.decode("utf-8"))
    else:
        response = verified_source_bytes(directory, record["response"]).decode("utf-8")
        verify_window_response(response, window)
        if record["origin"] != {"kind": "stdout"} or raw != response.encode("utf-8"):
            fail("ArkUI stdout tree differs from its complete response")
        expected = read_application_tree(directory, window, record)
    stored = json.loads(verified_source_bytes(directory, record))
    if stored != expected or count_arkui_nodes(stored) != record["node_count"]:
        fail(f"ArkUI JSON does not match its raw tree for window {window['window_id']}")
    return stored
