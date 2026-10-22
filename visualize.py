"""@brief 生成鸿蒙采集结果的交互可视化页面。"""

import argparse
import logging
from pathlib import Path

from har_capture.visualization import build_viewer, latest_capture


def main() -> int:
    """@brief 生成指定采集或最新采集的页面。 @return 进程退出码。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Build an offline viewer for a completed HarmonyOS capture.")
    parser.add_argument("capture", nargs="?", type=Path, help="Capture directory; defaults to the latest in captures/")
    args = parser.parse_args()
    try:
        directory = args.capture if args.capture is not None else latest_capture(Path("captures"))
        destination = build_viewer(directory)
    except Exception as error:
        logging.error("Visualization did not complete: %s", error)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
