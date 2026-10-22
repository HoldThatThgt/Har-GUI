"""@brief 从同批已索引的 SceneBoard 原文和原生几何建立逐窗口完整来源。"""

import hashlib
import json
import re
from pathlib import Path

from har_capture.arkui import parse_arkui
from har_capture.arkui_selection import find_target, index_nodes, node_slice, read_identity
from har_capture.hdc import fail


def artifact_bytes(directory: Path, record: dict, cache: dict) -> bytes:
    """@brief 每批只读取并核验同一原始文件一次。 @param directory 证据目录。
    @param record 文件摘要。 @param cache 本批缓存。 @return 已验证原始字节。
    """
    if (not isinstance(record, dict) or not isinstance(record.get("file"), str)
            or type(record.get("bytes")) is not int or record["bytes"] < 0
            or not isinstance(record.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record["sha256"]) is None):
        fail("SceneBoard evidence requires a valid file digest record")
    name = record["file"]
    identity = (record["bytes"], record["sha256"])
    if name in cache:
        if cache[name][0] != identity:
            fail("SceneBoard evidence has conflicting digests for one file")
        return cache[name][1]
    path = directory / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory):
        fail("SceneBoard evidence file is missing or escapes the capture directory")
    try:
        raw = path.read_bytes()
    except OSError:
        fail("Cannot read the indexed SceneBoard evidence file")
    if (len(raw), hashlib.sha256(raw).hexdigest()) != identity:
        fail("SceneBoard evidence digest differs from its file record")
    cache[name] = (identity, raw)
    return raw


def verify_source(source: dict, windows: dict) -> tuple[int, int]:
    """@brief 验证来源的真实系统窗口、进程、显示器及既有文件归属。 @param source 来源记录。
    @param windows 本批窗口索引。 @return 系统进程和显示器身份。
    """
    fields = {"source_window_id", "process_id", "display_id", "raw", "arkui_geometry"}
    if not isinstance(source, dict) or set(source) != fields:
        fail("SceneBoard source requires explicit window, process, display, raw and geometry records")
    if any(type(source[key]) is not int for key in ("source_window_id", "process_id", "display_id")):
        fail("SceneBoard source identities must be integers")
    origin = windows.get(source["source_window_id"])
    if (origin is None or origin.get("is_system") is not True
            or any(source[key] != origin[key] for key in ("process_id", "display_id"))):
        fail("SceneBoard source does not identify a captured system window on its process and display")
    arkui = origin["trees"]["arkui"]
    if source["raw"] != arkui["response"] or source["arkui_geometry"] != arkui["coordinate_evidence"]["arkui_geometry"]:
        fail("SceneBoard source files differ from the captured system window references")
    return source["process_id"], source["display_id"]


def source_records(windows: list[dict]) -> dict:
    """@brief 收集本批明确声明的来源并拒绝同进程显示器的冲突记录。 @param windows 窗口记录。
    @return 按系统进程和显示器索引的唯一来源。
    """
    index = {window["window_id"]: window for window in windows}
    if len(index) != len(windows):
        fail("SceneBoard evidence requires distinct captured window identities")
    result = {}
    for window in windows:
        geometry = window.get("trees", {}).get("arkui", {}).get("coordinate_evidence", {})
        sources = geometry.get("scene_sources")
        if not isinstance(sources, list) or not sources:
            fail(f"Missing SceneBoard coordinate sources for window {window['window_id']}")
        seen = set()
        for source in sources:
            key = verify_source(source, index)
            if source["display_id"] != window["display_id"] or key in seen:
                fail("SceneBoard window sources contain a wrong display or duplicate system process")
            if key in result and result[key] != source:
                fail("SceneBoard sources conflict for the same system process and display")
            result[key] = source
            seen.add(key)
    return result


def load_source(directory: Path, source: dict, cache: dict) -> dict:
    """@brief 读取一份共享完整场景森林并核验真实响应头。 @param directory 目录。
    @param source 明确来源。 @param cache 文件缓存。 @return 已建索引森林和完整原生几何。
    """
    raw = artifact_bytes(directory, source["raw"], cache)
    geometry = artifact_bytes(directory, source["arkui_geometry"], cache)
    try:
        text = raw.decode("utf-8")
        native = json.loads(geometry)
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail("SceneBoard evidence contains invalid UTF-8 or native geometry JSON")
    for label, key in (("WinId", "source_window_id"), ("Pid", "process_id")):
        matches = re.findall(rf"(?m)^{label}:\s*(\d+)\s*$", text)
        if matches != [str(source[key])]:
            fail("SceneBoard raw response header differs from its source identity")
    if not isinstance(native, dict) or "$ID" not in native or "$type" not in native:
        fail("SceneBoard native geometry has no valid root identity")
    lines, nodes = index_nodes(raw)
    if not nodes:
        fail("SceneBoard raw response contains no component forest")
    return {"raw": raw, "lines": lines, "nodes": nodes, "native": native, "source": source}


def target_index(forest: dict, window: dict) -> int | None:
    """@brief 按会话组件和无障碍身份选择目标，缺少匹配时返回空。 @param forest 已建索引森林。
    @param window 目标窗口。 @return 唯一目标位置或空。
    """
    lines, nodes = forest["lines"], forest["nodes"]
    accessibility_id = window.get("ui_node_id")
    if type(accessibility_id) is not int:
        fail("SceneBoard target requires an integer accessibility identity")
    expected_id = str(accessibility_id).encode("ascii")
    expected_component = f"session{window['window_id']}".encode("ascii")
    for index, node in enumerate(nodes):
        if node["tag"] != b"WindowScene":
            continue
        end = nodes[index + 1]["line_index"] if index + 1 < len(nodes) else len(lines)
        attrs = read_identity(lines, node["line_index"], end)
        if expected_id in attrs.get(b"AccessibilityId", []) and expected_component in attrs.get(b"compid", []):
            return find_target(lines, nodes, window["window_id"], accessibility_id)[0]
    return None


def parse_source_node(raw: bytes, source_line: int) -> dict:
    """@brief 解析原始节点或子树并保留完整来源行号。 @param raw 原文字节。
    @param source_line 原始起始行。 @return 解析结果。
    """
    try:
        result = parse_arkui(raw.decode("utf-8"))
    except UnicodeDecodeError:
        fail("SceneBoard node contains invalid UTF-8")
    pending = [result]
    while pending:
        node = pending.pop()
        node["source_line"] += source_line - 1
        pending.extend(node["children"])
    return result


def source_path(forest: dict, index: int) -> list[dict]:
    """@brief 选取完整根到会话路径，目标节点保留完整子树。 @param forest 原始森林。
    @param index 目标位置。 @return 保留自身属性的祖先及目标子树。
    """
    lines, nodes, stack = forest["lines"], forest["nodes"], []
    for position, node in enumerate(nodes[:index + 1]):
        while stack and node["indent"] <= nodes[stack[-1]]["indent"]:
            stack.pop()
        expected_indent = nodes[stack[-1]]["indent"] + 2 if stack else 0
        if node["indent"] != expected_indent:
            fail("SceneBoard raw response has an invalid ancestor indentation")
        stack.append(position)
    path = []
    for position in stack[:-1]:
        start = nodes[position]["line_index"]
        end = nodes[position + 1]["line_index"] if position + 1 < len(nodes) else len(lines)
        path.append(parse_source_node(b"".join(lines[start:end]), start + 1))
    raw, selection = node_slice(forest["raw"], lines, nodes, index)
    path.append(parse_source_node(raw, selection["start_line"]))
    return path


def select_window_scene(forests: list[dict], window: dict) -> dict:
    """@brief 从已核验的同显示器森林唯一选取窗口完整父链。 @param forests 候选来源。
    @param window 目标窗口。 @return 完整父链、原生几何和真实来源。
    """
    matches = []
    for forest in forests:
        if forest["source"]["display_id"] != window["display_id"]:
            fail("SceneBoard window source belongs to another display")
        index = target_index(forest, window)
        if index is not None:
            matches.append((forest, index))
    if len(matches) != 1:
        fail(f"Expected one SceneBoard source for window {window['window_id']}; found {len(matches)}")
    forest, index = matches[0]
    return {"path": source_path(forest, index), "native": forest["native"], "source": forest["source"]}


def load_scene_evidence(directory: Path, windows: list[dict]) -> dict[int, dict]:
    """@brief 每批核验共享来源并为每个窗口选择唯一完整父链。 @param directory 证据目录。
    @param windows 同批窗口。 @return 按窗口索引的路径、完整原生几何与明确来源。
    """
    directory = directory.resolve()
    cache, sources, result = {}, source_records(windows), {}
    forests = {key: load_source(directory, source, cache) for key, source in sources.items()}
    for window in windows:
        candidates = [forests[(source["process_id"], source["display_id"])]
                      for source in window["trees"]["arkui"]["coordinate_evidence"]["scene_sources"]]
        result[window["window_id"]] = select_window_scene(candidates, window)
    return result
