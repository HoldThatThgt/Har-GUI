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
                            "type": "Button", "visible": "true", "enabled": "true", "clickable": "true",
                            "accessibilityId": "594"},
            "children": []}


def container_node(component, bounds, children, window_id="92", identifier=""):
    """构造有明确窗口归属和实际屏幕边界的可见系统容器。"""
    return {"attributes": {"type": component, "bounds": bounds, "hostWindowId": window_id,
                            "visible": "true", "id": identifier}, "children": children}


def window_node(window_id="92", control_id="EnhanceMaximizeBtn"):
    """构造系统装饰层的首个直接标题行及真实按钮行层级。"""
    minimize = control_node(window_id, "EnhanceMinimizeBtn")
    minimize["attributes"]["bounds"] = "[2740,23][2816,99]"
    close = control_node(window_id, "EnhanceCloseBtn")
    close["attributes"]["bounds"] = "[2892,23][2968,99]"
    buttons = container_node("Row", "[2740,0][2970,112]",
                             [minimize, control_node(window_id, control_id), close], window_id,
                             "containerModalButtonRowId")
    row = container_node("Row", "[100,0][3000,112]", [buttons], window_id)
    stack = container_node("Stack", "[100,0][3000,112]", [row], window_id)
    title = container_node("Row", "[100,0][3000,112]", [stack], window_id)
    body = container_node("Row", "[100,112][3000,1800]", [], window_id)
    return container_node("ContainerModal", "[100,0][3000,1800]", [title, body], window_id)


def layout_data(control_id="EnhanceMaximizeBtn"):
    """加入相邻危险按钮以及其他窗口同名按钮，防止按视觉或数组顺序猜测。"""
    return container_node("Root", "[0,0][3120,2080]", [window_node("80"), window_node(control_id=control_id)])


def title_path(data):
    """返回目标窗口从根到按钮行的真实路径，供单一属性损坏测试使用。"""
    path = [data, data["children"][-1]]
    for _ in range(4):
        path.append(path[-1]["children"][0])
    return path


def target_control(data):
    """返回目标系统按钮行中的最大化控件。"""
    return title_path(data)[-1]["children"][1]


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

    def test_selects_each_standard_control_with_original_accessibility_identity(self):
        for control_id in ("EnhanceMaximizeBtn", "EnhanceCompatibleMaximizeBtn"):
            with self.subTest(control_id=control_id):
                result = select_window_control(layout_data(control_id), 92)
                self.assertEqual(result, {"window_id": 92, "control_id": control_id,
                                          "bounds": {"x": 2816, "y": 23, "width": 76, "height": 76},
                                          "center": {"x": 2854, "y": 61}, "accessibility_id": "594"})

    def test_hidden_empty_standard_control_does_not_compete_with_visible_compatible_control(self):
        data = layout_data("EnhanceCompatibleMaximizeBtn")
        hidden = control_node()
        hidden["attributes"].update(visible="false", bounds="[0,0][0,0]")
        title_path(data)[-1]["children"].append(hidden)
        self.assertEqual(select_window_control(data, 92)["control_id"], "EnhanceCompatibleMaximizeBtn")

    def test_missing_or_ambiguous_control_is_rejected_without_an_alternative(self):
        absent = layout_data()
        title_path(absent)[-1]["children"].pop(1)
        duplicate = layout_data()
        title_path(duplicate)[-1]["children"].append(control_node())
        for data in (absent, duplicate):
            with self.subTest(data=data), self.assertRaises(CaptureError):
                select_window_control(data, 92)

    def test_two_actionable_standard_variants_are_ambiguous_in_either_order(self):
        for reverse in (False, True):
            data = layout_data()
            children = title_path(data)[-1]["children"]
            children.append(control_node(control_id="EnhanceCompatibleMaximizeBtn"))
            if reverse:
                children.reverse()
            with self.subTest(reverse=reverse), self.assertRaisesRegex(CaptureError, "found 2"):
                select_window_control(data, 92)

    def test_other_window_and_application_body_cannot_supply_the_system_control(self):
        for control_id in ("EnhanceMaximizeBtn", "EnhanceCompatibleMaximizeBtn"):
            data = layout_data()
            title_path(data)[-1]["children"].pop(1)
            title_path(data)[-1]["children"].append(control_node("80", control_id))
            body = data["children"][-1]["children"][1]
            app_button = control_node(control_id=control_id)
            app_button["attributes"]["bounds"] = "[2816,200][2892,276]"
            body["children"].append(app_button)
            with self.subTest(control_id=control_id), self.assertRaisesRegex(CaptureError, "found 0"):
                select_window_control(data, 92)

    def test_same_name_non_button_is_not_an_actionable_system_control(self):
        for component in ("Text", "Row", "", None):
            data = layout_data()
            target_control(data)["attributes"]["type"] = component
            with self.subTest(component=component), self.assertRaisesRegex(CaptureError, "found 0"):
                select_window_control(data, 92)

    def test_each_actionable_state_must_be_the_official_true_string(self):
        for field in ("visible", "enabled", "clickable"):
            for value in ("false", True, "", None):
                data = layout_data()
                target_control(data)["attributes"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(CaptureError):
                    select_window_control(data, 92)

    def test_empty_invalid_bounds_and_missing_accessibility_id_are_rejected(self):
        for field, value in (("bounds", "[10,10][10,20]"), ("bounds", "[10,10][20,10]"),
                             ("bounds", "[20,10][10,20]"), ("bounds", "not-bounds"),
                             ("bounds", None), ("bounds", "[-100,23][-24,99]"),
                             ("accessibilityId", ""), ("accessibilityId", 594), ("accessibilityId", "invalid")):
            data = layout_data()
            target_control(data)["attributes"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(CaptureError):
                select_window_control(data, 92)

    def test_title_ancestors_require_same_window_and_visibility(self):
        for index in range(1, 6):
            for field, value in (("hostWindowId", "80"), ("visible", "false")):
                data = layout_data()
                title_path(data)[index]["attributes"][field] = value
                with self.subTest(index=index, field=field), self.assertRaises(CaptureError):
                    select_window_control(data, 92)

    def test_control_internal_ancestor_requires_same_window_and_visibility(self):
        for field, value in (("hostWindowId", "80"), ("visible", "false")):
            data = layout_data()
            buttons = title_path(data)[-1]
            wrapper = container_node("Stack", "[2816,23][2892,99]", [buttons["children"][1]])
            buttons["children"][1] = wrapper
            wrapper["attributes"][field] = value
            with self.subTest(field=field), self.assertRaises(CaptureError):
                select_window_control(data, 92)

    def test_every_title_ancestor_must_contain_the_control_center(self):
        for index in range(6):
            data = layout_data()
            title_path(data)[index]["attributes"]["bounds"] = "[0,0][2800,112]"
            with self.subTest(index=index), self.assertRaisesRegex(CaptureError, "clipped"):
                select_window_control(data, 92)

    def test_internal_clipping_obeys_left_closed_right_open_screen_bounds(self):
        for bounds, accepted in (("[2854,61][2892,99]", True), ("[2816,23][2854,99]", False),
                                 ("[2816,23][2892,61]", False), ("[2855,23][2892,99]", False)):
            data = layout_data()
            buttons = title_path(data)[-1]
            buttons["children"][1] = container_node("Stack", bounds, [buttons["children"][1]])
            with self.subTest(bounds=bounds):
                if accepted:
                    self.assertEqual(select_window_control(data, 92)["center"], {"x": 2854, "y": 61})
                else:
                    with self.assertRaisesRegex(CaptureError, "clipped"):
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
        no_control = layout_data()
        title_path(no_control)[-1]["children"].pop(1)
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
