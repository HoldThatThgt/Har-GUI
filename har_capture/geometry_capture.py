"""@brief 采集原生 ArkUI 已变换矩形和 RenderService 裁剪证据。"""

import json
import re
import shlex
from pathlib import Path

from har_capture.hdc import Hdc, fail
from har_capture.metadata import artifact_info, verify_window_response


def parse_geometry_response(text: str) -> dict:
    """@brief 提取唯一原生几何 JSON。 @param text 官方完整响应。 @return 原生树。"""
    candidates = [line for line in text.splitlines() if line.lstrip().startswith("{")]
    if len(candidates) != 1:
        fail("ArkUI complete geometry response did not return exactly one JSON object")
    try:
        root = json.loads(candidates[0])
    except json.JSONDecodeError:
        fail("ArkUI complete geometry response returned invalid JSON")
    if not isinstance(root, dict) or type(root.get("$ID")) is not int or not isinstance(root.get("$type"), str):
        fail("ArkUI complete geometry response returned an invalid root identity")
    return root


def verify_native_application(root: dict, window: dict) -> None:
    """@brief 将完整原生应用信息绑定真实窗口，系统 UIContent 宿主与会话窗口分别保存。
    @param root 原生完整树。 @param window 已核验窗口。
    """
    info = root.get("appInfo")
    if (not isinstance(info, dict) or type(info.get("WindowID")) is not int
            or not isinstance(info.get("BundleName"), str) or not info["BundleName"]):
        fail("ArkUI complete geometry requires application identity metadata")
    session = window.get("session", window.get("trees", {}).get("arkui", {}).get("session", {}))
    if session.get("is_system") or window.get("is_system"):
        return
    if info["WindowID"] != window["window_id"] or info["BundleName"] != window["bundle_name"]:
        fail(f"ArkUI application identity differs from window {window['window_id']}")


def dump_window_geometry(hdc: Hdc, remote: dict, window: dict) -> str:
    """@brief 在设备生成并登记原生几何响应，暂不接收。 @param hdc 连接。
    @param remote 临时文件登记。 @param window 窗口。 @return 设备响应路径。
    """
    window_id = window["window_id"]
    path = remote["layout"] + f"-geometry-{window_id}.txt"
    remote[f"geometry_{window_id}"] = path
    command = f"hidumper -s WindowManagerService -a '-w {window_id} -allInfoWithParamConfig'"
    command += f" | cat > {shlex.quote(path)}"
    command += f" && test -s {shlex.quote(path)} && echo HAR_CAPTURE_GEOMETRY_READY"
    output = hdc.run(["shell", command], f"geometry_{window_id}")
    if "HAR_CAPTURE_GEOMETRY_READY" not in output.splitlines():
        fail(f"ArkUI geometry command did not produce a response for window {window_id}")
    return path


def receive_window_geometry(hdc: Hdc, stage: Path, path: str, window: dict) -> dict:
    """@brief 接收已生成的原生响应并核验、保存几何。 @param hdc 连接。 @param stage 目录。
    @param path 设备响应路径。 @param window 窗口。 @return 文件摘要和来源。
    """
    response = stage / "arkui" / f"window-{window['window_id']}-geometry-response.txt"
    hdc.receive(path, response, f"receive_geometry_{window['window_id']}")
    text = response.read_bytes().decode("utf-8")
    verify_window_response(text, window)
    geometry = parse_geometry_response(text)
    verify_native_application(geometry, window)
    path = stage / "arkui" / f"window-{window['window_id']}-geometry.json"
    path.write_text(json.dumps(geometry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"arkui_geometry": artifact_info(stage, path), "response": artifact_info(stage, response),
            "source_window_id": window["window_id"], "process_id": window["process_id"]}


def collect_window_geometry(hdc: Hdc, stage: Path, remote: dict, window: dict) -> dict:
    """@brief 顺序转储并接收单窗口原生几何。 @param hdc 连接。 @param stage 目录。
    @param remote 临时文件登记。 @param window 窗口。 @return 文件摘要和来源。
    """
    path = dump_window_geometry(hdc, remote, window)
    return receive_window_geometry(hdc, stage, path, window)


def collect_render_evidence(hdc: Hdc, stage: Path, remote: dict) -> dict:
    """@brief 分别保存已挂载和未挂载渲染树。 @param hdc 连接。 @param stage 目录。
    @param remote 临时文件登记。 @return 两份独立证据索引。
    """
    results = {}
    commands = (("render_tree", "RSTree", "render_geometry", "render-tree.txt"),
                ("render_detached_tree", "allNodeNotOnTree", "render_detached_geometry", "render-detached-tree.txt"))
    for key, argument, operation, filename in commands:
        path = remote["layout"] + "-" + filename
        remote[operation] = path
        command = f"hidumper -s RenderService -a '{argument}' | cat > {shlex.quote(path)}"
        command += f" && test -s {shlex.quote(path)} && echo HAR_CAPTURE_RENDER_READY"
        if "HAR_CAPTURE_RENDER_READY" not in hdc.run(["shell", command], operation).splitlines():
            fail(f"RenderService did not produce {argument} evidence")
        destination = stage / filename
        hdc.receive(path, destination, "receive_" + operation)
        results[key] = artifact_info(stage, destination)
    return results


def scene_source(stage: Path, window: dict, windows: list[dict], geometry: dict) -> dict:
    """@brief 关联已采系统完整原文的真实请求窗口与共享几何。 @param stage 目录。
    @param window 提供原文的系统窗口。 @param windows 同批窗口。 @param geometry 同进程几何。 @return 来源。
    """
    raw = window["trees"]["arkui"]["response"]
    try:
        text = (stage / raw["file"]).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        fail("Cannot read the existing SceneBoard raw response")
    ids = re.findall(r"(?m)^WinId:\s*(\d+)\s*$", text)
    if len(ids) != 1:
        fail("SceneBoard source requires exactly one response window identity")
    matches = [item for item in windows if item["window_id"] == int(ids[0])]
    if len(matches) != 1:
        fail("SceneBoard source response window is missing or ambiguous")
    origin = matches[0]
    if (origin["trees"]["arkui"]["session"]["is_system"] is not True
            or any(origin[key] != window[key] for key in ("process_id", "display_id"))):
        fail("SceneBoard source response differs from its system process or display")
    verify_window_response(text, origin)
    return {"source_window_id": origin["window_id"], "process_id": origin["process_id"],
            "display_id": origin["display_id"], "raw": raw, "arkui_geometry": geometry["arkui_geometry"]}


def collected_scene_sources(stage: Path, windows: list[dict], geometry: dict) -> list[dict]:
    """@brief 按系统进程和显示器复用同批既有来源，不增加设备命令。 @param stage 目录。
    @param windows 本批窗口。 @param geometry 已采几何索引。 @return 去重的完整场景来源。
    """
    sources = {}
    for window in windows:
        if window["trees"]["arkui"]["session"]["is_system"] is not True:
            continue
        key = (window["process_id"], window["display_id"])
        if key not in sources:
            sources[key] = scene_source(stage, window, windows, geometry[window["window_id"]])
    return list(sources.values())


def collect_geometry_evidence(hdc: Hdc, stage: Path, remote: dict, windows: list[dict]) -> dict:
    """@brief 复用同一系统进程整树并采集整屏渲染树。 @param hdc 连接。 @param stage 目录。
    @param remote 临时文件登记。 @param windows 本批窗口。 @return 按窗口索引的文件摘要。
    """
    results, system_processes = {}, {}
    for window in windows:
        session = window.get("trees", {}).get("arkui", {}).get("session", {})
        key = (window["process_id"], window["display_id"])
        if session.get("is_system") and key in system_processes:
            results[window["window_id"]] = dict(system_processes[key])
            continue
        results[window["window_id"]] = (collect_window_geometry(hdc, stage, remote, window) if session["is_system"]
                                        else dict(window["trees"]["arkui"]["coordinate_evidence"]))
        if session.get("is_system"):
            system_processes[key] = results[window["window_id"]]
    render_files = collect_render_evidence(hdc, stage, remote)
    sources = collected_scene_sources(stage, windows, results)
    for window in windows:
        item = results[window["window_id"]]
        item.update(render_files)
        item["scene_sources"] = [source for source in sources if source["display_id"] == window["display_id"]]
    return results
