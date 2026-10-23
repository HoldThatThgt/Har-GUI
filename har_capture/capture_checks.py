"""@brief 校验顺序采集的一致性与屏幕窗口覆盖证据。"""

import hashlib
import io
import json

from PIL import Image, ImageChops

from har_capture.hdc import fail
from har_capture.sessions import SESSION_BLOCK, parse_sessions, pipe_values, section_value


def stable_digest(value: object) -> str:
    """@brief 对完整结构生成顺序无关的摘要。 @param value JSON 数据。 @return SHA256 摘要。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compare_screen_pixels(before: bytes, after: bytes) -> dict:
    """@brief 记录全屏真实像素差异，不遮罩或按变化拒收。 @param before 首图。
    @param after 末图。 @return 不含图片的差异事实。
    """
    with Image.open(io.BytesIO(before)) as source:
        first = source.convert("RGBA")
    with Image.open(io.BytesIO(after)) as source:
        second = source.convert("RGBA")
    if first.size != second.size:
        fail("Screen dimensions changed during collection")
    channels = ImageChops.difference(first, second).split()
    combined = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]),
                                  ImageChops.lighter(channels[2], channels[3]))
    bounds = combined.getbbox()
    return {"status": "observed", "method": "exact_rgba_pixels",
            "before_sha256": hashlib.sha256(first.tobytes()).hexdigest(),
            "after_sha256": hashlib.sha256(second.tobytes()).hexdigest(), "changed": bounds is not None,
            "width": first.width, "height": first.height,
            "changed_pixel_count": sum(combined.histogram()[1:]),
            "changed_bounds": list(bounds) if bounds else None}


def compare_capture_states(before: dict, after: dict) -> dict:
    """@brief 记录完整窗口与控件树变化，不将相等性当作页面语义。 @param before 前态。
    @param after 后态。 @return 可离线重算的原始结构差异。
    """
    checks = {name: {"before_sha256": stable_digest(before[name]),
                     "after_sha256": stable_digest(after[name]), "changed": before[name] != after[name]}
              for name in ("windows", "layout")}
    return {"atomic": False, "status": "observed", "method": "raw_wms_and_accessibility", "checks": checks}


def verify_pixel_receipt(pixels: dict, display: dict) -> None:
    """@brief 核验差异统计的格式与内部一致性，不要求零差异。 @param pixels 像素统计。
    @param display 画布尺寸。
    """
    if (pixels.get("status") != "observed" or pixels.get("method") != "exact_rgba_pixels"
            or any(pixels.get(key) != display[key] for key in ("width", "height"))):
        fail("Capture pixel observation does not match its display")
    for key in ("before_sha256", "after_sha256"):
        value = pixels.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            fail("Capture pixel observation has an invalid image digest")
    count, bounds = pixels.get("changed_pixel_count"), pixels.get("changed_bounds")
    if (type(count) is not int or not 0 <= count <= display["width"] * display["height"]
            or type(pixels.get("changed")) is not bool or pixels["changed"] != (count > 0)
            or (pixels["before_sha256"] == pixels["after_sha256"]) != (count == 0)
            or (bounds is None) != (count == 0)):
        fail("Capture pixel differences conflict with their observation")
    if bounds is not None:
        if (not isinstance(bounds, list) or len(bounds) != 4 or any(type(value) is not int for value in bounds)):
            fail("Capture pixel observation has invalid difference bounds")
        left, top, right, bottom = bounds
        if not (0 <= left < right <= display["width"] and 0 <= top < bottom <= display["height"]
                and count <= (right - left) * (bottom - top)):
            fail("Capture pixel difference bounds are outside its display")


def session_visibility(text: str) -> dict[int, dict]:
    """@brief 保留官方生命周期和渲染服务可见性证据。 @param text 会话原文。 @return 窗口字段。"""
    blocks = list(SESSION_BLOCK.finditer(text))
    result = parse_sessions(text)
    for index, block in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(text)
        lines = [line.strip() for line in text[block.end():end].splitlines()]
        life = pipe_values(section_value(lines, "Life"), 10, "Life")
        if any(life[position] not in ("0", "1") for position in (3, 6)):
            fail("Invalid session visibility or buffer availability")
        row = result[int(block["id"])]
        row.update(is_visible=life[3] == "1", buffer_available=life[6] == "1")
        visible = [line.split(":", 1)[1].strip() for line in lines if line.startswith("Visible:")]
        if len(visible) > 1:
            fail("Session contains duplicate render visibility records")
        row.update(rs_visible=None, visibility_state=None)
        if visible:
            values = pipe_values(visible[0], 2, "Visible")
            if values[0] not in ("0", "1") or values[1] not in ("0", "1", "2", "3"):
                fail("Invalid render visibility record")
            row.update(rs_visible=values[0] == "1", visibility_state=int(values[1]))
    return result


def session_exclusion(session: dict) -> str | None:
    """@brief 仅按明确会话及渲染可见性排除不在屏幕上的窗口。 @param session 原始会话事实。
    @return 排除原因或空值，部分可见窗口保留。
    """
    if session.get("is_visible") is False:
        return "session_not_visible"
    if session.get("rs_visible") is False:
        return "render_service_not_visible"
    if session.get("visibility_state") == 2:
        return "render_service_totally_occluded"
    return None


def window_exclusion(window: dict, session: dict, display: dict) -> str | None:
    """@brief 仅按明确证据排除不出现在画布上的窗口。 @param window 窗口。
    @param session 会话。 @param display 画布。 @return 排除原因或空值。
    """
    box = window["bounding_box"]
    if window["display_id"] != display["id"]:
        return "different_display"
    if window["z_order"] < 0:
        return "not_foreground"
    if (box["width"] <= 0 or box["height"] <= 0 or box["x"] >= display["width"]
            or box["y"] >= display["height"] or box["x"] + box["width"] <= 0
            or box["y"] + box["height"] <= 0):
        return "outside_canvas"
    return session_exclusion(session)


def coverage_report(metadata: dict, inventory: dict, session_text: str) -> dict:
    """@brief 核清全部 WMS 窗口双树覆盖，不推测系统层透明性。 @param metadata 已采集窗口。
    @param inventory 全部 WMS 窗口。 @param session_text 会话证据。 @return 覆盖审计结果。
    """
    sessions = session_visibility(session_text)
    captured = {window["window_id"]: window for window in metadata["windows"]}
    covered, excluded, unresolved = [], [], []
    for window_id, window in inventory.items():
        session = sessions.get(window_id, {})
        reason = window_exclusion(window, session, metadata["display"])
        if reason:
            excluded.append({"window_id": window_id, "reason": reason, "session": session})
            continue
        trees = captured.get(window_id, {}).get("trees", {})
        if all(trees.get(kind, {}).get("node_count", 0) > 0 for kind in ("accessibility", "arkui")):
            covered.append(window_id)
        else:
            unresolved.append({"window_id": window_id, "reason": "missing_visible_window_tree", "session": session})
    return {"status": "verified" if not unresolved else "failed", "covered_window_ids": sorted(covered),
            "excluded_windows": excluded, "unresolved_windows": unresolved,
            "policy": "Require both trees unless WMS proves the window is not exposed; do not infer transparency."}
