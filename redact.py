"""@brief 独立查看正文定位属性或按 project.toml 对既有采集打码。"""

import argparse
import json
import logging
import tomllib
from pathlib import Path

from har_capture.hdc import fail
from har_capture.privacy import node_inventory, redact_capture


def main() -> int:
    """@brief 执行离线后处理，不接触设备或模型。 @return 退出码。"""
    parser = argparse.ArgumentParser(description="Inspect selectors or apply the project privacy.enabled setting.")
    parser.add_argument("command", choices=("selectors", "apply"))
    parser.add_argument("capture", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--task")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        metadata = json.loads((args.capture / "metadata.json").read_bytes())
        if args.command == "selectors":
            result = node_inventory(args.capture, metadata)
        else:
            if args.project is None or args.task is None:
                fail("Apply requires --project and --task")
            project = tomllib.loads(args.project.read_text(encoding="utf-8"))
            tasks = [task for task in project["tasks"] if task["name"] == args.task]
            if len(tasks) != 1:
                fail("Expected one matching project task")
            result = redact_capture(args.capture, tasks[0]["privacy"])["privacy"]
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except Exception as error:
        logging.error("Redaction failed: %s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
