"""验证原生粘贴的范围绑定、模式与完整文本读回。"""

import copy
import contextlib
import hashlib
import io
import json
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
    NativeInputError, apply_input, candidate_summary, focused_component, input_text, new_diagnostics, run_native_driver,
    target_window, validate_input,
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
        self.diagnostics = new_diagnostics(self.request)
        self.context = {"point": (50, 40), "window": self.window, "hdc": Mock(), "mode_type": lambda: self.mode,
                        "diagnostics": self.diagnostics}

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
                    focused_component(self.driver, Mock(), self.request, (50, 40), new_diagnostics(self.request))
        self.component.getAllProperties.return_value.to_dict.return_value = self.properties
        self.driver.find_all_components.return_value = [self.component, self.component]
        with self.assertRaises(CaptureError):
            focused_component(self.driver, Mock(), self.request, (50, 40), new_diagnostics(self.request))

    def test_focus_parent_cannot_receive_input_when_its_leaf_does_not_contain_the_point(self):
        parent = Mock()
        parent.getAllProperties.return_value.to_dict.return_value = {**self.properties, "hierarchy": "ROOT100,0"}
        self.component.getAllProperties.return_value.to_dict.return_value = {
            **self.properties, "bounds": {"left": 100, "top": 0, "right": 120, "bottom": 20}}
        self.driver.find_all_components.return_value = [parent, self.component]
        with self.assertRaises(CaptureError):
            focused_component(self.driver, Mock(), self.request, (50, 40), self.diagnostics)

    def test_focus_propagated_to_ancestors_selects_only_the_actual_leaf(self):
        parent = Mock()
        parent.getAllProperties.return_value.to_dict.return_value = {**self.properties, "hierarchy": "ROOT100,0"}
        self.driver.find_all_components.return_value = [parent, self.component]
        selected, _ = focused_component(self.driver, Mock(), self.request, (50, 40), self.diagnostics)
        self.assertIs(selected, self.component)
        sibling = Mock()
        sibling.getAllProperties.return_value.to_dict.return_value = {**self.properties, "hierarchy": "ROOT100,0,1"}
        self.driver.find_all_components.return_value = [parent, self.component, sibling]
        with self.assertRaises(CaptureError):
            focused_component(self.driver, Mock(), self.request, (50, 40), new_diagnostics(self.request))

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

    def test_candidate_filters_explain_rejections_without_copying_text_or_resource_names(self):
        variants = [{"hostWindowId": "101"}, {"displayId": "2"}, {"focused": "false"},
                    {"hierarchy": "ROOT100,0", "bounds": {"left": 0, "top": 0, "right": 200, "bottom": 100}},
                    {"bounds": {"left": 150, "top": 10, "right": 180, "bottom": 30}}]
        components = []
        for variant in variants:
            component = Mock()
            component.getAllProperties.return_value.to_dict.return_value = {
                **self.properties, "text": "PRIVATE_BODY", "resourceId": "PRIVATE_RESOURCE",
                "description": "PRIVATE_DESCRIPTION", "type": "TextInput", **variant}
            components.append(component)
        self.driver.find_all_components.return_value = components
        with self.assertRaises(CaptureError):
            focused_component(self.driver, Mock(), self.request, (50, 40), self.diagnostics)
        candidates = self.diagnostics["candidates"]
        self.assertFalse(candidates[0]["filters"]["window"])
        self.assertFalse(candidates[1]["filters"]["display"])
        self.assertFalse(candidates[2]["filters"]["focused"])
        self.assertFalse(candidates[3]["leaf"])
        self.assertTrue(candidates[4]["leaf"])
        self.assertFalse(candidates[4]["filters"]["point"])
        self.assertEqual(self.diagnostics["phase"], "focus_leaf_selection")
        self.assertEqual(self.diagnostics["filter_counts"], {"scoped": 2, "leaves": 1, "point": 0})
        self.assertNotIn("PRIVATE", json.dumps(self.diagnostics))
        self.driver.find_all_components.assert_called_once()
        for component in components:
            component.getAllProperties.assert_called_once()

    def test_readback_diagnostics_are_type_aware_and_distinguish_unchanged_text(self):
        for actual in (None, self.properties["text"], "PRIVATE_WRONG_TEXT"):
            with self.subTest(actual=type(actual).__name__):
                diagnostics = new_diagnostics(self.request)
                self.component.getText.return_value = actual
                with patch("har_capture.agent_device.read_windows", return_value={"windows": [self.window]}):
                    with self.assertRaises(CaptureError):
                        apply_input(self.driver, Mock(), self.request, {**self.context, "diagnostics": diagnostics})
                self.assertEqual(diagnostics["phase"], "compare_text")
                comparison = diagnostics["comparison"]
                self.assertFalse(comparison["equal"])
                self.assertEqual(comparison["actual_equals_before"], actual == self.properties["text"])
                self.assertEqual(comparison["actual"]["type"], type(actual).__name__)
                self.assertEqual(comparison["actual"]["length"], len(actual) if isinstance(actual, str) else None)
                self.assertEqual(comparison["expected"]["length"], len(self.request["value"]))
                self.assertNotIn("PRIVATE_WRONG_TEXT", json.dumps(diagnostics))
                self.assertNotIn(self.request["value"], json.dumps(diagnostics))

    def test_each_existing_sdk_call_retains_its_own_exception_phase(self):
        cases = [(self.driver.click, "click"), (self.driver.find_all_components, "find_components"),
                 (self.component.getAllProperties, "component_properties"),
                 (self.driver.input_text, "input_text"), (self.component.getText, "get_text")]
        for method, phase in cases:
            with self.subTest(phase=phase):
                diagnostics = new_diagnostics(self.request)
                method.side_effect = RuntimeError("PRIVATE_SDK_BODY")
                with patch("har_capture.agent_device.read_windows", return_value={"windows": [self.window]}):
                    with self.assertRaises(RuntimeError):
                        apply_input(self.driver, Mock(), self.request, {**self.context, "diagnostics": diagnostics})
                method.side_effect = None
                self.assertEqual(diagnostics["phase"], phase)
                failed = next(step for step in diagnostics["trace"] if step["status"] == "failed")
                self.assertEqual(failed["phase"], phase)
                self.assertEqual(failed["error_type"], "RuntimeError")
                self.assertLessEqual(failed["started_at"], failed["finished_at"])
                self.assertNotIn("PRIVATE_SDK_BODY", json.dumps(diagnostics))

    def test_candidate_hierarchy_rejection_keeps_the_filter_phase(self):
        self.properties["hierarchy"] = "PRIVATE_INVALID_HIERARCHY"
        with self.assertRaises(CaptureError):
            focused_component(self.driver, Mock(), self.request, (50, 40), self.diagnostics)
        self.assertEqual(self.diagnostics["phase"], "candidate_scope")
        self.assertIsNone(self.diagnostics["candidates"][0]["hierarchy"])
        self.assertNotIn("PRIVATE_INVALID_HIERARCHY", json.dumps(self.diagnostics))

    def test_unexpected_candidate_types_cannot_copy_arbitrary_objects_into_diagnostics(self):
        private = {"text": "PRIVATE_BODY"}
        properties = {"hostWindowId": private, "displayId": ["PRIVATE_BODY"], "focused": private,
                      "hierarchy": private, "bounds": {"left": "PRIVATE_BODY", "right": 1, "top": 2, "bottom": 3},
                      "type": private, "text": "PRIVATE_BODY", "resourceId": "PRIVATE_BODY"}
        candidate = candidate_summary(properties, self.request, (50, 40), 0)
        self.assertNotIn("PRIVATE_BODY", json.dumps(candidate))
        self.assertTrue(all(candidate[key] is None for key in (
            "hostWindowId", "displayId", "focused", "hierarchy", "bounds", "type")))
        self.assertTrue(all(value is False for value in candidate["filters"].values()))


class NativeInputLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.public_logs = io.StringIO()
        handler = logging.StreamHandler(self.public_logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        self.addCleanup(handler.close)
        self.request = {"x": 50, "y": 40, "window_id": 100, "display_id": 0,
                        "bundle_name": "app.browser", "mode": "replace", "value": "private-input-text"}
        self.window = {"window_id": 100, "bundle_name": "app.browser", "is_system": False,
                       "z_order": 100, "bounding_box": {"x": 0, "y": 0, "width": 200, "height": 100},
                       "display_id": 0, "process_id": 42, "ui_node_id": 88, "transform": {}}
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
        self.component = Mock()
        self.component.getAllProperties.return_value.to_dict.return_value = {
            "hostWindowId": "100", "displayId": "0", "focused": "true", "hierarchy": "ROOT100,0",
            "bounds": {"left": 20, "top": 30, "right": 100, "bottom": 50}, "text": "old", "type": "TextInput"}
        self.component.getText.return_value = self.request["value"]
        self.driver.find_all_components.return_value = [self.component]
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
            self.assertIs(run_native_driver(self.hdc, self.request, self.window, new_diagnostics(self.request)), result)
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
                        run_native_driver(self.hdc, self.request, self.window, new_diagnostics(self.request))
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
                self.assertIn("RuntimeError", "".join(logs.output))
                self.assertEqual(self.driver.close.call_count, int(stage in ("apply", "close")))

    def failing_connect(self, **kwargs):
        self.connect(**kwargs)
        self.raise_sdk()

    def raise_sdk(self):
        self.sdk_output()
        raise RuntimeError(self.secret)

    def test_primary_readback_error_survives_close_and_logging_shutdown_errors(self):
        self.component.getText.side_effect = RuntimeError(self.secret)
        self.driver.close.side_effect = ValueError(self.secret)
        def failing_reports(**kwargs):
            report = self.report_directory(**kwargs)
            cleanup = report.cleanup
            def reject_cleanup():
                cleanup()
                raise OSError(self.secret)
            report.cleanup = reject_cleanup
            return report
        with patch.dict(sys.modules, self.modules), patch.dict(os.environ, {"PATH": "fixture-path"}), \
                patch("har_capture.agent_device.read_windows", return_value={"windows": [self.window]}), \
                patch("har_capture.native_input.logging.shutdown", side_effect=OSError(self.secret)), \
                patch("har_capture.native_input.tempfile.TemporaryDirectory", side_effect=failing_reports):
            with self.assertRaises(NativeInputError) as caught:
                input_text(self.hdc, self.request)
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["phase"], "get_text")
        self.assertEqual(diagnostics["input"], {"status": "failed", "error_type": "RuntimeError"})
        self.assertEqual(diagnostics["close"], {"status": "failed", "error_type": "ValueError"})
        self.assertEqual([step["phase"] for step in diagnostics["trace"] if step["status"] == "failed"],
                         ["get_text", "close", "logging_shutdown", "remove_reports"])
        self.assertNotIn(self.secret, json.dumps(diagnostics) + str(caught.exception))
        self.assertNotIn(self.secret, self.public_logs.getvalue())
        self.assertFalse(any(path.exists() for path in self.reports))
        self.driver.input_text.assert_called_once()
        self.component.getText.assert_called_once()

    def test_verified_input_and_failed_close_are_reported_as_separate_outcomes(self):
        self.driver.close.side_effect = ValueError(self.secret)
        with patch.dict(sys.modules, self.modules), patch.dict(os.environ, {"PATH": "fixture-path"}), \
                patch("har_capture.agent_device.read_windows", return_value={"windows": [self.window]}), \
                patch("har_capture.native_input.logging.shutdown"):
            with self.assertRaises(NativeInputError) as caught:
                input_text(self.hdc, self.request)
        diagnostics = caught.exception.diagnostics
        self.assertEqual(diagnostics["phase"], "close")
        self.assertEqual(diagnostics["input"], {"status": "verified"})
        self.assertEqual(diagnostics["close"], {"status": "failed", "error_type": "ValueError"})
        self.assertTrue(diagnostics["comparison"]["equal"])
        self.assertNotIn(self.request["value"], json.dumps(diagnostics))
        self.assertNotIn(self.secret, self.public_logs.getvalue())

    def test_cli_returns_native_diagnostics_in_the_existing_structured_error(self):
        import agent_device
        diagnostics = new_diagnostics(self.request)
        diagnostics["phase"] = "focus_leaf_selection"
        failure = NativeInputError("Native input binding failed", diagnostics)
        output = io.StringIO()
        with patch("agent_device.arguments", return_value=SimpleNamespace(hdc=None, device=None)), \
                patch("agent_device.execute_operation", side_effect=failure), \
                patch("sys.stdin", io.StringIO('{"operation":"input_text"}')), contextlib.redirect_stdout(output):
            self.assertEqual(agent_device.main(), 1)
        response = json.loads(output.getvalue())
        self.assertEqual(response["error"], {"message": str(failure), "diagnostics": diagnostics})
        self.assertNotIn(self.request["value"], output.getvalue())
