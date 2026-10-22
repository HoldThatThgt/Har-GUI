"""构造来自真实格式的匿名测试数据及设备替身。"""

import copy
import io
import json
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from har_capture.hdc import CaptureError, Hdc


FIXTURES = Path(__file__).parent / "fixtures"


def window_node(window_id="92", bundle="com.example.settings", z_index="9999"):
    """返回保留窗口归属字段的最小控件树。"""
    return {
        "attributes": {
            "hostWindowId": window_id,
            "bundleName": bundle,
            "displayId": "0",
            "bounds": "[1034,153][2630,1621]",
            "zIndex": z_index,
            "type": "root",
            "visible": "true",
        },
        "children": [],
    }


def layout_data():
    """故意让树的顺序和 zIndex 与窗口管理器顺序不同。"""
    return {
        "attributes": {"bounds": "[0,0][3120,2080]"},
        "children": [
            window_node("80", "com.example.reader", "999999"),
            window_node("92", "com.example.settings", "0"),
        ],
    }


def raw_layout(data=None):
    """用非默认空白和中文测试原始字节是否被改写。"""
    value = copy.deepcopy(data if data is not None else layout_data())
    value["attributes"]["text"] = "原始控件数据"
    return ("\r\n" + json.dumps(value, ensure_ascii=False, indent="\t") + "\r\n").encode("utf-8")


def raw_accessibility(roots=None):
    """返回不经可见边界过滤的原始无障碍窗口数组。"""
    value = copy.deepcopy(roots if roots is not None else layout_data()["children"])
    return ("\r\n" + json.dumps(value, ensure_ascii=False, indent="\t") + "\r\n").encode("utf-8")


def raw_arkui(window_id=92, process_id=202, include_tree=True):
    """保留窗口响应头、组件原文和原始换行。"""
    text = f"WindowName: Example\r\nWinId: {window_id}\r\nPid: {process_id}\r\n"
    if include_tree:
        text += ("|-> root childSize:1\r\n"
                 "    | ID: 0\r\n"
                 "  |-> Text childSize:0\r\n"
                 "        Content: 原始组件数据\r\n"
                 "        AccessibilityId: 7001\r\n"
                 "        Hint: 第一项\r\n"
                 "        Hint: 第二项\r\n")
    else:
        text += "ArkUI dump was written to the application file.\r\n"
    return text.encode("utf-8")


def raw_system_arkui(component_id="session92"):
    """构造包含邻窗和游离根的系统完整树，节点 ID 与无障碍 ID 故意不同。"""
    text = (
        "WindowName: 测试系统窗口\r\nDisplayId: 0\r\nWinId: 92\r\nPid: 202\r\n"
        "|-> root,0,0 childSize:3\r\n"
        "    | ID: 0\r\n"
        "    | AccessibilityId: 0\r\n"
        "  |-> Text,101,101 childSize:0\r\n"
        "        ID: 101\r\n"
        "        Content: 前置兄弟\r\n"
        "  |-> WindowScene,192,920 childSize:1\r\n"
        "      | ID: 192\r\n"
        "      | AccessibilityId: 920\r\n"
        f"      | compid: {component_id}\r\n"
        "      | PaintRect without transform: RectT (1034.00, 153.00) - [1596.00 x 1468.00]\r\n"
        "    |-> Text,193,921 childSize:0\r\n"
        "          ID: 193\r\n"
        "          AccessibilityId: 921\r\n"
        "          Content: 原始系统组件数据\r\n"
        "  |-> WindowScene,194,922 childSize:0\r\n"
        "        ID: 194\r\n"
        "        AccessibilityId: 922\r\n"
        "        compid: session80\r\n"
        "|-> root,9000,9000 childSize:0\r\n"
        "      ID: 9000\r\n"
    )
    return text.encode("utf-8")


def system_session_details():
    """将已知窗口标记为系统会话，不依赖包名或窗口名推断。"""
    details = (FIXTURES / "session-details.txt").read_text(encoding="utf-8")
    return details.replace("202|10000|0|", "202|10000|1|")


def png_bytes(color="white", size=(8, 6)):
    """创建能被完整解码的小型图片。"""
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def fake_hdc(payloads=None, failure=None, command_outputs=None):
    """替代设备访问，同时保留每条调用对应的日志。"""
    hdc = Mock(spec=Hdc)
    hdc.records = []
    hdc.device_info.return_value = {"serial": "test-device", "model": "test-model"}
    files = {"layout": raw_layout(), "screenshot": png_bytes(),
             "accessibility": raw_accessibility(), "arkui_response_92": raw_arkui(),
             "arkui_response_80": raw_arkui(80, 201), "arkui_92": raw_arkui(), "arkui_80": raw_arkui(80, 201)}
    files.update(payloads or {})
    timing = {"started_at": "2026-10-22T12:00:00.000000+00:00",
              "finished_at": "2026-10-22T12:00:00.010000+00:00", "duration_seconds": 0.01}

    def run(arguments, name, targeted=True):
        hdc.records.append({"name": name, "command": arguments, "return_code": 0, **timing})
        if name == failure:
            raise CaptureError("Injected command failure")
        if name in (command_outputs or {}):
            return command_outputs[name]
        if name == "screenshot":
            return "ScreenCap saved to " + arguments[-1]
        if name in ("layout", "accessibility"):
            return "DumpLayout saved to:" + arguments[-1]
        if name.startswith("dump_arkui_"):
            return "HAR_CAPTURE_DUMP_READY\n"
        if name == "windows":
            return (FIXTURES / "windows.txt").read_text(encoding="utf-8")
        if name == "session_details":
            return (FIXTURES / "session-details.txt").read_text(encoding="utf-8")
        if name == "cleanup":
            return "HAR_CAPTURE_CLEANUP_OK\n"
        return ""

    def receive(remote, local, name):
        hdc.records.append({"name": name, "remote": remote, "return_code": 0, **timing})
        if name == failure:
            raise CaptureError("Injected transfer failure")
        local.write_bytes(files[name.removeprefix("receive_")])

    hdc.run.side_effect = run
    hdc.receive.side_effect = receive
    return hdc
