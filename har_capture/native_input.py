"""@brief 通过官方 Hypium 输入文本，并核验原生控件的窗口归属和输入结果。"""

import contextlib
import hashlib
import io
import logging
import math
import os
import re
import tempfile

from har_capture.hdc import CaptureError, Hdc, fail, utc_now

LOGGER = logging.getLogger(__name__)


class NativeInputError(CaptureError):
    """@brief 保留不含正文的输入失败诊断。"""

    def __init__(self, message: str, diagnostics: dict):
        """@brief 绑定安全错误和本次调用证据。 @param message 安全错误。 @param diagnostics 诊断。"""
        super().__init__(message)
        self.diagnostics = diagnostics


def new_diagnostics(request: dict) -> dict:
    """@brief 仅保留合法形态的请求范围字段。 @param request 原始请求。 @return 无正文诊断。"""
    scope = {key: request.get(key) if type(request.get(key)) is int else None
             for key in ("x", "y", "window_id", "display_id")}
    bundle = request.get("bundle_name")
    scope["bundle_name"] = bundle if isinstance(bundle, str) and re.fullmatch(r"[\w.-]+", bundle) else None
    scope["mode"] = request.get("mode") if request.get("mode") in ("replace", "append", "clear") else None
    return {"request": scope, "phase": "validate_request", "trace": [], "candidates": [],
            "input": {"status": "not_started"}, "close": {"status": "not_connected"}}


def trace_call(diagnostics: dict, phase: str, operation: object, details: dict | None = None):
    """@brief 为现有调用记录阶段、时间和异常类型。 @param diagnostics 诊断。 @param phase 阶段。
    @param operation 已有调用。 @param details 非正文补充字段。 @return 调用结果。
    """
    if not any(item["status"] == "failed" for item in diagnostics["trace"]):
        diagnostics["phase"] = phase
    event = {"phase": phase, "started_at": utc_now(), "status": "running", **(details or {})}
    diagnostics["trace"].append(event)
    try:
        result = operation()
        event["status"] = "completed"
        return result
    except Exception as error:
        if not any(item["status"] == "failed" for item in diagnostics["trace"]):
            diagnostics["phase"] = phase
        event.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        event["finished_at"] = utc_now()


def text_summary(value: object) -> dict:
    """@brief 文本只输出类型、长度和摘要。 @param value 已有读回值。 @return 无正文摘要。"""
    return {"type": type(value).__name__, "length": len(value) if isinstance(value, str) else None,
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest() if isinstance(value, str) else None}


def contains(bounds: dict, point: tuple) -> bool:
    """@brief 检查原生矩形是否包含输入坐标。 @param bounds 原生矩形。
    @param point 屏幕坐标。 @return 是否包含。
    """
    if not isinstance(bounds, dict) or set(bounds) != {"left", "top", "right", "bottom"}:
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
        box = window["screen_bounds"]
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


def candidate_summary(properties: dict, request: dict, point: tuple, index: int) -> dict:
    """@brief 从原生属性中提取绑定证据，不复制正文。 @param properties 同次属性。 @param request 请求。
    @param point 输入点。 @param index 查找结果序号。 @return 筛选诊断。
    """
    result = {"index": index}
    patterns = {"hostWindowId": r"\d+", "displayId": r"\d+", "hierarchy": r"ROOT\d+(?:,\d+)*",
                "type": r"[A-Za-z_][A-Za-z0-9_.:-]*", "focused": r"true|false"}
    for key, pattern in patterns.items():
        value = properties.get(key)
        valid = isinstance(value, str) and re.fullmatch(pattern, value)
        numeric = key in ("hostWindowId", "displayId") and type(value) is int and value >= 0
        result[key] = value if valid or numeric or (key == "focused" and type(value) is bool) else None
    bounds = properties.get("bounds")
    valid_bounds = isinstance(bounds, dict) and set(bounds) == {"left", "top", "right", "bottom"}
    valid_bounds = valid_bounds and all(type(v) in (int, float) and math.isfinite(v) for v in bounds.values())
    result["bounds"] = bounds if valid_bounds else None
    result["filters"] = {"window": properties.get("hostWindowId") == str(request["window_id"]),
                         "display": properties.get("displayId") == str(request["display_id"]),
                         "focused": properties.get("focused") == "true",
                         "hierarchy": isinstance(properties.get("hierarchy"), str) and bool(re.fullmatch(
                             rf"ROOT{request['window_id']}(?:,\d+)*", properties["hierarchy"])),
                         "point": contains(bounds, point)}
    return result


def select_leaf(matches: list, diagnostics: dict) -> tuple:
    """@brief 从已有候选中选唯一焦点叶子。 @param matches 已限定候选。 @param diagnostics 诊断。
    @return 原生控件及属性。
    """
    leaves = [item for item in matches if not any(
        other[1]["hierarchy"].startswith(item[1]["hierarchy"] + ",") for other in matches)]
    for item in matches:
        diagnostics["candidates"][item[2]]["leaf"] = item in leaves
    targets = [item for item in leaves if diagnostics["candidates"][item[2]]["filters"]["point"]]
    diagnostics["filter_counts"] = {"scoped": len(matches), "leaves": len(leaves), "point": len(targets)}
    if len(targets) != 1:
        fail(f"Expected one focused native input leaf in the requested window; found {len(targets)}")
    selected = targets[0]
    diagnostics["candidates"][selected[2]]["selected"] = True
    return selected[0], selected[1]


def scoped_candidate(candidate: dict) -> bool:
    """@brief 检查候选范围及原生层级格式。 @param candidate 无正文候选证据。 @return 是否通过范围过滤。"""
    filters = candidate["filters"]
    matched = filters["window"] and filters["display"] and filters["focused"]
    if matched and not filters["hierarchy"]:
        fail("Native focused component has no verified window hierarchy")
    return matched


def focused_component(driver: object, selector: object, request: dict, point: tuple, diagnostics: dict) -> tuple:
    """@brief 选择同窗口内包含输入点的唯一焦点叶子，保留祖先焦点传播语义。 @param driver 官方驱动。
    @param selector 已限定应用的焦点选择器。 @param request 输入请求。 @param point 输入点。 @param diagnostics 诊断。
    @return 原生控件与同次读取的属性。
    """
    matches = []
    components = trace_call(diagnostics, "find_components", lambda: driver.find_all_components(selector))
    for index, component in enumerate(components):
        properties = trace_call(diagnostics, "component_properties", lambda: component.getAllProperties().to_dict(),
                                {"candidate_index": index})
        candidate = trace_call(diagnostics, "candidate_summary", lambda: candidate_summary(
            properties, request, point, index), {"candidate_index": index})
        diagnostics["candidates"].append(candidate)
        if trace_call(diagnostics, "candidate_scope", lambda: scoped_candidate(candidate), {"candidate_index": index}):
            matches.append((component, properties, index))
    return trace_call(diagnostics, "focus_leaf_selection", lambda: select_leaf(matches, diagnostics))


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


def verify_component_window(properties: dict, request: dict, context: dict) -> None:
    """@brief 复核官方输入会点击的控件中心及窗口身份。 @param properties 原生属性。
    @param request 请求。 @param context 已绑定窗口与连接。
    """
    from har_capture.agent_device import read_windows
    bounds = properties["bounds"]
    center = (int((bounds["left"] + bounds["right"]) / 2), int((bounds["top"] + bounds["bottom"]) / 2))
    current = read_windows(context["hdc"], {"display_id": request["display_id"]})["windows"]
    latest = target_window(current, request, center)
    fields = ("window_id", "process_id", "ui_node_id", "display_id", "bundle_name", "bounding_box", "transform",
              "screen_bounds")
    if any(latest[field] != context["window"][field] for field in fields):
        fail("Application window changed before native text input")


def expected_text(properties: dict, request: dict, diagnostics: dict) -> str:
    """@brief 根据既有属性生成预期值和输入前摘要。 @param properties 原生属性。
    @param request 请求。 @param diagnostics 诊断。 @return 仅在内存使用的预期文本。
    """
    before = properties.get("text")
    diagnostics["before"] = text_summary(before)
    if not isinstance(before, str):
        fail("Native input component has no readable text")
    expected = {"replace": request["value"], "append": before + request["value"], "clear": ""}[request["mode"]]
    diagnostics["comparison"] = {"expected": text_summary(expected), "actual": None, "equal": None,
                                 "actual_equals_before": None}
    return expected


def perform_input(driver: object, component: object, request: dict, mode_type: object) -> None:
    """@brief 使用官方默认输入选择，不强制粘贴，且只调用一次。 @param driver 驱动。 @param component 已绑定控件。
    @param request 请求。 @param mode_type 官方输入模式类型。
    """
    if request["mode"] == "clear":
        driver.clear_text(component)
    else:
        mode = mode_type().paste(False).addition(request["mode"] == "append")
        driver.input_text(component, request["value"], mode=mode)


def compare_text(actual: object, expected: str, before: str, diagnostics: dict) -> None:
    """@brief 比较既有读回值并仅保存摘要。 @param actual 读回值。 @param expected 预期文本。
    @param before 输入前文本。 @param diagnostics 诊断。
    """
    diagnostics["comparison"].update(actual=text_summary(actual), equal=actual == expected,
                                     actual_equals_before=actual == before,
                                     actual_is_expected_prefix=isinstance(actual, str) and bool(actual)
                                     and actual != expected and expected.startswith(actual))
    if actual != expected:
        fail("Native input text does not exactly match the requested content")


def failure_text_summary(value: object, expected: str, diagnostics: dict) -> dict:
    """@brief 失败后的文本仅保留摘要和与先前值的关系，不据此重新判定成功。
    @param value 本次读取值。 @param expected 原始预期文本。 @param diagnostics 原始诊断。
    @return 不含正文的文本比较。
    """
    summary = text_summary(value)
    readable = isinstance(value, str)
    return {"text": summary, "matches_expected": readable and value == expected,
            "matches_initial_readback": readable and summary == diagnostics["comparison"]["actual"],
            "matches_before_input": readable and summary == diagnostics["before"],
            "is_expected_prefix": readable and bool(value) and value != expected and expected.startswith(value)}


def failure_observations(component: object, request: dict, properties: dict, expected: str, diagnostics: dict) -> None:
    """@brief 写入阶段失败后各读一次属性和文本，保留原失败及同一原生句柄的后续观察。
    @param component 已选定控件。 @param request 输入请求。 @param properties 原选择属性。
    @param expected 原始预期文本。 @param diagnostics 原始诊断。
    """
    evidence = {"primary_phase": diagnostics["phase"], "properties": {"status": "not_started"},
                "text": {"status": "not_started"}}
    diagnostics["failure_observations"] = evidence
    operations = (("properties", lambda: component.getAllProperties().to_dict()), ("text", component.getText))
    for name, operation in operations:
        try:
            value = trace_call(diagnostics, "failure_" + name, operation)
            record = {"status": "recorded", "same_component_handle": True}
            if name == "properties":
                index = next(item["index"] for item in diagnostics["candidates"] if item.get("selected"))
                record["component"] = candidate_summary(value, request, (request["x"], request["y"]), index)
                fields = ("hostWindowId", "displayId", "hierarchy")
                record["binding_matches_selected"] = all(value.get(key) == properties.get(key) for key in fields)
                value = value.get("text")
            record.update(failure_text_summary(value, expected, diagnostics))
            evidence[name] = record
        except Exception as error:
            evidence[name] = {"status": "failed", "error_type": type(error).__name__}
            LOGGER.error("Native input failure observation failed: %s %s", name, type(error).__name__)


def apply_input(driver: object, selector: object, request: dict, context: dict) -> dict:
    """@brief 使用官方控件输入接口并读回完整文本。 @param driver 官方驱动。
    @param selector 焦点选择器。 @param request 请求。 @param context 当前窗口、模式类型和诊断。
    @return 不包含输入正文的验证摘要。
    """
    point, diagnostics = context["point"], context["diagnostics"]
    diagnostics["input"]["status"] = "in_progress"
    trace_call(diagnostics, "click", lambda: driver.click(point))
    component, properties = focused_component(driver, selector, request, point, diagnostics)
    trace_call(diagnostics, "center_window_check", lambda: verify_component_window(properties, request, context))
    expected = trace_call(diagnostics, "prepare_text", lambda: expected_text(properties, request, diagnostics))
    phase = "clear_text" if request["mode"] == "clear" else "input_text"
    try:
        trace_call(diagnostics, phase, lambda: perform_input(driver, component, request, context["mode_type"]))
        actual = trace_call(diagnostics, "get_text", component.getText)
        trace_call(diagnostics, "compare_text", lambda: compare_text(actual, expected, properties["text"], diagnostics))
    except Exception:
        failure_observations(component, request, properties, expected, diagnostics)
        raise
    diagnostics["input"]["status"] = "verified"
    return {"window_id": context["window"]["window_id"], "mode": request["mode"], "verified": True,
            "character_count": len(actual), "text_sha256": hashlib.sha256(actual.encode("utf-8")).hexdigest()}


def load_hypium() -> tuple:
    """@brief 在日志隔离范围内加载官方驱动。 @return 官方选择器、驱动与输入模式。"""
    from hypium import BY, UiDriver
    from hypium.model import InputTextMode
    return BY, UiDriver, InputTextMode


def close_driver(driver: object, diagnostics: dict) -> Exception | None:
    """@brief 独立记录关闭结果，不覆盖输入结果。 @param driver 驱动。 @param diagnostics 诊断。
    @return 关闭异常，或空值。
    """
    if driver is None:
        return None
    try:
        trace_call(diagnostics, "close", driver.close)
        diagnostics["close"] = {"status": "completed"}
        return None
    except Exception as error:
        diagnostics["close"] = {"status": "failed", "error_type": type(error).__name__}
        return error


def native_session(hdc: Hdc, request: dict, window: dict, diagnostics: dict, reports: str) -> dict:
    """@brief 执行一次原生输入并分别保留输入和关闭状态。 @param hdc 连接。 @param request 请求。
    @param window 已绑定窗口。 @param diagnostics 诊断。 @param reports 临时报告路径。 @return 输入收据。
    """
    driver, result, primary_error = None, None, None
    try:
        selector_type, driver_type, mode_type = trace_call(diagnostics, "import_sdk", load_hypium)
        driver = trace_call(diagnostics, "connect", lambda: driver_type.connect(
            device_sn=hdc.device, report_path=reports, log_level="error"))
        selector = trace_call(diagnostics, "selector", lambda: selector_type.focused(True).inWindow(
            window["bundle_name"]))
        context = {"point": (request["x"], request["y"]), "window": window, "hdc": hdc,
                   "mode_type": mode_type, "diagnostics": diagnostics}
        result = apply_input(driver, selector, request, context)
    except Exception as error:
        primary_error = error
        diagnostics["input"] = {"status": "failed", "error_type": type(error).__name__}
    finally:
        close_error = close_driver(driver, diagnostics)
    if primary_error is not None:
        raise primary_error
    if close_error is not None:
        raise close_error
    return result


def run_native_driver(hdc: Hdc, request: dict, window: dict, diagnostics: dict) -> dict:
    """@brief 隔离官方驱动日志，退出时关闭连接和临时文件。 @param hdc 已授权连接。
    @param request 明确的输入请求。 @param window 已绑定窗口。 @param diagnostics 诊断。 @return 原生文本验证摘要。
    """
    previous_path, previous_logging = os.environ["PATH"], logging.root.manager.disable
    os.environ["PATH"] = str(hdc.executable.parent) + os.pathsep + previous_path
    logging.disable(logging.CRITICAL)
    output, reports, result, primary_error = io.StringIO(), None, None, None
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            try:
                reports = trace_call(diagnostics, "prepare_reports", lambda: tempfile.TemporaryDirectory(
                    prefix="har-native-input-"))
                result = native_session(hdc, request, window, diagnostics, reports.name)
            except Exception as error:
                primary_error = error
            finally:
                for phase, operation in (("logging_shutdown", logging.shutdown),
                                         ("remove_reports", reports.cleanup if reports is not None else None)):
                    if operation is None:
                        continue
                    try:
                        trace_call(diagnostics, phase, operation)
                    except Exception as error:
                        if primary_error is None:
                            primary_error = error
    finally:
        os.environ["PATH"] = previous_path
        logging.disable(previous_logging)
        output.close()
    if primary_error is not None:
        raise primary_error
    return result


def input_text(hdc: Hdc, request: dict) -> dict:
    """@brief 校验窗口后执行文本输入，只输出安全的错误与验证摘要。 @param hdc 已授权连接。
    @param request 明确的输入请求。 @return 原生文本验证摘要。
    """
    from har_capture.agent_device import read_windows
    diagnostics = new_diagnostics(request)
    try:
        point = trace_call(diagnostics, "validate_request", lambda: validate_input(request))
        windows = trace_call(diagnostics, "read_windows", lambda: read_windows(
            hdc, {"display_id": request["display_id"]}))["windows"]
        window = trace_call(diagnostics, "window_scope", lambda: target_window(windows, request, point))
        return run_native_driver(hdc, request, window, diagnostics)
    except Exception as error:
        if diagnostics["input"]["status"] != "verified":
            diagnostics["input"] = {"status": "failed", "error_type": type(error).__name__}
        message = (str(error) if isinstance(error, CaptureError)
                   else "Native text input failed; no completion was recorded")
        LOGGER.error("Native text input failed at %s: %s", diagnostics["phase"], type(error).__name__)
        raise NativeInputError(message, diagnostics) from None
