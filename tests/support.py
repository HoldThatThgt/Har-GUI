"""构造来自真实格式的匿名测试数据及设备替身。"""

import copy
import io
import json
from pathlib import Path
from unittest.mock import Mock

from PIL import Image

from har_capture.hdc import CaptureError, Hdc


FIXTURES = Path(__file__).parent / "fixtures"


def synthetic_screen_geometry(_directory, metadata):
    """为隔离几何采集的匿名单元替身提供明确的一比一屏幕框。"""
    for window in metadata["windows"]:
        window["screen_bounds"] = dict(window["bounding_box"])
        window["screen_bounds_source"] = {"method": "SyntheticUnitFixture"}


def synthetic_closing_geometry(_hdc, windows):
    """隔离独立几何实现的测试使用已声明的同批屏幕外框。"""
    return windows


def save_page_stability(directory, metadata, decision="same_page", evidence="Synthetic target comparison"):
    """以真实观测摘要生成模型替身收据，不触发任何模型或设备。"""
    from har_capture.privacy import atomic_json
    observation = json.loads((directory / "page-observation.json").read_bytes())
    receipt = {"rule_version": 1, "capture_id": metadata["capture_id"], "target": observation["target"],
               "observation_sha256": metadata["files"]["page_observation"]["sha256"],
               "decision": decision, "evidence": evidence}
    atomic_json(directory / "page-stability.json", receipt)
    return receipt


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
        text += ("|-> root,0,0 childSize:1\r\n"
                 "    | ID: 0\r\n"
                 "  |-> Text,1,7001 childSize:0\r\n"
                 "        ID: 1\r\n"
                 "        Content: 原始组件数据\r\n"
                 "        AccessibilityId: 7001\r\n"
                 "        Hint: 第一项\r\n"
                 "        Hint: 第二项\r\n")
    else:
        text += "ArkUI dump was written to the application file.\r\n"
    return text.encode("utf-8")


def native_arkui(window_id=92, process_id=202, bundle="com.example.settings", root=None):
    """构造完整原生响应，应用身份位于首键且每个原始节点都有对应身份。"""
    tree = {"appInfo": {"WindowID": window_id, "BundleName": bundle}}
    tree.update(copy.deepcopy(root if root is not None else {
        "$type": "root", "$ID": 0, "$rect": "[0.00, 0.00],[1596.00,1468.00]",
        "$children": [{"$type": "Text", "$ID": 1}]}))
    return (f"WindowName: Example\nWinId: {window_id}\nPid: {process_id}\n" + json.dumps(tree)).encode()


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


def native_system_arkui():
    """系统完整原生树对应全部本地主根节点，UIContent 宿主与所请求会话身份分开。"""
    root = {"appInfo": {"WindowID": 1, "BundleName": "com.example.system"}, "$ID": 0, "$type": "root",
            "$rect": "[0,0],[3120,2080]", "$children": [
                {"$ID": 101, "$type": "Text"},
                {"$ID": 192, "$type": "WindowScene", "compid": "session92",
                 "$rect": "[1034,153],[2630,1621]", "$children": [{"$ID": 193, "$type": "Text"}]},
                {"$ID": 194, "$type": "WindowScene", "compid": "session80"}]}
    return native_arkui(root=root)


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
    files = {"layout": raw_layout(), "screenshot": png_bytes(size=(3120, 2080)),
             "accessibility": raw_accessibility(), "arkui_response_92": raw_arkui(),
             "arkui_response_80": raw_arkui(80, 201)}
    for window_id, process_id in ((92, 202), (80, 201)):
        bundle = "com.example.settings" if window_id == 92 else "com.example.reader"
        files[f"geometry_{window_id}"] = native_arkui(window_id, process_id, bundle)
    files["render_geometry"] = b"| ROOT_NODE[1], Properties: Bounds[0 0 1 1] Frame[0 0 1 1], OtherModifiers:[]\n"
    files["render_detached_geometry"] = b"-- Render Node Not On Tree\n"
    files.update(payloads or {})
    timing = {"started_at": "2026-10-22T12:00:00.000000+00:00",
              "finished_at": "2026-10-22T12:00:00.010000+00:00", "duration_seconds": 0.01}

    def run(arguments, name, targeted=True):
        hdc.records.append({"name": name, "command": arguments, "return_code": 0, **timing})
        if name == failure:
            raise CaptureError("Injected command failure")
        if name in (command_outputs or {}):
            return command_outputs[name]
        if name in ("screenshot", "closing_screen"):
            return "ScreenCap saved to " + arguments[-1]
        if name in ("layout", "accessibility", "closing_layout"):
            return "DumpLayout saved to:" + arguments[-1]
        if name.startswith("dump_arkui_"):
            return "HAR_CAPTURE_DUMP_READY\n"
        if name.startswith("geometry_"):
            return "HAR_CAPTURE_GEOMETRY_READY\n"
        if name in ("render_geometry", "render_detached_geometry"):
            return "HAR_CAPTURE_RENDER_READY\n"
        if name in ("windows", "closing_windows"):
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
        key = name.removeprefix("receive_")
        if key in ("closing_layout", "closing_screen") and key not in files:
            key = {"closing_layout": "layout", "closing_screen": "screenshot"}[key]
        local.write_bytes(files[key])

    hdc.run.side_effect = run
    hdc.receive.side_effect = receive
    return hdc
