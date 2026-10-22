"""@brief 采集原生 ArkUI 已变换矩形和 RenderService 裁剪证据。"""

import json
import shlex
from pathlib import Path

from har_capture.hdc import Hdc, fail
from har_capture.tree_capture import artifact_info, verify_window_response


def parse_geometry_response(text: str) -> dict:
    """@brief 提取唯一原生几何 JSON。 @param text 官方完整响应。 @return 原生树。"""
    candidates = [line for line in text.splitlines() if line.startswith('{"$type"')]
    if len(candidates) != 1:
        fail("ArkUI allInfo did not return exactly one native geometry tree")
    try:
        root = json.loads(candidates[0])
    except json.JSONDecodeError:
        fail("ArkUI allInfo returned invalid native geometry JSON")
    if not isinstance(root, dict) or "$ID" not in root:
        fail("ArkUI allInfo returned an invalid native geometry root")
    return root


def receive_geometry_response(hdc: Hdc, stage: Path, remote: dict, window: dict) -> Path:
    """@brief 接收官方原生几何响应。 @param hdc 连接。 @param stage 目录。
    @param remote 临时文件登记。 @param window 窗口。 @return 响应路径。
    """
    window_id = window["window_id"]
    path = remote["layout"] + f"-geometry-{window_id}.txt"
    remote[f"geometry_{window_id}"] = path
    command = f"hidumper -s WindowManagerService -a '-w {window_id} -allInfo' | cat > {shlex.quote(path)}"
    command += f" && test -s {shlex.quote(path)} && echo HAR_CAPTURE_GEOMETRY_READY"
    output = hdc.run(["shell", command], f"geometry_{window_id}")
    if "HAR_CAPTURE_GEOMETRY_READY" not in output.splitlines():
        fail(f"ArkUI geometry command did not produce a response for window {window_id}")
    destination = stage / "arkui" / f"window-{window_id}-geometry-response.txt"
    hdc.receive(path, destination, f"receive_geometry_{window_id}")
    return destination


def collect_window_geometry(hdc: Hdc, stage: Path, remote: dict, window: dict) -> dict:
    """@brief 保存单次原生几何及实际请求窗口。 @param hdc 连接。 @param stage 目录。
    @param remote 临时文件登记。 @param window 窗口。 @return 文件摘要和来源。
    """
    response = receive_geometry_response(hdc, stage, remote, window)
    text = response.read_bytes().decode("utf-8")
    verify_window_response(text, window)
    geometry = parse_geometry_response(text)
    path = stage / "arkui" / f"window-{window['window_id']}-geometry.json"
    path.write_text(json.dumps(geometry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"arkui_geometry": artifact_info(stage, path), "response": artifact_info(stage, response),
            "source_window_id": window["window_id"], "process_id": window["process_id"]}


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
        results[window["window_id"]] = collect_window_geometry(hdc, stage, remote, window)
        if session.get("is_system"):
            system_processes[key] = results[window["window_id"]]
    render_files = collect_render_evidence(hdc, stage, remote)
    for item in results.values():
        item.update(render_files)
    return results
