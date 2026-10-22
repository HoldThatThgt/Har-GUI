"""@brief 通过官方 Hypium 粘贴文本，并核验原生控件的窗口归属和输入结果。"""

import contextlib
import hashlib
import io
import logging
import math
import os
import re
import tempfile

from har_capture.hdc import CaptureError, Hdc, fail

LOGGER = logging.getLogger(__name__)


def contains(bounds: dict, point: tuple) -> bool:
    """@brief 检查原生矩形是否包含输入坐标。 @param bounds 原生矩形。
    @param point 屏幕坐标。 @return 是否包含。
    """
    if set(bounds) != {"left", "top", "right", "bottom"}:
        return False
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in bounds.values()):
        return False
    return bounds["left"] <= point[0] < bounds["right"] and bounds["top"] <= point[1] < bounds["bottom"]


def target_window(windows: list, request: dict, point: tuple) -> dict:
    """@brief 将输入点绑定到当前最高层普通窗口。 @param windows 当前窗口。
    @param request 输入请求。 @param point 输入点。 @return 已验证窗口。
    """
    matches = []
    for window in windows:
        box = window["bounding_box"]
        bounds = {"left": box["x"], "top": box["y"],
                  "right": box["x"] + box["width"], "bottom": box["y"] + box["height"]}
        if not window["is_system"] and contains(bounds, point):
            matches.append(window)
    if not matches:
        fail("Text input point is outside application windows")
    window = max(matches, key=lambda item: item["z_order"])
    if window["window_id"] != request["window_id"] or window["bundle_name"] != request["bundle_name"]:
        fail("Text input point does not belong to the requested window")
    return window


def focused_component(driver: object, selector: object, request: dict, point: tuple) -> tuple:
    """@brief 选择同窗口内包含输入点的唯一焦点叶子，保留祖先焦点传播语义。 @param driver 官方驱动。
    @param selector 已限定应用的焦点选择器。 @param request 输入请求。 @param point 输入点。
    @return 原生控件与同次读取的属性。
    """
    matches = []
    for component in driver.find_all_components(selector):
        properties = component.getAllProperties().to_dict()
        if (properties.get("hostWindowId") == str(request["window_id"])
                and properties.get("displayId") == str(request["display_id"])
                and properties.get("focused") == "true"):
            hierarchy = properties.get("hierarchy", "")
            if not isinstance(hierarchy, str) or not re.fullmatch(rf"ROOT{request['window_id']}(?:,\d+)*", hierarchy):
                fail("Native focused component has no verified window hierarchy")
            matches.append((component, properties))
    leaves = [item for item in matches if not any(
        other[1]["hierarchy"].startswith(item[1]["hierarchy"] + ",") for other in matches)]
    targets = [item for item in leaves if contains(item[1].get("bounds", {}), point)]
    if len(targets) != 1:
        fail(f"Expected one focused native input leaf in the requested window; found {len(targets)}")
    return targets[0]


def validate_input(request: dict) -> tuple:
    """@brief 校验文本输入的明确参数。 @param request 输入请求。 @return 屏幕坐标。
    """
    for field in ("x", "y", "window_id", "display_id"):
        if type(request.get(field)) is not int or request[field] < 0:
            fail(f"Text input requires a non-negative integer {field}")
    if request["window_id"] == 0 or not isinstance(request.get("bundle_name"), str) or not request["bundle_name"]:
        fail("Text input requires an explicit application window and bundle")
    if request.get("mode") not in ("replace", "append", "clear") or not isinstance(request.get("value"), str):
        fail("Text input requires a string value and replace, append, or clear mode")
    if request["mode"] == "clear" and request["value"]:
        fail("Clear mode requires an empty value")
    return request["x"], request["y"]


def apply_input(driver: object, selector: object, request: dict, context: dict) -> dict:
    """@brief 使用官方控件粘贴接口并读回完整文本。 @param driver 官方驱动。
    @param selector 焦点选择器。 @param request 请求。 @param context 当前窗口和官方输入模式类型。
    @return 不包含输入正文的验证摘要。
    """
    from har_capture.agent_device import read_windows
    point, window = context["point"], context["window"]
    driver.click(point)
    component, properties = focused_component(driver, selector, request, point)
    bounds = properties["bounds"]
    center = ((bounds["left"] + bounds["right"]) / 2, (bounds["top"] + bounds["bottom"]) / 2)
    current = read_windows(context["hdc"], {"display_id": request["display_id"]})["windows"]
    latest = target_window(current, request, center)
    fields = ("window_id", "process_id", "ui_node_id", "display_id", "bundle_name", "bounding_box", "transform")
    if any(latest[field] != window[field] for field in fields):
        fail("Application window changed before native text input")
    before = properties.get("text")
    if not isinstance(before, str):
        fail("Native input component has no readable text")
    expected = {"replace": request["value"], "append": before + request["value"], "clear": ""}[request["mode"]]
    if request["mode"] == "clear":
        driver.clear_text(component)
    else:
        mode = context["mode_type"]().paste(True).addition(request["mode"] == "append")
        driver.input_text(component, request["value"], mode=mode)
    actual = component.getText()
    if actual != expected:
        fail("Native input text does not exactly match the requested content")
    return {"window_id": window["window_id"], "mode": request["mode"], "verified": True,
            "character_count": len(actual), "text_sha256": hashlib.sha256(actual.encode("utf-8")).hexdigest()}


def run_native_driver(hdc: Hdc, request: dict, window: dict) -> dict:
    """@brief 隔离官方驱动日志，退出时关闭连接和临时文件。 @param hdc 已授权连接。
    @param request 明确的输入请求。 @param window 已绑定窗口。 @return 原生文本验证摘要。
    """
    previous_path, previous_logging = os.environ["PATH"], logging.root.manager.disable
    os.environ["PATH"] = str(hdc.executable.parent) + os.pathsep + previous_path
    logging.disable(logging.CRITICAL)
    output = io.StringIO()
    try:
        with tempfile.TemporaryDirectory(prefix="har-native-input-") as reports:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                from hypium import BY, UiDriver
                from hypium.model import InputTextMode
                driver = None
                try:
                    driver = UiDriver.connect(device_sn=hdc.device, report_path=reports, log_level="error")
                    selector = BY.focused(True).inWindow(window["bundle_name"])
                    context = {"point": (request["x"], request["y"]), "window": window,
                               "hdc": hdc, "mode_type": InputTextMode}
                    return apply_input(driver, selector, request, context)
                finally:
                    try:
                        if driver is not None:
                            driver.close()
                    finally:
                        logging.shutdown()
    finally:
        os.environ["PATH"] = previous_path
        logging.disable(previous_logging)


def input_text(hdc: Hdc, request: dict) -> dict:
    """@brief 校验窗口后执行文本输入，只输出安全的错误与验证摘要。 @param hdc 已授权连接。
    @param request 明确的输入请求。 @return 原生文本验证摘要。
    """
    from har_capture.agent_device import read_windows
    point = validate_input(request)
    windows = read_windows(hdc, {"display_id": request["display_id"]})["windows"]
    window = target_window(windows, request, point)
    try:
        return run_native_driver(hdc, request, window)
    except CaptureError as error:
        LOGGER.error("Native text input rejected: %s", error)
        raise
    except Exception as error:
        LOGGER.error("Native text input failed: %s", type(error).__name__)
        raise CaptureError("Native text input failed; no completion was recorded") from None
