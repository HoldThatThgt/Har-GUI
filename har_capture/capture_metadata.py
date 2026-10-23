"""@brief 按采集时保存的原始元数据核验发布证据，不重算历史坐标。"""

import json
from pathlib import Path

from har_capture.hdc import fail


def verify_captured_metadata(directory: Path, metadata: dict) -> None:
    """@brief 核对采集阶段拥有的窗口事实与原文件索引。 @param directory 原始证据目录。
    @param metadata 发布阶段的元数据。
    """
    path = directory / "capture-evidence.json"
    if path.is_symlink() or not path.is_file():
        fail("Original capture metadata is missing or is a symbolic link")
    try:
        original = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        fail(f"Cannot read original capture metadata: {type(error).__name__}")
    required = {"display", "files", "windows", "applications", "foreground_window_ids_without_tree",
                "consistency", "window_inventory", "coverage", "stability"}
    if not isinstance(original, dict) or not required.issubset(original):
        fail("Original capture metadata is incomplete")
    if any(metadata.get(key) != value for key, value in original.items() if key != "files"):
        fail("Published metadata differs from the original captured window facts")
    before = {key: value for key, value in original["files"].items() if key != "screenshot"}
    current = {key: value for key, value in metadata["files"].items() if key != "screenshot"}
    if current != before:
        fail("Published metadata file index differs from its original capture")
    if original["files"]["screenshot"]["sha256"] != metadata["privacy"]["before_sha256"]:
        fail("Published privacy receipt differs from the original screenshot")
