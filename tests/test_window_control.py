"""验证窗口按钮的确定性选择以及成功、失败时两端临时文件清理。"""

import json
import unittest
from unittest.mock import Mock

from har_capture.agent_device import execute_operation
from har_capture.hdc import CaptureError, Hdc
from har_capture.window_control import select_window_control, window_size_control


def control_node(window_id="92", control_id="EnhanceMaximizeBtn"):
    """返回已匿名化的真实标题栏按钮格式。"""
    return {"attributes": {"hostWindowId": window_id, "id": control_id, "bounds": "[2816,23][2892,99]",
                            "visible": "true", "enabled": "true", "clickable": "true", "accessibilityId": "594"},
            "children": []}


def layout_data():
    """加入相邻危险按钮以及其他窗口同名按钮，防止按视觉或数组顺序猜测。"""
    return {"attributes": {}, "children": [control_node("80"), control_node(control_id="EnhanceMinimizeBtn"),
                                              control_node(), control_node(control_id="EnhanceCloseBtn")]}


def fake_hdc(layout=None, failure=None):
    """提供纯本地设备替身，记录本次接收文件路径。"""
    hdc = Mock(spec=Hdc)
    hdc.local_path = None
    hdc.remote_paths = []

    def run(arguments, name):
        hdc.remote_paths.append(arguments)
        if failure == name:
            raise CaptureError("Injected device failure")
        if name == "window_control_layout":
            return "DumpLayout saved to:" + arguments[-1]
        if name == "cleanup_window_control":
            return "HAR_WINDOW_CONTROL_CLEANUP_OK\n"
        raise AssertionError(f"Unexpected operation: {name}")

    def receive(remote, local, name):
        hdc.local_path = local
        local.write_text(json.dumps(layout if layout is not None else layout_data()), encoding="utf-8")
        if failure == name:
            raise CaptureError("Injected transfer failure")

    hdc.run.side_effect = run
    hdc.receive.side_effect = receive
    return hdc


class WindowControlTests(unittest.TestCase):
    """不连接设备地验证窗口操作定位边界。"""

    def test_selects_exact_window_and_control_with_original_accessibility_identity(self):
        result = select_window_control(layout_data(), 92)
        self.assertEqual(result, {"window_id": 92, "control_id": "EnhanceMaximizeBtn",
                                  "bounds": {"x": 2816, "y": 23, "width": 76, "height": 76},
                                  "center": {"x": 2854, "y": 61}, "accessibility_id": "594"})

    def test_missing_or_ambiguous_control_is_rejected_without_an_alternative(self):
        absent = layout_data()
        absent["children"] = [node for node in absent["children"] if node["attributes"]["id"] != "EnhanceMaximizeBtn"]
        duplicate = layout_data()
        duplicate["children"].append(control_node())
        for data in (absent, duplicate):
            with self.subTest(data=data), self.assertRaises(CaptureError):
                select_window_control(data, 92)

    def test_each_actionable_state_must_be_the_official_true_string(self):
        for field in ("visible", "enabled", "clickable"):
            for value in ("false", True, "", None):
                data = layout_data()
                data["children"][2]["attributes"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(CaptureError):
                    select_window_control(data, 92)

    def test_empty_invalid_bounds_and_missing_accessibility_id_are_rejected(self):
        for field, value in (("bounds", "[10,10][10,20]"), ("bounds", "[10,10][20,10]"),
                             ("bounds", "[20,10][10,20]"), ("bounds", "not-bounds"),
                             ("accessibilityId", ""), ("accessibilityId", 594)):
            data = layout_data()
            data["children"][2]["attributes"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(CaptureError):
                select_window_control(data, 92)

    def test_invalid_window_ids_never_issue_a_device_command(self):
        for value in (None, True, 0, -1, "92"):
            hdc = fake_hdc()
            with self.subTest(value=value), self.assertRaises(CaptureError):
                window_size_control(hdc, {"window_id": value})
            hdc.run.assert_not_called()

    def test_success_cleans_remote_and_local_json_without_screen_capture(self):
        hdc = fake_hdc()
        result = window_size_control(hdc, {"window_id": 92})
        self.assertEqual(result["center"], {"x": 2854, "y": 61})
        self.assertFalse(hdc.local_path.exists())
        self.assertFalse(hdc.local_path.parent.exists())
        arguments, name = hdc.run.call_args_list[0].args
        self.assertEqual(arguments[:8], ["shell", "uitest", "dumpLayout", "-d", "0", "-m", "false", "-p"])
        self.assertEqual(name, "window_control_layout")
        cleanup, cleanup_name = hdc.run.call_args_list[-1].args
        self.assertEqual(cleanup[3], arguments[-1])
        self.assertEqual(cleanup_name, "cleanup_window_control")
        self.assertNotIn("screenCap", str(hdc.run.call_args_list))

    def test_lookup_and_transfer_failures_still_clean_both_sides(self):
        no_control = {"attributes": {}, "children": [control_node(control_id="EnhanceMinimizeBtn")]}
        for data, failure in ((no_control, None), (layout_data(), "receive_window_control_layout")):
            hdc = fake_hdc(data, failure)
            with self.subTest(failure=failure), self.assertRaises(CaptureError):
                window_size_control(hdc, {"window_id": 92})
            self.assertFalse(hdc.local_path.exists())
            self.assertFalse(hdc.local_path.parent.exists())
            self.assertEqual(hdc.run.call_args.args[1], "cleanup_window_control")

    def test_dump_failure_still_attempts_remote_cleanup(self):
        hdc = fake_hdc(failure="window_control_layout")
        with self.assertRaises(CaptureError):
            window_size_control(hdc, {"window_id": 92})
        hdc.receive.assert_not_called()
        self.assertEqual(hdc.run.call_args.args[1], "cleanup_window_control")

    def test_remote_cleanup_failure_prevents_a_successful_control_result(self):
        hdc = fake_hdc(failure="cleanup_window_control")
        with self.assertRaises(CaptureError):
            window_size_control(hdc, {"window_id": 92})
        self.assertFalse(hdc.local_path.exists())
        self.assertFalse(hdc.local_path.parent.exists())

    def test_operation_dispatch_preserves_the_small_result_contract(self):
        hdc = fake_hdc()
        response = execute_operation(hdc, {"operation": "window_size_control", "window_id": 92})
        self.assertEqual(response["operation"], "window_size_control")
        self.assertEqual(response["result"]["accessibility_id"], "594")
        hdc.connect.assert_called_once_with()
        with self.assertRaises(CaptureError):
            execute_operation(hdc, {"operation": "window_size_control", "window_id": 92, "control_id": "other"})


if __name__ == "__main__":
    unittest.main()
