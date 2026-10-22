"""@brief 分别采集系统无障碍树与 ArkUI 内部组件树。"""

import json
import re
import shlex
import shutil
from pathlib import Path

from har_capture.arkui import count_arkui_nodes, parse_arkui
from har_capture.arkui_selection import select_system_tree
from har_capture.hdc import Hdc, fail
from har_capture.metadata import count_nodes, file_info
from har_capture.sessions import parse_sessions


def artifact_info(stage: Path, path: Path) -> dict:
    """@brief 保存相对采集目录的原始文件摘要。 @param stage 采集目录。
    @param path 原始文件。 @return 可直接访问文件的摘要。
    """
    return {**file_info(path), "file": path.relative_to(stage).as_posix()}


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


def dump_files(hdc: Hdc, window: dict, phase: str) -> dict:
    """@brief 读取目标进程命名空间内已有组件树文件状态。 @param hdc 连接。
    @param window 窗口记录。 @param phase 采集阶段。 @return 文件路径到修改时间和大小的映射。
    """
    root = f"/proc/{window['process_id']}/root/data/storage/el2/base"
    command = f"find {root} -maxdepth 5 -type f -name arkui.dump -exec stat -c '%n|%y|%s' {{}} \\;"
    output = hdc.run(["shell", command], f"arkui_{phase}_{window['window_id']}")
    files = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) != 3 or not fields[0].startswith(root + "/") or not fields[0].endswith("/arkui.dump"):
            fail(f"Cannot identify ArkUI dump files for window {window['window_id']}: {line}")
        files[fields[0]] = {"modified_at": fields[1], "bytes": int(fields[2])}
    return files


def verify_window_response(text: str, window: dict) -> None:
    """@brief 确认组件树命令仍指向当前窗口与进程。 @param text 窗口管理器响应。
    @param window 已采集的窗口记录。
    """
    for label, expected in (("WinId", window["window_id"]), ("Pid", window["process_id"])):
        match = re.search(rf"(?m)^{label}:\s*(\d+)\s*$", text)
        if match is None or int(match[1]) != expected:
            fail(f"ArkUI response does not match {label} for window {window['window_id']}")


def receive_arkui(hdc: Hdc, stage: Path, window: dict, evidence: dict) -> dict:
    """@brief 依据官方输出位置接收当前组件树并验证新鲜度。 @param hdc 连接。
    @param stage 采集目录。 @param window 窗口记录。 @param evidence 输出与文件状态。 @return 树元数据。
    """
    response = evidence["response"]
    text = response.read_text(encoding="utf-8")
    verify_window_response(text, window)
    destination = stage / "arkui" / f"window-{window['window_id']}.txt"
    if re.search(r"(?m)^ *\|-> .+ childSize:\d+", text):
        shutil.copyfile(response, destination)
        origin = {"kind": "stdout"}
    else:
        before, after = evidence["before"], evidence["after"]
        changed = [path for path in after if after[path] != before.get(path)]
        if len(changed) != 1:
            fail(f"Expected one freshly written ArkUI dump for window {window['window_id']}; found {len(changed)}")
        source = changed[0]
        hdc.receive(source, destination, f"receive_arkui_{window['window_id']}")
        if destination.stat().st_size != after[source]["bytes"]:
            fail(f"ArkUI dump changed during transfer for window {window['window_id']}")
        origin = {"kind": "application_file", "path": source, "before": before.get(source), "after": after[source]}
    return {**save_arkui_tree(stage, destination),
            "source": "WindowManagerService.-element", "origin": origin,
            "response": artifact_info(stage, response)}


def collect_arkui(hdc: Hdc, stage: Path, remote: dict, window: dict) -> dict:
    """@brief 按窗口顺序导出组件树，避免同进程文件相互覆盖。 @param hdc 连接。
    @param stage 采集目录。 @param remote 本次设备临时文件。 @param window 窗口记录。 @return 树元数据。
    """
    window_id = window["window_id"]
    before = dump_files(hdc, window, "before")
    path = remote["layout"] + f"-arkui-{window_id}.txt"
    remote[f"arkui_{window_id}"] = path
    quoted = shlex.quote(path)
    command = f"hidumper -s WindowManagerService -a '-w {window_id} -element' | cat > {quoted}"
    command += f" && test -s {quoted} && echo HAR_CAPTURE_DUMP_READY"
    output = hdc.run(["shell", command], f"dump_arkui_{window_id}")
    if "HAR_CAPTURE_DUMP_READY" not in output.splitlines():
        fail(f"ArkUI command did not produce a response for window {window_id}")
    after = dump_files(hdc, window, "after")
    response = stage / "arkui" / f"window-{window_id}-response.txt"
    hdc.receive(path, response, f"receive_arkui_response_{window_id}")
    return receive_arkui(hdc, stage, window, {"before": before, "after": after, "response": response})


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
