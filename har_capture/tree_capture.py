"""@brief 分别采集系统无障碍树与 ArkUI 内部组件树。"""

import json
import shlex
from pathlib import Path

from har_capture.arkui import count_arkui_nodes, parse_arkui, parse_forest
from har_capture.arkui_selection import select_system_tree, select_native_tree
from har_capture.geometry_capture import dump_window_geometry, receive_window_geometry
from har_capture.hdc import Hdc, fail
from har_capture.metadata import count_nodes, artifact_info, verify_window_response
from har_capture.sessions import parse_sessions


def save_arkui_tree(stage: Path, raw_path: Path) -> dict:
    """@brief 将原始 ArkUI 文本解析为独立 JSON 树。 @param stage 采集目录。
    @param raw_path 原始文本。 @return JSON、原文摘要与节点数。
    """
    root = parse_arkui(raw_path.read_bytes().decode("utf-8"))
    destination = raw_path.with_suffix(".json")
    with destination.open("w", encoding="utf-8", newline="\n") as output:
        json.dump(root, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")
    return {**artifact_info(stage, destination), "raw": artifact_info(stage, raw_path),
            "node_count": count_arkui_nodes(root)}


def save_full_arkui_tree(stage: Path, raw_path: Path, geometry: dict) -> dict:
    """@brief 同时保存未过滤森林与有明确来源的窗口树。 @param stage 采集目录。
    @param raw_path 完整原文。 @param geometry 原生完整几何。 @return 原始森林及窗口树文件索引。
    """
    roots = parse_forest(raw_path.read_bytes().decode("utf-8"))
    native = json.loads((stage / geometry["arkui_geometry"]["file"]).read_bytes())
    root = select_native_tree(roots, native)
    forest = raw_path.with_name(raw_path.stem + "-forest.json")
    destination = raw_path.with_suffix(".json")
    for path, data in ((forest, roots), (destination, root)):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return {**artifact_info(stage, destination), "raw": artifact_info(stage, raw_path),
            "forest": artifact_info(stage, forest), "node_count": count_arkui_nodes(root),
            "forest_node_count": sum(count_arkui_nodes(item) for item in roots), "coordinate_evidence": geometry}


def collect_arkui(hdc: Hdc, stage: Path, remote: dict, window: dict) -> dict:
    """@brief 连续完成应用原文和原生转储后再接收，减少两次来源之间的主机等待。 @param hdc 连接。
    @param stage 采集目录。 @param remote 本次设备临时文件。 @param window 窗口记录。 @return 树元数据。
    """
    window_id = window["window_id"]
    path = remote["layout"] + f"-arkui-{window_id}.txt"
    remote[f"arkui_{window_id}"] = path
    quoted = shlex.quote(path)
    command = f"hidumper -s WindowManagerService -a '-w {window_id} -default -all' | cat > {quoted}"
    command += f" && test -s {quoted} && echo HAR_CAPTURE_DUMP_READY"
    output = hdc.run(["shell", command], f"dump_arkui_{window_id}")
    if "HAR_CAPTURE_DUMP_READY" not in output.splitlines():
        fail(f"ArkUI command did not produce a response for window {window_id}")
    geometry_path = dump_window_geometry(hdc, remote, window)
    response = stage / "arkui" / f"window-{window_id}-response.txt"
    hdc.receive(path, response, f"receive_arkui_response_{window_id}")
    raw = response.read_bytes()
    verify_window_response(raw.decode("utf-8"), window)
    destination = stage / "arkui" / f"window-{window_id}.txt"
    destination.write_bytes(raw)
    geometry = receive_window_geometry(hdc, stage, geometry_path, window)
    files = save_full_arkui_tree(stage, destination, geometry)
    return {**files, "origin": {"kind": "stdout"}, "response": artifact_info(stage, response),
            "source": "WindowManagerService.-default.-all"}


def collect_accessibility(hdc: Hdc, stage: Path, remote: dict, display_id: int) -> dict:
    """@brief 原样保存 UITest 未进行可见边界过滤的无障碍导出。 @param hdc 连接。
    @param stage 采集目录。 @param remote 临时路径。 @param display_id 显示器。 @return 按窗口索引的树。
    """
    path = remote["layout"] + "-accessibility.json"
    remote["accessibility"] = path
    output = hdc.run(["shell", "uitest", "dumpLayout", "-d", str(display_id), "-i", "-p", path], "accessibility")
    if f"DumpLayout saved to:{path}" not in output:
        fail("UITest did not confirm the accessibility dump")
    destination = stage / "accessibility.json"
    hdc.receive(path, destination, "receive_accessibility")
    roots = json.loads(destination.read_bytes())
    if not isinstance(roots, list):
        fail("Accessibility dump must contain a window array")
    trees = {}
    for index, root in enumerate(roots):
        window_id = int(root["attributes"]["hostWindowId"])
        if window_id in trees:
            fail(f"Duplicate accessibility window: {window_id}")
        trees[window_id] = {"file": "accessibility.json", "json_pointer": f"/{index}",
                            "artifact": artifact_info(stage, destination),
                            "node_count": count_nodes(root, window_id, display_id),
                            "bundle_name": root["attributes"]["bundleName"], "source": "UITest.dumpLayout.-i"}
    return trees


def collect_system_arkui(hdc: Hdc, stage: Path, remote: dict, window: dict, session: dict) -> dict:
    """@brief 保留系统完整导出并按会话节点提取窗口组件树。 @param hdc 连接。 @param stage 采集目录。
    @param remote 临时路径。 @param window 窗口记录。 @param session 会话详情。 @return 树与原始出处。
    """
    window_id = window["window_id"]
    path = remote["layout"] + f"-arkui-{window_id}.txt"
    remote[f"arkui_{window_id}"] = path
    quoted = shlex.quote(path)
    command = f"hidumper -s WindowManagerService -a '-w {window_id} -default -all' | cat > {quoted}"
    command += f" && test -s {quoted} && echo HAR_CAPTURE_DUMP_READY"
    output = hdc.run(["shell", command], f"dump_arkui_{window_id}")
    if "HAR_CAPTURE_DUMP_READY" not in output.splitlines():
        fail(f"ArkUI command did not produce a response for window {window_id}")
    response = stage / "arkui" / f"window-{window_id}-response.txt"
    hdc.receive(path, response, f"receive_arkui_response_{window_id}")
    raw = response.read_bytes()
    verify_window_response(raw.decode("utf-8"), window)
    subtree, selection = select_system_tree(raw, window, session)
    destination = stage / "arkui" / f"window-{window_id}.txt"
    destination.write_bytes(subtree)
    return {**save_arkui_tree(stage, destination),
            "source": "WindowManagerService.-default.-all", "origin": {"kind": "source_slice", "selection": selection},
            "response": artifact_info(stage, response)}


def collect_trees(hdc: Hdc, stage: Path, remote: dict, windows: list[dict], display_id: int) -> dict:
    """@brief 为每个已识别窗口获取两种独立来源。 @param hdc 连接。 @param stage 采集目录。
    @param remote 本次临时文件。 @param windows 窗口记录。 @param display_id 显示器。 @return 窗口树映射。
    """
    accessibility = collect_accessibility(hdc, stage, remote, display_id)
    details = hdc.run(["shell", "hidumper", "-s", "WindowManagerService", "-a", "-c"], "session_details")
    (stage / "session-details.txt").write_bytes(details.encode("utf-8"))
    sessions = parse_sessions(details)
    (stage / "arkui").mkdir()
    result = {}
    for window in windows:
        window_id = window["window_id"]
        if window_id not in accessibility:
            continue
        if accessibility[window_id]["bundle_name"] != window["bundle_name"]:
            fail(f"Accessibility tree does not match window {window_id}")
        if window_id not in sessions:
            fail(f"Session details are missing window {window_id}")
        session = sessions[window_id]
        if any(session[key] != window[key] for key in ("process_id", "display_id")):
            fail(f"Session details do not match window {window_id}")
        arkui = (collect_system_arkui(hdc, stage, remote, window, session) if session["is_system"]
                 else collect_arkui(hdc, stage, remote, window))
        arkui.update(scope="window", display_id=display_id, session=session)
        result[window_id] = {"accessibility": accessibility[window_id],
                             "arkui": arkui}
    return result
