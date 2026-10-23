"""@brief 鸿蒙屏幕及窗口信息采集命令行入口。"""

import argparse
import logging
import tomllib
from pathlib import Path

from har_capture.collector import capture
from har_capture.hdc import Hdc
from har_capture.stability import validate_rules


def arguments() -> argparse.Namespace:
    """@brief 读取必要的采集参数。 @return 命令行参数。"""
    parser = argparse.ArgumentParser(description="Capture HarmonyOS screenshots, accessibility and ArkUI trees.")
    default_hdc = Path(__file__).resolve().parent / ".tools/openharmony/toolchains/hdc.exe"
    parser.add_argument("--hdc", type=Path, default=default_hdc, help="Path to the HDC executable")
    parser.add_argument("--device", help="Connected device serial; required when more than one device is connected")
    parser.add_argument("--display", type=int, default=0, help="Display ID (default: 0)")
    parser.add_argument("--output", type=Path, default=Path("captures"), help="Capture directory (default: captures)")
    parser.add_argument("--project", type=Path, help="Read declared dataset.stability_regions from project TOML")
    parser.add_argument("--target-window-id", type=int, help="Explicit application window for page observation")
    parser.add_argument("--target-bundle-name", help="Explicit application bundle for page observation")
    args = parser.parse_args()
    if args.display < 0:
        logging.error("Display ID must be non-negative")
        parser.error("--display must be non-negative")
    if (args.target_window_id is None) != (args.target_bundle_name is None):
        logging.error("Target window and bundle must be supplied together")
        parser.error("--target-window-id and --target-bundle-name must be supplied together")
    return args


def main() -> int:
    """@brief 执行采集，成功时仅向标准输出打印目录。 @return 进程退出码。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = arguments()
    try:
        project = tomllib.loads(args.project.read_text(encoding="utf-8")) if args.project else {}
        rules = validate_rules(project.get("dataset", {}).get("stability_regions", []))
        target = None if args.target_window_id is None else {
            "window_id": args.target_window_id, "bundle_name": args.target_bundle_name}
        result = capture(Hdc(args.hdc, args.device), args.output, args.display, rules, target)
    except KeyboardInterrupt:
        logging.error("Capture interrupted")
        return 130
    except Exception as error:
        logging.error("Capture did not complete: %s", error)
        return 1
    print(result["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
