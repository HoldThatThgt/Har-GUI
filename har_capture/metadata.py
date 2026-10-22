"""@brief 解析官方窗口列表并关联未经修改的控件树。"""

import hashlib
import json
import math
import re
from pathlib import Path

from PIL import Image

from har_capture.hdc import fail

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
WINDOW_COLUMNS = (
    "WindowName DisplayId Pid WinId Type Mode Flag ZOrd Orientation "
    "[ x y w h ] [ OffsetX OffsetY ] [ ScaleX ScaleY PivotX PivotY ]"
).split()
WINDOW_ROW = re.compile(
    r"^(?P<name>.+?)\s+(?P<display>\d+)\s+(?P<pid>\d+)\s+(?P<id>\d+)\s+"
    r"(?P<type>\d+)\s+(?P<mode>\d+)\s+(?P<flag>\d+)\s+(?P<z>-?\d+)\s+"
    r"(?P<orientation>\d+)\s+\[\s*(?P<x>-?\d+)\s+(?P<y>-?\d+)\s+"
    r"(?P<width>\d+)\s+(?P<height>\d+)\s*\]\s*"
    r"\[ (?P<offsets>[^\]\r\n]{16})\]\s*"
    r"\[ (?P<scales>[^\]\r\n]{32})\]\s*$"
)
BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def bounding_box(values: list[int]) -> dict:
    """@brief 构造屏幕像素坐标矩形。 @param values x、y、宽、高。 @return 矩形字段。"""
    return dict(zip(("x", "y", "width", "height"), values, strict=True))


def parse_window_transform(match: re.Match) -> dict:
    """@brief 按官方每字段八列格式解析浮点变换，保留粘连字段边界。 @param match 窗口行。 @return 变换字段。"""
    transform = {}
    groups = (("offsets", ("offset_x", "offset_y")), ("scales", ("scale_x", "scale_y", "pivot_x", "pivot_y")))
    for group, keys in groups:
        for index, key in enumerate(keys):
            # DumpSessionInfo 使用 GetFloatWidth(8) 截断文本，再由 std::left 和 std::setw(8) 补空格。
            value = match[group][index * 8:(index + 1) * 8].rstrip(" ")
            if re.fullmatch(NUMBER, value) is None:
                fail(f"Invalid fixed-width window transform {key}: {value!r}")
            transform[key] = float(value)
    if not all(math.isfinite(value) for value in transform.values()):
        fail(f"Non-finite window transform: {match['id']}")
    return transform


def parse_window_row(match: re.Match) -> dict:
    """@brief 转换单条窗口管理器记录。 @param match 匹配结果。 @return 窗口字段。"""
    return {
        "window_id": int(match["id"]),
        "window_name": match["name"].strip(),
        "display_id": int(match["display"]),
        "process_id": int(match["pid"]),
        "window_type": int(match["type"]),
        "window_mode": int(match["mode"]),
        "z_order": int(match["z"]),
        "bounding_box": bounding_box([int(match[key]) for key in ("x", "y", "width", "height")]),
        "transform": parse_window_transform(match),
    }


def parse_windows(text: str) -> dict[int, dict]:
    """@brief 严格解析当前已验证的窗口表格式。 @param text 原始输出。 @return 按窗口 ID 索引的记录。"""
    windows = {}
    in_table = False
    for line in text.splitlines():
        if line.startswith("WindowName"):
            if line.split() != WINDOW_COLUMNS:
                fail("Unsupported WindowManagerService table header")
            in_table = True
            continue
        if not in_table or not line.strip() or line.startswith("-"):
            continue
        if line.startswith("Focus window:"):
            break
        match = WINDOW_ROW.match(line)
        if match is None:
            fail(f"Unrecognized WindowManagerService row: {line}")
        window = parse_window_row(match)
        window_id = window["window_id"]
        if window_id in windows:
            fail(f"Duplicate window ID in WindowManagerService output: {window_id}")
        windows[window_id] = window
    if not in_table or not windows:
        fail("WindowManagerService did not return a window table")
    return windows


def load_layout(path: Path) -> dict:
    """@brief 验证控件树顶层结构，保持文件字节不变。 @param path 原始 JSON 文件。 @return 控件树。"""
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or not isinstance(data.get("attributes"), dict):
        fail("Layout must have an attributes object")
    if not isinstance(data.get("children"), list) or not data["children"]:
        fail("Layout does not contain any window trees")
    return data


def layout_bounding_box(value: str) -> dict:
    """@brief 转换控件树矩形。 @param value 官方 bounds 字符串。 @return 屏幕像素坐标矩形。"""
    match = BOUNDS.fullmatch(value)
    if match is None:
        fail(f"Invalid layout bounds: {value!r}")
    left, top, right, bottom = map(int, match.groups())
    if right < left or bottom < top:
        fail(f"Invalid layout bounds dimensions: {value}")
    return bounding_box([left, top, right - left, bottom - top])


def count_nodes(root: dict, window_id: int, display_id: int) -> int:
    """@brief 验证整棵子树的窗口归属并统计节点。 @param root 窗口根。
    @param window_id 窗口 ID。 @param display_id 显示器 ID。 @return 节点数量。
    """
    pending, count = [root], 0
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or not isinstance(node.get("attributes"), dict):
            fail(f"Invalid node in window {window_id}")
        attrs = node["attributes"]
        if attrs.get("hostWindowId") != str(window_id) or attrs.get("displayId") != str(display_id):
            fail(f"Node ownership does not match window {window_id} on display {display_id}")
        children = node.get("children")
        if not isinstance(children, list):
            fail(f"Invalid node children in window {window_id}")
        pending.extend(children)
        count += 1
    return count


def associate_window(node: dict, index: int, inventory: dict, display_id: int) -> dict:
    """@brief 按窗口 ID 关联控件树与窗口管理器。 @param node 窗口根。 @param index 根的数组位置。
    @param inventory 窗口管理器记录。 @param display_id 目标显示器。 @return 关联后的窗口信息。
    """
    if not isinstance(node, dict) or not isinstance(node.get("attributes"), dict):
        fail("Invalid layout window root")
    attrs = node["attributes"]
    raw_id = attrs.get("hostWindowId", "")
    if not isinstance(raw_id, str) or not raw_id.isdecimal():
        fail(f"Invalid layout window ID: {raw_id!r}")
    window_id = int(raw_id)
    bundle_name = attrs.get("bundleName")
    if not isinstance(bundle_name, str) or not bundle_name.strip():
        fail(f"Missing bundle name for window {window_id}")
    if window_id not in inventory:
        fail(f"Window {window_id} is missing from WindowManagerService output")
    window = dict(inventory[window_id])
    if window["display_id"] != display_id or window["z_order"] < 0:
        fail(f"Window {window_id} is no longer foreground on display {display_id}")
    window.update(
        bundle_name=bundle_name,
        layout_bounding_box=layout_bounding_box(attrs.get("bounds", "")),
        node_count=count_nodes(node, window_id, display_id),
        layout={"file": "layout.json", "json_pointer": f"/children/{index}"},
    )
    return window


def associate_windows(layout: dict, inventory: dict, display_id: int) -> list[dict]:
    """@brief 关联所有窗口并按从前到后排列。 @param layout 控件树。 @param inventory 窗口列表。
    @param display_id 目标显示器。 @return 具有完整必需字段的窗口列表。
    """
    windows = [associate_window(node, index, inventory, display_id)
               for index, node in enumerate(layout["children"])]
    if len({window["window_id"] for window in windows}) != len(windows):
        fail("Layout contains duplicate window roots")
    return sorted(windows, key=lambda window: window["z_order"], reverse=True)


def file_info(path: Path) -> dict:
    """@brief 计算原始文件大小和摘要。 @param path 文件路径。 @return 文件元数据。"""
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": digest}


def image_info(path: Path) -> dict:
    """@brief 使用 Pillow 验证完整 PNG 文件。 @param path 截图路径。 @return 图片及文件元数据。"""
    with Image.open(path) as screenshot:
        if screenshot.format != "PNG":
            fail(f"Screenshot is not a PNG file: {path}")
        width, height = screenshot.size
        screenshot.verify()
    with Image.open(path) as screenshot:
        screenshot.load()
    return {**file_info(path), "width": width, "height": height}
