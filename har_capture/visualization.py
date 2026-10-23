"""@brief 将已完成的采集结果生成为可以离线打开的交互页面。"""

import base64
import json
import re
from pathlib import Path

from har_capture.arkui_selection import read_window_arkui
from har_capture.hdc import fail
from har_capture.metadata import count_nodes, file_info, image_info, load_layout
from har_capture.sessions import parse_sessions
from har_capture.privacy import viewer_tree
from har_capture.system_accessibility import read_window_accessibility

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
    screenshot_file = metadata["files"]["screenshot"]["file"]
    verify_file(directory, metadata["files"]["screenshot"], screenshot_file)
    image = image_info(directory / screenshot_file)
    if any(image[key] != metadata["display"][key] for key in ("width", "height")):
        fail("Screenshot dimensions do not match capture metadata")
    screenshot = base64.b64encode((directory / screenshot_file).read_bytes()).decode("ascii")
    trees = read_trees(directory, metadata)
    from har_capture.capture_facts import verify_screen_geometry
    verify_screen_geometry(directory, metadata)
    trees = {"accessibility": {key: viewer_tree(tree) for key, tree in trees["accessibility"].items()},
             "arkui": {key: viewer_tree(tree) for key, tree in trees["arkui"].items()}}
    return {"metadata": metadata, "layout": viewer_tree(load_layout(directory / "layout.json")),
            "screenshot": "data:image/png;base64," + screenshot, "trees": trees}


def read_trees(directory: Path, metadata: dict) -> dict:
    """@brief 校验并加载分别采集的两类树。 @param directory 采集目录。
    @param metadata 采集记录。 @return 无障碍原始数组与按窗口索引的 ArkUI 树。
    """
    verify_file(directory, metadata["files"]["accessibility"], "accessibility.json")
    accessibility = {}
    verify_file(directory, metadata["files"]["session_details"], "session-details.txt")
    sessions = parse_sessions((directory / "session-details.txt").read_text(encoding="utf-8"))
    arkui = {}
    for window in metadata["windows"]:
        window_id = window["window_id"]
        accessibility[str(window_id)] = read_window_accessibility(directory, window)
        evidence = window["trees"]["arkui"]
        verify_arkui_scope(window, evidence, sessions)
        arkui[str(window_id)] = read_window_arkui(directory, window)
    if metadata.get("delivery"):
        manifest = metadata["delivery"]
        record = manifest["files"]["windows_akui"]
        destination = Path(manifest["path"])
        verify_file(destination, record, record["file"])
        normalized = json.loads((destination / record["file"]).read_bytes())
        for item in normalized["windows"]:
            arkui[str(item["window_id"])] = item["tree"]
    return {"accessibility": accessibility, "arkui": arkui}


def verify_arkui_scope(window: dict, evidence: dict, sessions: dict) -> None:
    """@brief 核对组件树范围的系统会话依据。 @param window 窗口记录。
    @param evidence 组件树元数据。 @param sessions 系统会话原始记录。
    """
    window_id = window["window_id"]
    if window_id not in sessions:
        fail(f"Session details are missing window {window_id}")
    session = sessions[window_id]
    source = "WindowManagerService.-default.-all"
    identity_matches = all(evidence["session"].get(key) == value for key, value in session.items())
    if (not identity_matches or evidence["scope"] != "window" or evidence["source"] != source
            or evidence["display_id"] != window["display_id"]
            or any(session[key] != window[key] for key in ("process_id", "display_id"))):
        fail(f"ArkUI scope does not match session details for window {window_id}")


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
