"""验证系统标题栏的严格祖先关系、拖动边界及只读设备操作。"""

import copy
import json
import unittest
from unittest.mock import Mock

from har_capture.agent_device import execute_operation
from har_capture.hdc import CaptureError, Hdc
from har_capture.window_title_bar import select_window_title_bar, window_title_bar


def node(kind, bounds, children=None, identity="", window_id="92"):
    """构建匿名化且保持官方字段格式的节点。"""
    return {"attributes": {"type": kind, "bounds": bounds, "id": identity,
                            "hostWindowId": window_id, "visible": "true"},
            "children": [] if children is None else children}


def window_tree(window_id="92"):
    """仅放入关闭按钮，证明标题栏定位与最大化能力无关。"""
    buttons = node("Row", "[780,100][900,180]", [node("Button", "[820,120][880,160]",
                   identity="EnhanceCloseBtn", window_id=window_id)], "containerModalButtonRowId", window_id)
    row = node("Row", "[100,100][900,180]", [buttons], window_id=window_id)
    stack = node("Stack", "[100,100][900,180]", [row], window_id=window_id)
    title = node("Row", "[100,100][900,180]", [stack], window_id=window_id)
    application_title = node("HdsTitleBar", "[100,180][900,250]", window_id=window_id)
    modal = node("ContainerModal", "[100,100][900,700]", [title, application_title], window_id=window_id)
    return node("root", "[100,100][900,700]", [modal], window_id=window_id)


def layout_data():
    """包含其他窗口同名系统装饰层，避免按遍历顺序选取。"""
    return node("", "[0,0][1200,900]", [window_tree("80"), window_tree()], window_id="")


def target_nodes(layout):
    """返回目标窗口的装饰层、标题栏及按钮行。"""
    modal = layout["children"][1]["children"][0]
    title = modal["children"][0]
    buttons = title["children"][0]["children"][0]["children"][0]
    return modal, title, buttons


def fake_hdc(layout=None, failure=None):
    """提供仅接受临时布局采集、接收和清理的设备替身。"""
    hdc = Mock(spec=Hdc)
    hdc.local_path = None

    def run(arguments, name):
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


class WindowTitleBarTests(unittest.TestCase):
    """不连接设备地验证只读系统标题栏定位。"""

    def test_selects_system_title_bar_without_maximize_button_and_keeps_layout(self):
        layout = layout_data()
        original = copy.deepcopy(layout)
        self.assertEqual(select_window_title_bar(layout, 92), {
            "window_id": 92, "bounds": {"x": 100, "y": 100, "width": 800, "height": 600},
            "title_bar_bounds": {"x": 100, "y": 100, "width": 800, "height": 80},
            "button_row_bounds": {"x": 780, "y": 100, "width": 120, "height": 80},
            "drag_point": {"x": 130, "y": 140},
        })
        self.assertEqual(layout, original)

    def test_missing_hidden_or_duplicate_button_row_is_rejected(self):
        for variation in ("missing", "hidden", "duplicate", "wrong_type"):
            data = layout_data()
            modal, title, buttons = target_nodes(data)
            if variation == "missing":
                buttons["attributes"]["id"] = "AppTitleBar"
            elif variation == "hidden":
                buttons["attributes"]["visible"] = "false"
            elif variation == "duplicate":
                title["children"].append(copy.deepcopy(buttons))
            else:
                buttons["attributes"]["type"] = "HdsTitleBar"
            with self.subTest(variation=variation), self.assertRaises(CaptureError):
                select_window_title_bar(data, 92)

    def test_content_column_before_the_first_direct_row_is_not_mistaken_for_title_bar(self):
        data = layout_data()
        modal = target_nodes(data)[0]
        modal["children"].insert(0, node("Column", "[100,100][900,700]"))
        self.assertEqual(select_window_title_bar(data, 92)["drag_point"], {"x": 130, "y": 140})

    def test_container_modal_ancestry_must_be_unique_visible_and_owned_by_window(self):
        for variation in ("missing", "nested", "hidden", "wrong_window"):
            data = layout_data()
            modal, title, buttons = target_nodes(data)
            if variation == "missing":
                modal["attributes"]["type"] = "Column"
            elif variation == "nested":
                title["children"][0]["attributes"]["type"] = "ContainerModal"
            elif variation == "hidden":
                title["attributes"]["visible"] = "false"
            else:
                title["children"][0]["attributes"]["hostWindowId"] = "80"
            with self.subTest(variation=variation), self.assertRaises(CaptureError):
                select_window_title_bar(data, 92)

    def test_button_row_must_belong_to_the_first_direct_container_modal_row(self):
        for variation in ("second_child", "wrong_type", "direct_button_row"):
            data = layout_data()
            modal, title, buttons = target_nodes(data)
            if variation == "second_child":
                modal["children"].insert(0, node("Row", "[100,100][900,180]"))
            elif variation == "wrong_type":
                title["attributes"]["type"] = "Column"
            else:
                modal["children"] = [buttons]
            with self.subTest(variation=variation), self.assertRaises(CaptureError):
                select_window_title_bar(data, 92)

    def test_drag_point_must_be_on_screen_inside_window_and_title_and_outside_buttons(self):
        for target, bounds in ((0, "[100,100][130,700]"), (1, "[150,100][900,180]"),
                               (2, "[120,100][900,180]"), (0, "[-100,100][900,700]"),
                               (1, "[100,-100][900,-10]"), (1, "[100,100][900,100]"),
                               (2, "[780,100][780,180]"), (0, None)):
            data = layout_data()
            target_nodes(data)[target]["attributes"]["bounds"] = bounds
            with self.subTest(target=target, bounds=bounds), self.assertRaises(CaptureError):
                select_window_title_bar(data, 92)

    def test_drag_point_clipped_by_screen_or_window_root_is_rejected(self):
        for is_screen in (True, False):
            data = layout_data()
            ancestor = data if is_screen else data["children"][1]
            ancestor["attributes"]["bounds"] = "[140,100][900,700]"
            with self.subTest(is_screen=is_screen), self.assertRaises(CaptureError):
                select_window_title_bar(data, 92)

    def test_malformed_tree_fails_without_guessing_from_app_title_bar(self):
        for bad in ({}, {"attributes": {}, "children": "not-a-list"}, None):
            data = layout_data()
            data["children"].append(bad)
            with self.subTest(bad=bad), self.assertRaises(CaptureError):
                select_window_title_bar(data, 92)

    def test_invalid_window_ids_never_query_device(self):
        for value in (None, True, 0, -1, "92"):
            hdc = fake_hdc()
            with self.subTest(value=value), self.assertRaises(CaptureError):
                window_title_bar(hdc, {"window_id": value})
            hdc.run.assert_not_called()

    def test_dispatch_returns_title_bar_and_removes_all_temporary_files(self):
        hdc = fake_hdc()
        response = execute_operation(hdc, {"operation": "window_title_bar", "window_id": 92})
        self.assertEqual(response["operation"], "window_title_bar")
        self.assertEqual(response["result"]["drag_point"], {"x": 130, "y": 140})
        hdc.connect.assert_called_once_with()
        self.assertFalse(hdc.local_path.exists())
        self.assertFalse(hdc.local_path.parent.exists())
        arguments, name = hdc.run.call_args_list[0].args
        self.assertEqual(arguments[:8], ["shell", "uitest", "dumpLayout", "-d", "0", "-m", "false", "-p"])
        self.assertEqual(name, "window_control_layout")
        self.assertEqual(hdc.run.call_args.args[0][3], arguments[-1])
        self.assertEqual(hdc.run.call_count, 2)

    def test_dump_transfer_selection_and_cleanup_errors_fail_with_remote_cleanup(self):
        for failure in ("window_control_layout", "receive_window_control_layout", "cleanup_window_control", None):
            data = layout_data()
            target_nodes(data)[2]["attributes"]["visible"] = "false"
            hdc = fake_hdc(data, failure)
            with self.subTest(failure=failure), self.assertRaises(CaptureError):
                window_title_bar(hdc, {"window_id": 92})
            self.assertEqual(hdc.run.call_args.args[1], "cleanup_window_control")
            if hdc.local_path is not None:
                self.assertFalse(hdc.local_path.exists())
                self.assertFalse(hdc.local_path.parent.exists())

    def test_operation_rejects_custom_selectors_and_coordinates(self):
        hdc = fake_hdc()
        with self.assertRaises(CaptureError):
            execute_operation(hdc, {"operation": "window_title_bar", "window_id": 92, "x": 130})
        hdc.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
