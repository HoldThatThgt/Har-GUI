"""@brief 通过单个 JSON 请求执行已注册设备操作，标准输出仅包含 JSON。"""

import argparse
import json
import logging
import sys
from pathlib import Path

from har_capture.agent_device import execute_operation
from har_capture.hdc import Hdc
from har_capture.native_input import NativeInputError


def arguments() -> argparse.Namespace:
    """@brief 读取连接参数，不允许传入任意设备命令。 @return 命令行参数。"""
    parser = argparse.ArgumentParser(description="Run one registered HarmonyOS device operation from JSON stdin.")
    parser.add_argument("--hdc", type=Path, help="Path to HDC; required for device operations")
    parser.add_argument("--device", help="Device serial; required for device operations")
    return parser.parse_args()


def main() -> int:
    """@brief 执行单次请求并以退出码及 JSON 表达结果。 @return 进程退出码。"""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = arguments()
    operation = None
    try:
        request = json.load(sys.stdin)
        operation = request.get("operation") if isinstance(request, dict) else None
        connection = Hdc(args.hdc, args.device) if args.hdc and args.device else None
        response = execute_operation(connection, request)
    except KeyboardInterrupt:
        logging.error("Device operation interrupted")
        response = {"operation": operation, "error": {"message": "Device operation interrupted"}}
        print(json.dumps(response, ensure_ascii=True))
        return 130
    except Exception as error:
        logging.error("Device operation failed: %s", error)
        response = {"operation": operation, "error": {"message": str(error)}}
        if isinstance(error, NativeInputError):
            response["error"]["diagnostics"] = error.diagnostics
        print(json.dumps(response, ensure_ascii=True))
        return 1
    print(json.dumps(response, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
