"""以已安装官方 SDK 核对单次输入协议；在 RPC 入口截断，不连接设备。"""

import contextlib
import io
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from har_capture.native_input import perform_input


class NativeInputSdkTests(unittest.TestCase):
    def test_official_mode_keeps_full_text_and_explicit_append_without_forcing_paste(self):
        output = io.StringIO()
        previous = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                from hypium import UiDriver
                from hypium.model import InputTextMode
                from hypium.uidriver.uicomponent import UiComponent
                component = UiComponent("Component#offline-input-contract")
                driver = SimpleNamespace(input_text=Mock(side_effect=lambda target, value, mode:
                                                        target.inputText(value, mode)), clear_text=Mock())
                with patch.object(UiDriver, "connect", side_effect=AssertionError("Unexpected device connection")) as connect:
                    with patch("hypium.uidriver.uicomponent.do_hypium_rpc", return_value=None) as rpc:
                        for mode in ("replace", "append"):
                            for value in ("https://example.test/" + "x" * 51, "中文输入😀", "a" * 200, "a" * 201):
                                with self.subTest(mode=mode, length=len(value)):
                                    rpc.reset_mock()
                                    driver.input_text.reset_mock()
                                    perform_input(driver, component, {"mode": mode, "value": value}, InputTextMode)
                                    driver.input_text.assert_called_once()
                                    rpc.assert_called_once()
                                    config, api, target, text, options = rpc.call_args.args
                                    self.assertEqual(config.since, 20)
                                    self.assertEqual(api, "Component.inputText")
                                    self.assertIs(target, component)
                                    self.assertEqual(text, value)
                                    self.assertEqual(options.to_dict(), {"paste": False, "addition": mode == "append"})
                        rpc.reset_mock()
                        perform_input(driver, component, {"mode": "clear", "value": ""}, InputTextMode)
                        driver.clear_text.assert_called_once_with(component)
                        rpc.assert_not_called()
                    connect.assert_not_called()
        finally:
            logging.disable(previous)


if __name__ == "__main__":
    unittest.main()
