"""@brief 读取当前系统无障碍布局，并验证两端临时文件的清理。"""

import logging
import tempfile
import uuid
from pathlib import Path

from har_capture.hdc import Hdc, fail
from har_capture.metadata import load_layout

LOGGER = logging.getLogger(__name__)


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
