"""@brief 从官方逐窗口接口采集 UITest 未返回但仍然暴露的窗口。"""

import copy
import json
import math
import re
import shlex
from pathlib import Path

from har_capture.arkui import parse_arkui
from har_capture.arkui_selection import select_system_tree
from har_capture.capture_checks import window_exclusion
from har_capture.hdc import Hdc, fail
from har_capture.metadata import count_nodes, file_info
from har_capture.tree_capture import (artifact_info, collect_arkui, collect_system_arkui,
                                      save_arkui_tree, verify_window_response)


def read_window_accessibility(evidence: Path, window: dict) -> dict:
    """@brief 按明确来源和 JSON 指针读取并核验窗口无障碍树。 @param evidence 证据目录。
    @param window 关联窗口。 @return 无障碍树根。
    """
    record = window["trees"]["accessibility"]
    path = (evidence / record["file"]).resolve()
    if not path.is_relative_to(evidence.resolve()):
        fail("Accessibility evidence path escapes the capture directory")
    expected = record["artifact"]
    actual = file_info(path)
    if record["file"] != expected["file"] or any(actual[key] != expected[key] for key in ("bytes", "sha256")):
        fail(f"Accessibility evidence digest mismatch for window {window['window_id']}")
    root = json.loads(path.read_bytes())
    pointer = record["json_pointer"]
    if pointer:
        if re.fullmatch(r"/\d+", pointer) is None or not isinstance(root, list):
            fail("Unsupported accessibility JSON pointer")
        if int(pointer[1:]) >= len(root):
            fail("Accessibility JSON pointer is outside the source array")
        root = root[int(pointer[1:])]
    if (count_nodes(root, window["window_id"], window["display_id"]) != record["node_count"]
            or root["attributes"]["bundleName"] != window["bundle_name"]):
        fail(f"Accessibility ownership mismatch for window {window['window_id']}")
    return root


def select_inspector_window(root: dict, window: dict) -> dict:
    """@brief 按系统会话 ID 唯一选择真实无障碍窗口。 @param root Inspector 全树。
    @param window 会话关联窗口。 @return 未改写的窗口子树。
    """
    pending, matches = [root], []
    while pending:
        node = pending.pop()
        attrs = node["attributes"]
        if (attrs.get("type") == "WindowScene" and attrs.get("compid") == f"session{window['window_id']}"
                and attrs.get("ID") == str(window["session"]["ui_node_id"])):
            matches.append(node)
        pending.extend(node["children"])
    if len(matches) != 1:
        fail(f"Inspector did not uniquely identify system window {window['window_id']}")
    return matches[0]


def normalize_inspector_tree(root: dict, window: dict) -> dict:
    """@brief 保留原属性并标明经过会话证明的窗口归属。 @param root 无障碍子树。
    @param window 已核验窗口。 @return 带精确屏幕矩形的派生无障碍树。
    """
    result = copy.deepcopy(root)
    pending = [result]
    while pending:
        node = pending.pop()
        attrs = node["attributes"]
        box = {key: float(attrs[source]) for key, source in (
            ("x", "left"), ("y", "top"), ("width", "width"), ("height", "height"))}
        if any(not math.isfinite(value) for value in box.values()) or box["width"] < 0 or box["height"] < 0:
            fail("Inspector returned invalid screen bounds")
        left, top = math.floor(box["x"]), math.floor(box["y"])
        right, bottom = math.ceil(box["x"] + box["width"]), math.ceil(box["y"] + box["height"])
        attrs.update(hostWindowId=str(window["window_id"]), displayId=str(window["display_id"]),
                     bundleName=window["bundle_name"], bounds=f"[{left},{top}][{right},{bottom}]",
                     accessibilityId=attrs["ID"], id=attrs.get("compid", ""))
        node["screen_bounds"] = box
        node["coordinate_source"] = "AccessibilityManager Inspector left/top/width/height; bounds is outward-rounded"
        pending.extend(node["children"])
    return result


def collect_inspector(hdc: Hdc, stage: Path, remote: dict, window: dict) -> dict:
    """@brief 普通应用逐窗口采集，系统进程保存一份共享全树。 @param hdc 连接。 @param stage 目录。
    @param remote 临时文件。 @param window 请求关联窗口。 @return 全树、来源及官方包名。
    """
    key = f"process-{window['process_id']}" if window["is_system"] else f"window-{window['window_id']}"
    path = remote["layout"] + f"-accessibility-inspector-{key}.txt"
    remote[f"accessibility_inspector_{key}"] = path
    command = f"hidumper -s WindowManagerService -a '-w {window['window_id']} -inspector' | cat > {shlex.quote(path)}"
    command += f" && test -s {shlex.quote(path)} && echo HAR_CAPTURE_INSPECTOR_READY"
    if "HAR_CAPTURE_INSPECTOR_READY" not in hdc.run(["shell", command], f"inspector_{key}").splitlines():
        fail("WindowManagerService did not produce an accessibility Inspector dump")
    destination = stage / "accessibility-inspector" / f"{key}-inspector.txt"
    hdc.receive(path, destination, f"receive_inspector_{key}")
    text = destination.read_text(encoding="utf-8")
    verify_window_response(text, window)
    bundles = re.findall(r"(?m)^bundleName:([A-Za-z_][A-Za-z0-9_.]*)\s*$", text)
    if len(bundles) != 1:
        fail("Inspector response requires one verified bundle name")
    # 两种官方 dump 共用缩进文本语法；此处内容来源始终是 AccessibilityManager。
    return {"root": parse_arkui(text), "raw": artifact_info(stage, destination), "bundle_name": bundles[0]}


def save_window_accessibility(stage: Path, window: dict, inspector: dict) -> dict:
    """@brief 保存逐窗口响应或系统会话子树，明确记录来源选择。 @param stage 证据目录。
    @param window 关联窗口。 @param inspector 官方无障碍全树。 @return 无障碍索引。
    """
    selected = select_inspector_window(inspector["root"], window) if window["is_system"] else inspector["root"]
    selection = {"kind": "window_response", "window_id": window["window_id"],
                 "process_id": window["process_id"], "source_line": selected["source_line"]}
    if window["is_system"]:
        selection.update(kind="system_session_subtree", component_id=f"session{window['window_id']}",
                         accessibility_id=window["session"]["ui_node_id"])
    root = normalize_inspector_tree(selected, window)
    destination = stage / "accessibility-inspector" / f"window-{window['window_id']}.json"
    destination.write_text(json.dumps(root, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"file": destination.relative_to(stage).as_posix(), "json_pointer": "",
            "artifact": artifact_info(stage, destination), "raw": inspector["raw"],
            "node_count": count_nodes(root, window["window_id"], window["display_id"]),
            "bundle_name": window["bundle_name"], "source": "WindowManagerService.-inspector.AccessibilityManager",
            "selection": selection}


def system_arkui_from_shared_source(stage: Path, window: dict, source: dict) -> dict:
    """@brief 从同进程已采的完整 SceneBoard 原文提取真实 ArkUI 子树。 @param stage 目录。
    @param window 系统窗口。 @param source 完整源索引。 @return 原始 ArkUI 索引。
    """
    raw = (stage / source["response"]["file"]).read_bytes()
    subtree, selection = select_system_tree(raw, window, window["session"])
    path = stage / "arkui" / f"window-{window['window_id']}.txt"
    path.write_bytes(subtree)
    return {**save_arkui_tree(stage, path), "source": "WindowManagerService.-default.-all",
            "origin": {"kind": "source_slice", "selection": selection}, "response": source["response"],
            "scope": "window", "display_id": window["display_id"],
            "session": {key: window["session"][key] for key in (
                "window_id", "process_id", "is_system", "ui_node_id", "display_id")}}


def attach_window_trees(stage: Path, metadata: dict, window: dict, trees: dict) -> None:
    """@brief 持久索引逐窗口补齐的双树，不删除完整 WMS 原始记录。 @param stage 目录。
    @param metadata 本次元数据。 @param window 已核验窗口。 @param trees 双树文件索引。
    """
    access, arkui = trees["accessibility"], trees["arkui"]
    root = json.loads((stage / access["file"]).read_bytes())
    window.update(trees=trees, ui_node_id=window["session"]["ui_node_id"],
                  layout={"file": access["file"], "json_pointer": ""},
                  layout_bounding_box=root["screen_bounds"], node_count=access["node_count"])
    metadata["windows"].append(window)
    metadata["files"][f"accessibility_inspector_tree_{window['window_id']}"] = access["artifact"]
    metadata["files"][f"accessibility_inspector_raw_{window['window_id']}"] = access["raw"]
    for key in ("raw", "response"):
        metadata["files"][f"arkui_{key}_{window['window_id']}"] = arkui[key]
    metadata["files"][f"arkui_{window['window_id']}"] = {key: arkui[key] for key in ("file", "bytes", "sha256")}


def supplement_missing_windows(hdc: Hdc, stage: Path, remote: dict, metadata: dict) -> None:
    """@brief 以 WMS 暴露窗口为准补齐双树，不把 UITest 窗口列表当作完整清单。 @param hdc 连接。
    @param stage 目录。
    @param remote 临时文件。 @param metadata 同次采集元数据，原地补充窗口。
    """
    captured = {window["window_id"] for window in metadata["windows"]}
    missing = [window for window in metadata["window_inventory"] if window["window_id"] not in captured
               and window_exclusion(window, window["session"], metadata["display"]) is None]
    if not missing:
        return
    (stage / "accessibility-inspector").mkdir()
    inspectors = {}
    sources = {window["process_id"]: window["trees"]["arkui"] for window in metadata["windows"]
               if window["is_system"]}
    for window in missing:
        process_id = window["process_id"]
        session = window["session"]
        if type(window["is_system"]) is not bool or any(session[key] != window[key] for key in (
                "process_id", "window_id", "display_id")):
            fail(f"Session details do not match exposed window {window['window_id']}")
        key = ("system", process_id) if window["is_system"] else ("application", window["window_id"])
        if key not in inspectors:
            inspectors[key] = collect_inspector(hdc, stage, remote, window)
        if window.get("bundle_name") and window["bundle_name"] != inspectors[key]["bundle_name"]:
            fail(f"Inspector bundle differs from the UITest window {window['window_id']}")
        window["bundle_name"] = inspectors[key]["bundle_name"]
        access = save_window_accessibility(stage, window, inspectors[key])
        if window["is_system"]:
            if process_id not in sources:
                sources[process_id] = collect_system_arkui(hdc, stage, remote, window, session)
            arkui = system_arkui_from_shared_source(stage, window, sources[process_id])
        else:
            arkui = collect_arkui(hdc, stage, remote, window)
            arkui.update(scope="window", display_id=window["display_id"], session=session)
        attach_window_trees(stage, metadata, window, {"accessibility": access, "arkui": arkui})
    metadata["windows"].sort(key=lambda item: item["z_order"], reverse=True)
