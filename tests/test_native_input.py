"""验证原生粘贴的范围绑定、模式与完整文本读回。"""

import copy
import contextlib
import hashlib
import io
import logging
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import traceback
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from har_capture.hdc import CaptureError
from har_capture.native_input import (
    apply_input, focused_component, input_text, run_native_driver, target_window, validate_input,
)


class NativeInputTests(unittest.TestCase):
    def setUp(self):
        self.request = {"x": 50, "y": 40, "window_id": 100, "display_id": 0,
                        "bundle_name": "app.browser", "mode": "replace", "value": "https://example.test/full"}
        self.window = {"window_id": 100, "display_id": 0, "bundle_name": "app.browser", "is_system": False,
                       "z_order": 100, "process_id": 42, "ui_node_id": 88, "transform": {},
                       "bounding_box": {"x": 0, "y": 0, "width": 200, "height": 100}}
        self.properties = {"hostWindowId": "100", "displayId": "0", "focused": "true", "text": "old",
                           "hierarchy": "ROOT100,0,0",
                           "bounds": {"left": 20, "top": 30, "right": 100, "bottom": 50}}
        self.component = Mock()
        self.component.getAllProperties.return_value.to_dict.return_value = self.properties
        self.driver = Mock()
        self.driver.find_all_components.return_value = [self.component]
        self.mode = Mock()
        self.mode.paste.return_value.addition.return_value = self.mode
        self.context = {"point": (50, 40), "window": self.window, "hdc": Mock(), "mode_type": lambda: self.mode}

    def test_only_the_top_application_window_can_receive_input(self):
        other = {**self.window, "window_id": 101, "z_order": 101, "bundle_name": "app.other"}
        with self.assertRaises(CaptureError):
            target_window([self.window, other], self.request, (50, 40))
        self.assertEqual(target_window([self.window], self.request, (50, 40)), self.window)

    def test_focused_native_component_requires_window_display_bounds_and_unique_match(self):
        for key, value in (("hostWindowId", "101"), ("displayId", "1"), ("focused", "false"),
                           ("bounds", {"left": 0, "top": 0, "right": 10, "bottom": 10})):
            with self.subTest(key=key):
                self.component.getAllProperties.return_value.to_dict.return_value = {**self.properties, key: value}
                with self.assertRaises(CaptureError):
                    focused_component(self.driver, Mock(), self.request, (50, 40))
        self.component.getAllProperties.return_value.to_dict.return_value = self.properties
        self.driver.find_all_components.return_value = [self.component, self.component]
        with self.assertRaises(CaptureError):
            focused_component(self.driver, Mock(), self.request, (50, 40))

    def test_focus_parent_cannot_receive_input_when_its_leaf_does_not_contain_the_point(self):
        parent = Mock()
        parent.getAllProperties.return_value.to_dict.return_value = {**self.properties, "hierarchy": "ROOT100,0"}
        self.component.getAllProperties.return_value.to_dict.return_value = {
            **self.properties, "bounds": {"left": 100, "top": 0, "right": 120, "bottom": 20}}
        self.driver.find_all_components.return_value = [parent, self.component]
        with self.assertRaises(CaptureError):
            focused_component(self.driver, Mock(), self.request, (50, 40))

    def test_focus_propagated_to_ancestors_selects_only_the_actual_leaf(self):
        parent = Mock()
        parent.getAllProperties.return_value.to_dict.return_value = {**self.properties, "hierarchy": "ROOT100,0"}
        self.driver.find_all_components.return_value = [parent, self.component]
        selected, _ = focused_component(self.driver, Mock(), self.request, (50, 40))
        self.assertIs(selected, self.component)
        sibling = Mock()
        sibling.getAllProperties.return_value.to_dict.return_value = {**self.properties, "hierarchy": "ROOT100,0,1"}
        self.driver.find_all_components.return_value = [parent, self.component, sibling]
        with self.assertRaises(CaptureError):
            focused_component(self.driver, Mock(), self.request, (50, 40))

    def test_replace_append_and_clear_verify_exact_content_using_official_mode(self):
        for name, expected in (("replace", self.request["value"]),
                               ("append", "old" + self.request["value"]), ("clear", "")):
            with self.subTest(mode=name):
                request = {**self.request, "mode": name, "value": "" if name == "clear" else self.request["value"]}
                self.component.getText.return_value = expected
                with patch("har_capture.agent_device.read_windows", return_value={"windows": [self.window]}):
                    result = apply_input(self.driver, Mock(), request, self.context)
                self.assertEqual(result["text_sha256"], hashlib.sha256(expected.encode()).hexdigest())
                self.assertNotIn("text", result)
                if name == "clear":
                    self.driver.clear_text.assert_called_once_with(self.component)
                else:
                    self.mode.paste.assert_called_with(True)
                    self.mode.paste.return_value.addition.assert_called_with(name == "append")

    def test_partial_or_corrupted_readback_never_records_completion(self):
        for actual in ("https://example.test/", "", self.request["value"] + "old composition"):
            self.component.getText.return_value = actual
            with self.subTest(actual=actual), patch("har_capture.agent_device.read_windows",
                                                   return_value={"windows": [self.window]}):
                with self.assertRaises(CaptureError):
                    apply_input(self.driver, Mock(), self.request, self.context)

    def test_window_identity_or_component_center_scope_change_blocks_native_input(self):
        changed = copy.deepcopy(self.window)
        changed["process_id"] += 1
        with patch("har_capture.agent_device.read_windows", return_value={"windows": [changed]}):
            with self.assertRaises(CaptureError):
                apply_input(self.driver, Mock(), self.request, self.context)
        self.driver.input_text.assert_not_called()

    def test_request_requires_explicit_value_valid_mode_and_integer_coordinates(self):
        for key, value in (("x", 1.5), ("x", True), ("display_id", -1), ("mode", "typeOnly"), ("value", None)):
            with self.subTest(key=key, value=value), self.assertRaises(CaptureError):
                validate_input({**self.request, key: value})
        with self.assertRaises(CaptureError):
            validate_input({**self.request, "mode": "clear"})
        self.assertEqual(validate_input(self.request), (50, 40))


class NativeInputLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.request = {"x": 50, "y": 40, "window_id": 100, "display_id": 0,
                        "bundle_name": "app.browser", "mode": "replace", "value": "private-input-text"}
        self.window = {"window_id": 100, "bundle_name": "app.browser", "is_system": False,
                       "z_order": 100, "bounding_box": {"x": 0, "y": 0, "width": 200, "height": 100}}
        self.hdc = SimpleNamespace(executable=Path("fixture-tools") / "hdc.exe", device="fixture-device")
        self.driver = Mock()
        self.reports = []
        self.secret = "private-sdk-message-with-input-text"
        self.sdk = ModuleType("hypium")
        self.sdk.__path__ = []
        self.sdk.BY = Mock()
        self.sdk.UiDriver = SimpleNamespace(connect=Mock(side_effect=self.connect))
        self.model = ModuleType("hypium.model")
        self.model.InputTextMode = Mock()
        self.modules = {"hypium": self.sdk, "hypium.model": self.model}
        reports = patch("har_capture.native_input.tempfile.TemporaryDirectory", side_effect=self.report_directory)
        reports.start()
        self.addCleanup(reports.stop)
        previous_logging = logging.root.manager.disable
        self.addCleanup(logging.disable, previous_logging)
        logging.disable(logging.WARNING)

    def sdk_output(self):
        print(self.secret)
        print(self.request["value"], file=sys.stderr)
        logging.error(self.secret)

    def report_directory(self, **kwargs):
        report = TemporaryDirectory(**kwargs)
        self.reports.append(Path(report.name))
        return report

    def connect(self, **kwargs):
        self.assertEqual(kwargs["device_sn"], self.hdc.device)
        self.assertEqual(kwargs["log_level"], "error")
        report = Path(kwargs["report_path"])
        self.assertTrue(report.is_dir())
        (report / "sdk.log").write_text(self.secret, encoding="utf-8")
        self.assertEqual(logging.root.manager.disable, logging.CRITICAL)
        self.assertEqual(os.environ["PATH"], str(self.hdc.executable.parent) + os.pathsep + "fixture-path")
        self.sdk_output()
        return self.driver

    def assert_restored(self, output):
        self.assertEqual(os.environ["PATH"], "fixture-path")
        self.assertEqual(logging.root.manager.disable, logging.WARNING)
        self.assertNotIn(self.secret, output.getvalue())
        self.assertNotIn(self.request["value"], output.getvalue())
        for report in self.reports:
            self.assertFalse(report.exists())

    def test_success_closes_driver_restores_process_state_and_removes_sdk_report(self):
        result = {"verified": True, "character_count": 18}
        output = io.StringIO()
        self.driver.close.side_effect = self.sdk_output
        with patch.dict(sys.modules, self.modules), patch.dict(os.environ, {"PATH": "fixture-path"}), \
                patch("har_capture.native_input.apply_input", return_value=result) as apply, \
                patch("har_capture.native_input.logging.shutdown") as shutdown, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertIs(run_native_driver(self.hdc, self.request, self.window), result)
            self.assert_restored(output)
        apply.assert_called_once()
        self.driver.close.assert_called_once_with()
        shutdown.assert_called_once_with()

    def test_apply_or_close_failure_still_closes_driver_restores_state_and_cleans_report(self):
        for stage in ("apply", "close"):
            with self.subTest(stage=stage):
                self.driver.reset_mock(side_effect=True)
                error = RuntimeError(self.secret)
                self.driver.close.side_effect = error if stage == "close" else self.sdk_output
                output = io.StringIO()
                with patch.dict(sys.modules, self.modules), patch.dict(os.environ, {"PATH": "fixture-path"}), \
                        patch("har_capture.native_input.apply_input", side_effect=error if stage == "apply" else None), \
                        patch("har_capture.native_input.logging.shutdown") as shutdown, \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    with self.assertRaises(RuntimeError):
                        run_native_driver(self.hdc, self.request, self.window)
                    self.assert_restored(output)
                self.driver.close.assert_called_once_with()
                shutdown.assert_called_once_with()

    def test_sdk_import_connect_apply_and_close_failures_expose_only_safe_boundary_error(self):
        for stage in ("import", "connect", "apply", "close"):
            with self.subTest(stage=stage):
                self.driver.reset_mock(side_effect=True)
                error = RuntimeError(self.secret)
                self.driver.close.side_effect = error if stage == "close" else None
                self.sdk.UiDriver = SimpleNamespace(connect=Mock(side_effect=self.connect))
                self.sdk.__dict__.pop("__getattr__", None)
                if stage == "connect":
                    self.sdk.UiDriver.connect.side_effect = self.failing_connect
                if stage == "import":
                    del self.sdk.UiDriver
                    self.sdk.__getattr__ = lambda name: self.raise_sdk()
                output = io.StringIO()
                with patch.dict(sys.modules, self.modules), patch.dict(os.environ, {"PATH": "fixture-path"}), \
                        patch("har_capture.agent_device.read_windows", return_value={"windows": [self.window]}), \
                        patch("har_capture.native_input.apply_input", side_effect=error if stage == "apply" else None), \
                        patch("har_capture.native_input.logging.shutdown"), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output), \
                        self.assertLogs("har_capture.native_input", level="ERROR") as logs:
                    with self.assertRaises(CaptureError) as caught:
                        input_text(self.hdc, self.request)
                    self.assert_restored(output)
                visible_error = "".join(traceback.format_exception(caught.exception))
                self.assertNotIn(self.secret, visible_error + "".join(logs.output))
                self.assertNotIn(self.request["value"], visible_error + "".join(logs.output))
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertIn("Native text input failed; no completion was recorded", visible_error)
                self.assertIn("Native text input failed: RuntimeError", "".join(logs.output))
                self.assertEqual(self.driver.close.call_count, int(stage in ("apply", "close")))

    def failing_connect(self, **kwargs):
        self.connect(**kwargs)
        self.raise_sdk()

    def raise_sdk(self):
        self.sdk_output()
        raise RuntimeError(self.secret)
