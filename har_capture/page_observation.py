"""@brief 保存目标窗口观测来源并核验独立页面语义收据，不持久化末帧图片。"""

import base64
import hashlib
import io
import json
import math
from pathlib import Path

from PIL import Image

from har_capture.hdc import Hdc, fail
from har_capture.metadata import file_info
from har_capture.privacy import verify_privacy_receipt

TARGET_KEYS = ("window_id", "bundle_name", "process_id", "display_id")
FRAME_KEYS = {"screenshot_sha256", "crop_sha256", "screen_bounds", "crop_bounds", "windows_sha256", "layout_sha256"}
DECISIONS = {"same_page", "page_changed", "body_blank", "unobservable"}


def validate_target(target: object) -> dict | None:
    """@brief 验证明确选择的目标，不猜测前台窗口。 @param target 窗口和应用身份。 @return 已校验目标。"""
    if target is None:
        return None
    if (not isinstance(target, dict) or set(target) != {"window_id", "bundle_name"}
            or type(target["window_id"]) is not int or target["window_id"] <= 0
            or not isinstance(target["bundle_name"], str) or not target["bundle_name"].strip()):
        fail("Page observation requires an explicit target window and bundle")
    return dict(target)


def target_window(metadata: dict, target: dict) -> dict:
    """@brief 唯一绑定已采集的普通应用窗口。 @param metadata 原始采集信息。
    @param target 明确选择。 @return 目标窗口。
    """
    matches = [window for window in metadata["windows"] if all(window.get(key) == value
               for key, value in target.items()) and window.get("is_system") is False]
    if len(matches) != 1:
        fail("Page observation target is not a unique captured application window")
    return matches[0]


def closing_target(hdc: Hdc, metadata: dict, target: dict, inventory: dict) -> dict:
    """@brief 独立重读目标精确几何，仅要求实际坐标来源的身份完整。 @param hdc 设备连接。
    @param metadata 原始窗口集合。 @param target 目标窗口。 @param inventory 末尾 WMS。 @return 末尾目标。
    """
    from har_capture.window_geometry import read_window_geometry
    windows = []
    for window in metadata["windows"]:
        if not window["is_system"] and window["window_id"] != target["window_id"]:
            continue
        current = inventory.get(window["window_id"])
        if current is None or any(current[key] != window[key] for key in ("process_id", "display_id")):
            if window["window_id"] == target["window_id"]:
                fail("Page observation target window identity changed during collection")
            continue
        windows.append({**window, **current})
    observed = [window for window in read_window_geometry(hdc, windows)
                if window["window_id"] == target["window_id"]]
    if len(observed) != 1 or observed[0]["screen_bounds"] != target["screen_bounds"]:
        fail("Page observation target geometry changed during collection")
    return observed[0]


def crop_bounds(bounds: dict, canvas: dict) -> dict:
    """@brief 按向外取整的真实外框裁画布，不改变记录中的浮点几何。 @param bounds 精确外框。
    @param canvas 整屏尺寸。 @return 实际裁图整数矩形。
    """
    if (not isinstance(bounds, dict) or set(bounds) != {"x", "y", "width", "height"}
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in bounds.values())
            or min(bounds["width"], bounds["height"]) <= 0):
        fail("Page observation has invalid exact target bounds")
    left, top = max(0, math.floor(bounds["x"])), max(0, math.floor(bounds["y"]))
    right = min(canvas["width"], math.ceil(bounds["x"] + bounds["width"]))
    bottom = min(canvas["height"], math.ceil(bounds["y"] + bounds["height"]))
    if left >= right or top >= bottom:
        fail("Page observation target has no visible screen intersection")
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def crop_image(raw: bytes, bounds: dict) -> bytes:
    """@brief 仅在内存中生成目标截图，不保存额外图片。 @param raw 原始 PNG。
    @param bounds 整数裁图矩形。 @return 编码后的 PNG 字节。
    """
    with Image.open(io.BytesIO(raw)) as image:
        cropped = image.crop((bounds["x"], bounds["y"], bounds["x"] + bounds["width"],
                              bounds["y"] + bounds["height"]))
        output = io.BytesIO()
        cropped.save(output, format="PNG")
    return output.getvalue()


def image_observation(raw: bytes, window: dict, sources: dict, canvas: dict) -> tuple[dict, str]:
    """@brief 绑定完整图片、目标裁图和原始树来源。 @param raw 完整 PNG。
    @param window 目标精确窗口。 @param sources WMS 与树文件记录。 @param canvas 画布。 @return 收据和临时图片。
    """
    bounds = crop_bounds(window["screen_bounds"], canvas)
    cropped = crop_image(raw, bounds)
    receipt = {"screenshot_sha256": hashlib.sha256(raw).hexdigest(),
               "crop_sha256": hashlib.sha256(cropped).hexdigest(), "screen_bounds": window["screen_bounds"],
               "crop_bounds": bounds, "windows_sha256": sources["windows"]["sha256"],
               "layout_sha256": sources["layout"]["sha256"]}
    return receipt, "data:image/png;base64," + base64.b64encode(cropped).decode("ascii")


def observe_page(stage: Path, images: tuple[bytes, bytes], windows: tuple[dict, dict], canvas: dict) -> dict:
    """@brief 构造目标首尾观测及仅供当前请求使用的裁图。 @param stage 原始证据目录。
    @param images 首尾原始 PNG。 @param windows 首尾目标窗口。 @param canvas 画布。 @return 临时图片和无图收据。
    """
    frames, encoded = [], []
    for raw, window, prefix in zip(images, windows, ("", "closing-"), strict=True):
        sources = {kind: file_info(stage / (prefix + name)) for kind, name in (
            ("windows", "windows.txt"), ("layout", "layout.json"))}
        frame, image = image_observation(raw, window, sources, canvas)
        frames.append(frame)
        encoded.append(image)
    receipt = {"rule_version": 1, "target": {key: windows[0][key] for key in TARGET_KEYS},
               "before": frames[0], "after": frames[1]}
    return {"receipt": receipt, "before_image": encoded[0], "after_image": encoded[1]}


def read_receipt(path: Path) -> dict:
    """@brief 读取普通收据对象，拒绝缺失和符号链接。 @param path 收据文件。 @return 原对象。"""
    if path.is_symlink() or not path.is_file():
        fail(f"Page observation receipt is missing or is a symbolic link: {path.name}")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        fail(f"Cannot read page observation receipt {path.name}: {type(error).__name__}")
    if not isinstance(value, dict):
        fail(f"Page observation receipt is not an object: {path.name}")
    return value


def verify_frame(frame: dict, sources: dict, canvas: dict) -> None:
    """@brief 核对无图观测的摘要格式、来源和确定性裁图边界。 @param frame 观测帧。
    @param sources 原始索引。 @param canvas 画布尺寸。
    """
    if not isinstance(frame, dict) or set(frame) != FRAME_KEYS:
        fail("Page observation frame fields are invalid")
    crop = frame["crop_bounds"]
    if (not isinstance(crop, dict) or set(crop) != {"x", "y", "width", "height"}
            or any(type(value) is not int for value in crop.values())):
        fail("Page observation crop bounds must be an integer rectangle")
    for key in ("screenshot_sha256", "crop_sha256", "windows_sha256", "layout_sha256"):
        value = frame[key]
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            fail("Page observation frame has an invalid source digest")
    if any(frame[kind + "_sha256"] != sources[kind]["sha256"] for kind in ("windows", "layout")):
        fail("Page observation differs from its raw WMS or layout source")
    if frame["crop_bounds"] != crop_bounds(frame["screen_bounds"], canvas):
        fail("Page observation crop bounds differ from the exact target envelope")


def original_screenshot_digest(directory: Path, metadata: dict) -> str:
    """@brief 只从实际索引图片和匹配的隐私收据取得原图摘要。 @param directory 证据目录。
    @param metadata 本次元数据。 @return 已验证的原始 PNG 摘要。
    """
    record = metadata["files"]["screenshot"]
    path = directory / record["file"]
    if path.is_symlink() or not path.is_file():
        fail("Page observation indexed screenshot is missing or is a symbolic link")
    actual = file_info(path)
    if any(actual[key] != record[key] for key in ("bytes", "sha256")):
        fail("Page observation indexed screenshot digest does not match its actual bytes")
    if "privacy" not in metadata:
        return actual["sha256"]
    receipt = read_receipt(directory / "privacy.json")
    verify_privacy_receipt(receipt)
    if receipt != metadata["privacy"] or receipt["after_sha256"] != actual["sha256"]:
        fail("Page observation original screenshot is not proven by its saved privacy receipt")
    return receipt["before_sha256"]


def observed_sources(directory: Path, metadata: dict, prefix: str) -> dict:
    """@brief 从实际原始文件核对每帧树来源，而非只信元数据摘要。 @param directory 证据目录。
    @param metadata 原始文件索引。 @param prefix 末尾观测前缀。 @return 已核验来源。
    """
    sources = {}
    for kind, suffix in (("windows", "windows.txt"), ("layout", "layout.json")):
        record = metadata["files"][prefix + kind]
        filename = ("closing-" if prefix else "") + suffix
        path = directory / filename
        if (record.get("file") != filename or path.is_symlink() or not path.is_file()
                or record != file_info(path)):
            fail("Page observation raw WMS or layout source differs from its captured index")
        sources[kind] = record
    return sources


def verify_page_observation(directory: Path, metadata: dict) -> dict:
    """@brief 核验新采集的明确目标及原始观测摘要，不重新推断页面。 @param directory 证据目录。
    @param metadata 原采集信息。 @return 已核验观测。
    """
    record = metadata["files"].get("page_observation", {})
    if record.get("file") != "page-observation.json":
        fail("New delivery requires its indexed target page observation")
    observation = read_receipt(directory / record["file"])
    if record != file_info(directory / "page-observation.json"):
        fail("Target page observation differs from its captured file index")
    if (set(observation) != {"rule_version", "target", "before", "after"}
            or type(observation["rule_version"]) is not int or observation["rule_version"] != 1):
        fail("Page observation fields or rule version are invalid")
    target = observation["target"]
    if not isinstance(target, dict) or set(target) != set(TARGET_KEYS):
        fail("Page observation target identity is invalid")
    validate_target({key: target[key] for key in ("window_id", "bundle_name")})
    if (type(target["process_id"]) is not int or target["process_id"] <= 0
            or type(target["display_id"]) is not int or target["display_id"] < 0):
        fail("Page observation process or display identity is invalid")
    window = target_window(metadata, {key: target[key] for key in ("window_id", "bundle_name")})
    if target != {key: window[key] for key in TARGET_KEYS}:
        fail("Page observation target differs from its captured window identity")
    for name, prefix in (("before", ""), ("after", "closing_")):
        sources = observed_sources(directory, metadata, prefix)
        verify_frame(observation[name], sources, metadata["display"])
        if observation[name]["screen_bounds"] != window["screen_bounds"]:
            fail("Page observation target geometry differs from the verified capture")
    expected = original_screenshot_digest(directory, metadata)
    if observation["before"]["screenshot_sha256"] != expected:
        fail("Page observation does not identify the original saved screenshot")
    return observation


def verify_page_stability(directory: Path, metadata: dict) -> dict:
    """@brief 验证独立语义收据，明确拒收切页和主体空白，保留不可观测结论。 @param directory 证据目录。
    @param metadata 已核验原始采集信息。 @return 已绑定的语义收据。
    """
    observation = verify_page_observation(directory, metadata)
    receipt = read_receipt(directory / "page-stability.json")
    target = receipt.get("target")
    if (not isinstance(target, dict) or set(target) != set(TARGET_KEYS)
            or any(type(target[key]) is not type(observation["target"][key]) for key in TARGET_KEYS)):
        fail("Page stability receipt target identity has invalid scalar types")
    required = {"rule_version", "capture_id", "target", "observation_sha256", "decision", "evidence"}
    if (set(receipt) != required or type(receipt["rule_version"]) is not int or receipt["rule_version"] != 1
            or receipt["capture_id"] != metadata["capture_id"] or receipt["target"] != observation["target"]
            or receipt["observation_sha256"] != metadata["files"]["page_observation"]["sha256"]
            or not isinstance(receipt["decision"], str) or receipt["decision"] not in DECISIONS
            or not isinstance(receipt["evidence"], str)
            or not receipt["evidence"].strip()):
        fail("Page stability receipt does not match its target observation")
    if receipt["decision"] in {"page_changed", "body_blank"}:
        fail(f"Target page stability rejected the capture: {receipt['decision']}")
    return receipt
