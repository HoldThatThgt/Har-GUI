"""@brief 从当前无障碍树唯一定位窗口最大化或还原按钮，不执行界面操作。"""

import logging
import tempfile
import uuid
from pathlib import Path

from har_capture.hdc import Hdc, fail
from har_capture.metadata import layout_bounding_box, load_layout

LOGGER = logging.getLogger(__name__)
CONTROL_ID = "EnhanceMaximizeBtn"


def select_window_control(layout: dict, window_id: int) -> dict:
    """@brief 严格选取当前窗口唯一可操作按钮。 @param layout 当前原始树。
    @param window_id 目标窗口。 @return 控件身份、矩形及中心坐标。
    """
    if type(window_id) is not int or window_id <= 0:
        fail("window_id must be a positive integer")
    pending, matches = [layout], []
    while pending:
        node = pending.pop()
        if not isinstance(node, dict) or not isinstance(node.get("attributes"), dict):
            fail("Invalid accessibility node while locating the window size control")
        if not isinstance(node.get("children"), list):
            fail("Invalid accessibility children while locating the window size control")
        attributes = node["attributes"]
        if attributes.get("hostWindowId") == str(window_id) and attributes.get("id") == CONTROL_ID:
            if all(attributes.get(field) == "true" for field in ("visible", "enabled", "clickable")):
                matches.append(attributes)
        pending.extend(node["children"])
    if len(matches) != 1:
        fail(f"Expected one actionable {CONTROL_ID} in window {window_id}; found {len(matches)}")
    attributes = matches[0]
    bounds = layout_bounding_box(attributes.get("bounds", ""))
    if bounds["width"] <= 0 or bounds["height"] <= 0:
        fail("Window size control has empty bounds")
    accessibility_id = attributes.get("accessibilityId")
    if not isinstance(accessibility_id, str) or not accessibility_id.isdecimal():
        fail("Window size control has no valid accessibility ID")
    return {"window_id": window_id, "control_id": CONTROL_ID, "bounds": bounds,
            "center": {"x": bounds["x"] + bounds["width"] // 2, "y": bounds["y"] + bounds["height"] // 2},
            "accessibility_id": accessibility_id}


def cleanup_layout(hdc: Hdc, remote: str) -> None:
    """@brief 清理本次唯一设备临时文件并验证完成。 @param hdc 连接。 @param remote 精确临时路径。"""
    marker = "HAR_WINDOW_CONTROL_CLEANUP_OK"
    output = hdc.run(["shell", "rm", "-f", remote, "&&", "echo", marker], "cleanup_window_control")
    if marker not in output.splitlines():
        fail("Device did not confirm window control temporary file cleanup")


def read_current_window_layout(hdc: Hdc) -> dict:
    """@brief 读取当前过滤后布局并清除两端临时文件。 @param hdc 连接。 @return 当前无障碍布局。"""
    remote = f"/data/local/tmp/har-window-control-{uuid.uuid4().hex}.json"
    try:
        with tempfile.TemporaryDirectory(prefix="har-window-control-") as directory:
            local = Path(directory) / "layout.json"
            try:
                output = hdc.run(["shell", "uitest", "dumpLayout", "-d", "0", "-m", "false", "-p", remote],
                                 "window_control_layout")
                if f"DumpLayout saved to:{remote}" not in output:
                    fail("UITest did not confirm the window control layout dump")
                hdc.receive(remote, local, "receive_window_control_layout")
                return load_layout(local)
            finally:
                cleanup_layout(hdc, remote)
    except Exception as error:
        LOGGER.error("Current window layout lookup failed: %s", error)
        raise


def window_size_control(hdc: Hdc, request: dict) -> dict:
    """@brief 从最新布局返回最大化或还原按钮。 @param hdc 连接。
    @param request 含窗口标识的请求。 @return 当前唯一最大化或还原按钮。
    """
    window_id = request.get("window_id")
    if type(window_id) is not int or window_id <= 0:
        fail("window_id must be a positive integer")
    return select_window_control(read_current_window_layout(hdc), window_id)
