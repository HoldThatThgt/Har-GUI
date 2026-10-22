"""@brief 执行 HDC 命令并保存时间与错误证据。"""

import logging
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class CaptureError(RuntimeError):
    """@brief 表示无法完成一次有效采集。"""


def fail(message: str) -> None:
    """@brief 记录错误并中止当前采集。 @param message 英文错误说明。"""
    LOGGER.error(message)
    raise CaptureError(message)


def utc_now() -> str:
    """@brief 返回带时区的主机 UTC 时间。 @return ISO 8601 时间字符串。"""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Hdc:
    """@brief 管理单个设备连接及本次调用的命令记录。"""

    def __init__(self, executable: Path, device: str | None, timeout: float = 60.0):
        """@brief 初始化连接参数。 @param executable HDC 路径。 @param device 设备标识。
        @param timeout 每条命令的超时秒数。
        """
        self.executable = executable.resolve()
        self.device = device
        self.timeout = timeout
        self.records: list[dict] = []

    def run(self, arguments: list[str], name: str, targeted: bool = True) -> str:
        """@brief 执行并记录一条命令。 @param arguments 参数列表。 @param name 操作名称。
        @param targeted 是否指定当前设备。 @return 标准输出文本。
        """
        command = [str(self.executable)]
        if targeted:
            command.extend(["-t", self.device])
        command.extend(arguments)
        record = {"name": name, "command": command, "started_at": utc_now()}
        self.records.append(record)
        started = time.perf_counter()
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            record["error"] = str(error)
            LOGGER.error("HDC operation %s failed: %s", name, error)
            raise CaptureError(f"HDC operation {name} failed: {error}") from error
        finally:
            record.update(finished_at=utc_now(), duration_seconds=time.perf_counter() - started)
        output = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        record.update(return_code=result.returncode, stdout=output, stderr=stderr)
        if result.returncode != 0 or re.search(r"(?mi)^\s*\[Fail\]", output + stderr):
            fail(f"HDC operation {name} failed: {output.strip()} {stderr.strip()}")
        LOGGER.info("%s completed in %.3fs", name, record["duration_seconds"])
        return output

    def connect(self) -> None:
        """@brief 验证指定设备，或选择唯一已连接设备。"""
        if not self.executable.is_file():
            fail(f"HDC executable does not exist: {self.executable}")
        output = self.run(["list", "targets", "-v"], "list_devices", targeted=False)
        connected = []
        for line in output.splitlines():
            columns = line.split()
            if len(columns) >= 3 and columns[2] == "Connected":
                connected.append(columns[0])
        if self.device is not None:
            if self.device not in connected:
                fail(f"Device is not connected or authorized: {self.device}")
        elif len(connected) == 1:
            self.device = connected[0]
        else:
            fail(f"Expected one connected device; found {len(connected)}. Use --device.")

    def device_info(self) -> dict:
        """@brief 读取设备型号、系统版本及采集工具版本。 @return 设备元数据。"""
        keys = {
            "model": "const.product.model",
            "device_type": "const.product.devicetype",
            "os_version": "const.ohos.fullname",
            "api_version": "const.ohos.apiversion",
        }
        info = {"serial": self.device}
        for name, key in keys.items():
            value = self.run(["shell", "param", "get", key], f"device_{name}").strip()
            if not value or "Get parameter" in value:
                fail(f"Device parameter is unavailable: {key}")
            info[name] = value
        info["hdc_version"] = self.run(["-v"], "hdc_version", targeted=False).strip()
        info["uitest_version"] = self.run(["shell", "uitest", "--version"], "uitest_version").strip()
        return info

    def receive(self, remote: str, local: Path, name: str) -> None:
        """@brief 原样接收设备文件。 @param remote 设备路径。 @param local 本地路径。
        @param name 操作名称。
        """
        if local.exists():
            fail(f"Refusing to overwrite an existing artifact: {local}")
        output = self.run(["file", "recv", remote, str(local)], name)
        if "FileTransfer finish" not in output:
            fail(f"HDC did not confirm a complete transfer: {remote}")
        if not local.is_file() or local.stat().st_size == 0:
            fail(f"HDC did not create a nonempty artifact: {local}")
