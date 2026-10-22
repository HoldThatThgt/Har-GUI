"""@brief 将已完成的采集结果生成为可以离线打开的交互页面。"""

import base64
import json
import re
from pathlib import Path

from har_capture.arkui import count_arkui_nodes, parse_arkui
from har_capture.arkui_selection import select_system_tree
from har_capture.hdc import fail
from har_capture.metadata import count_nodes, file_info, image_info, load_layout
from har_capture.sessions import parse_sessions

TEMPLATES = Path(__file__).resolve().parent.parent / "viewer"


def latest_capture(root: Path) -> Path:
    """@brief 选择名称最新的已完成采集目录。 @param root 采集根目录。 @return 本次选择的目录。"""
    if not root.is_dir():
        fail(f"Capture directory does not exist: {root}")
    candidates = [path for path in root.iterdir() if path.is_dir()
                  and not path.name.endswith((".partial", ".failed"))]
    if not candidates:
        fail(f"No completed capture was found in {root}")
    return max(candidates, key=lambda path: path.name)


def verify_file(directory: Path, expected: dict, filename: str) -> None:
    """@brief 核对原始文件的名称、大小和摘要。 @param directory 采集目录。
    @param expected 元数据中的文件记录。 @param filename 当前采集格式的文件名。
    """
    actual = {**file_info(directory / filename), "file": filename}
    if any(expected[key] != actual[key] for key in ("file", "bytes", "sha256")):
        fail(f"Capture file does not match its metadata: {filename}")


def read_capture(directory: Path) -> dict:
    """@brief 读取并校验可视化所需的原始证据。 @param directory 采集目录。 @return 页面使用的数据。"""
    if directory.name.endswith((".partial", ".failed")):
        fail(f"Cannot visualize an incomplete capture: {directory}")
    metadata = json.loads((directory / "metadata.json").read_bytes())
    if metadata["status"] != "complete":
        fail(f"Capture is not complete: {directory}")
    verify_file(directory, metadata["files"]["layout"], "layout.json")
    verify_file(directory, metadata["files"]["screenshot"], "screenshot.png")
    image = image_info(directory / "screenshot.png")
    if any(image[key] != metadata["display"][key] for key in ("width", "height")):
        fail("Screenshot dimensions do not match capture metadata")
    screenshot = base64.b64encode((directory / "screenshot.png").read_bytes()).decode("ascii")
    return {"metadata": metadata, "layout": load_layout(directory / "layout.json"),
            "screenshot": "data:image/png;base64," + screenshot, "trees": read_trees(directory, metadata)}


def read_trees(directory: Path, metadata: dict) -> dict:
    """@brief 校验并加载分别采集的两类树。 @param directory 采集目录。
    @param metadata 采集记录。 @return 无障碍原始数组与按窗口索引的 ArkUI 树。
    """
    verify_file(directory, metadata["files"]["accessibility"], "accessibility.json")
    accessibility = json.loads((directory / "accessibility.json").read_bytes())
    verify_file(directory, metadata["files"]["session_details"], "session-details.txt")
    sessions = parse_sessions((directory / "session-details.txt").read_text(encoding="utf-8"))
    arkui = {}
    for window in metadata["windows"]:
        window_id = window["window_id"]
        access = window["trees"]["accessibility"]
        match = re.fullmatch(r"/(\d+)", access["json_pointer"])
        if match is None:
            fail(f"Invalid accessibility pointer for window {window_id}")
        access_root = accessibility[int(match[1])]
        if (count_nodes(access_root, window_id, window["display_id"]) != access["node_count"]
                or access_root["attributes"]["bundleName"] != window["bundle_name"]):
            fail(f"Accessibility tree does not match window {window_id}")
        evidence = window["trees"]["arkui"]
        verify_arkui_scope(window, evidence, sessions)
        filename = f"arkui/window-{window_id}.json"
        raw_filename = f"arkui/window-{window_id}.txt"
        verify_file(directory, evidence, filename)
        verify_file(directory, evidence["raw"], raw_filename)
        if evidence["session"]["is_system"]:
            verify_system_selection(directory, window, evidence)
        root = json.loads((directory / filename).read_bytes())
        if root != parse_arkui((directory / raw_filename).read_bytes().decode("utf-8")):
            fail(f"ArkUI JSON does not match its raw tree for window {window_id}")
        if count_arkui_nodes(root) != evidence["node_count"]:
            fail(f"ArkUI node count does not match window {window_id}")
        arkui[str(window_id)] = root
    return {"accessibility": accessibility, "arkui": arkui}


def verify_arkui_scope(window: dict, evidence: dict, sessions: dict) -> None:
    """@brief 核对组件树范围的系统会话依据。 @param window 窗口记录。
    @param evidence 组件树元数据。 @param sessions 系统会话原始记录。
    """
    window_id = window["window_id"]
    if window_id not in sessions:
        fail(f"Session details are missing window {window_id}")
    session = sessions[window_id]
    source = "WindowManagerService.-default.-all" if session["is_system"] else "WindowManagerService.-element"
    if (evidence["session"] != session or evidence["scope"] != "window" or evidence["source"] != source
            or evidence["display_id"] != window["display_id"]
            or any(session[key] != window[key] for key in ("process_id", "display_id"))):
        fail(f"ArkUI scope does not match session details for window {window_id}")


def verify_system_selection(directory: Path, window: dict, evidence: dict) -> None:
    """@brief 用完整原始输出重新验证系统窗口子树切片。 @param directory 采集目录。
    @param window 窗口记录。 @param evidence 原文和切片元数据。
    """
    window_id = window["window_id"]
    filename = f"arkui/window-{window_id}-response.txt"
    verify_file(directory, evidence["response"], filename)
    raw = (directory / filename).read_bytes()
    subtree, selection = select_system_tree(raw, window, evidence["session"])
    if (evidence["origin"] != {"kind": "source_slice", "selection": selection}
            or (directory / evidence["raw"]["file"]).read_bytes() != subtree):
        fail(f"ArkUI subtree does not match its complete source for window {window_id}")


def encode_data(data: dict) -> str:
    """@brief 转义 HTML 脚本边界而保持 JSON 内容不变。 @param data 页面数据。 @return 可嵌入的 JSON 文本。"""
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def build_viewer(directory: Path) -> Path:
    """@brief 生成包含数据与资源的单文件页面。 @param directory 完整采集目录。 @return 页面路径。"""
    directory = directory.resolve()
    data = read_capture(directory)
    parts = {"STYLES": (TEMPLATES / "styles.css").read_text(encoding="utf-8"),
             "SCRIPT": (TEMPLATES / "app.js").read_text(encoding="utf-8"), "DATA": encode_data(data)}
    template = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"\{\{(STYLES|DATA|SCRIPT)\}\}", lambda match: parts[match[1]], template)
    destination = directory / "viewer.html"
    destination.write_text(html, encoding="utf-8")
    return destination
