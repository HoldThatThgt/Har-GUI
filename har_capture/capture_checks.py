"""@brief 校验顺序采集的一致性与屏幕窗口覆盖证据。"""

import hashlib
import io
import json

from PIL import Image, ImageChops

from har_capture.hdc import fail
from har_capture.sessions import SESSION_BLOCK, parse_sessions, pipe_values, section_value
from har_capture.stability import comparison_layout


def stable_digest(value: object) -> str:
    """@brief 对完整结构生成顺序无关的摘要。 @param value JSON 数据。 @return SHA256 摘要。"""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def compare_screen_pixels(before: bytes, after: bytes, regions: tuple | list = ()) -> dict:
    """@brief 比较内存中的首尾画面，禁止以容差吞掉真实内容变化。 @param before 首图。
    @param after 末图。 @param regions 已核实的系统叶子。 @return 像素摘要和差异证据，不返回图片。
    """
    with Image.open(io.BytesIO(before)) as source:
        first = source.convert("RGBA")
    with Image.open(io.BytesIO(after)) as source:
        second = source.convert("RGBA")
    if first.size != second.size:
        fail("Screen dimensions changed during collection")
    difference = ImageChops.difference(first, second)
    channels = difference.split()
    combined = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]),
                                  ImageChops.lighter(channels[2], channels[3]))
    raw_count, raw_bounds = sum(combined.histogram()[1:]), combined.getbbox()
    raw_hashes = {"before_sha256": hashlib.sha256(first.tobytes()).hexdigest(),
                  "after_sha256": hashlib.sha256(second.tobytes()).hexdigest()}
    for region in regions:
        bounds = tuple(region["bounds"])
        combined.paste(0, bounds)
        first.paste((0, 0, 0, 0), bounds)
        second.paste((0, 0, 0, 0), bounds)
    bounds = combined.getbbox()
    changed = bounds is not None
    evidence = {"status": "failed" if changed else "verified", "method": "exact_rgba_pixels",
                **raw_hashes, "compared_before_sha256": hashlib.sha256(first.tobytes()).hexdigest(),
                "compared_after_sha256": hashlib.sha256(second.tobytes()).hexdigest(), "changed": changed,
                "raw_changed_pixel_count": raw_count, "raw_changed_bounds": list(raw_bounds) if raw_bounds else None,
                "declared_regions": [region["bounds"] for region in regions],
                "changed_pixel_count": sum(combined.histogram()[1:]),
                "changed_bounds": list(bounds) if bounds else None}
    return evidence


def compare_capture_states(before: dict, after: dict, stability: dict | None = None) -> dict:
    """@brief 验证采集前后窗口与可见页面一致。 @param before 前态。 @param after 后态。
    @param stability 已验证的显式系统叶子范围。 @return 完整比较证据。
    """
    compared_before, compared_after = dict(before), dict(after)
    if stability is not None:
        compared_before["layout"] = comparison_layout(before["layout"], stability)
        compared_after["layout"] = comparison_layout(after["layout"], stability)
    checks = {name: {"before_sha256": stable_digest(canonical_state(compared_before[name])),
                     "after_sha256": stable_digest(canonical_state(compared_after[name])),
                     "raw_before_sha256": stable_digest(before[name]), "raw_after_sha256": stable_digest(after[name])}
              for name in ("windows", "layout")}
    changed = [name for name, value in checks.items() if value["before_sha256"] != value["after_sha256"]]
    if changed:
        fail(f"Capture state changed during collection: {', '.join(changed)}")
    return {"atomic": False, "stable": True, "status": "verified",
            "method": "bracketed_wms_and_accessibility", "checks": checks,
            "canonicalization": {"rule_version": "uitest-semantic-state-v2",
                "rules": ["Only attributes.blur empty and 0.00px represent the same default zero radius.",
                          "Within each verified Web or XComponent host, rename positive treeIds by first occurrence.",
                          "Preserve host identity, low 40-bit elementId, and namespace partitions; retain all text."],
                "source": "https://github.com/openharmony/arkui_ace_engine/blob/"
                          "21b150e7bb1d5308846f8e5be8ef0482fb433315/"
                          "adapter/ohos/osal/js_accessibility_manager.cpp",
                "identity_source": "https://github.com/openharmony/accessibility/blob/"
                    "3ffbe3513f07cb1df2bd98d74089582dff5eb33a/"
                    "interfaces/innerkits/asacfwk/include/accessibility_system_ability_client.h#L207-L243",
                "provider_source": "https://github.com/openharmony/arkui_ace_engine/blob/"
                    "21b150e7bb1d5308846f8e5be8ef0482fb433315/"
                    "adapter/ohos/osal/js_third_provider_interaction_operation.cpp#L309-L348"}}


def verify_pixel_receipt(pixels: dict, stability: dict) -> None:
    """@brief 核验内存比较收据与重新推导的声明边界一致。 @param pixels 比较收据。
    @param stability 从原始树及会话重新验证的系统叶子。
    """
    bounds = [region["bounds"] for region in stability["regions"]]
    if (pixels.get("status") != "verified" or pixels.get("method") != "exact_rgba_pixels"
            or pixels.get("changed") is not False or pixels.get("changed_pixel_count") != 0
            or pixels.get("changed_bounds") is not None or pixels.get("declared_regions") != bounds
            or pixels.get("compared_before_sha256") != pixels.get("compared_after_sha256")):
        fail("Capture pixel consistency has not passed exact comparison outside declared system leaves")
    for key in ("before_sha256", "after_sha256", "compared_before_sha256", "compared_after_sha256"):
        value = pixels.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            fail("Capture pixel consistency has an invalid image digest")
    count = pixels.get("raw_changed_pixel_count")
    area = sum((right - left) * (bottom - top) for left, top, right, bottom in bounds)
    raw_equal = pixels["before_sha256"] == pixels["after_sha256"]
    if (type(count) is not int or not 0 <= count <= area or raw_equal != (count == 0)
            or (pixels.get("raw_changed_bounds") is None) != (count == 0)):
        fail("Capture raw pixel differences conflict with the declared comparison evidence")
    if not bounds and (pixels["before_sha256"] != pixels["compared_before_sha256"]
                       or pixels["after_sha256"] != pixels["compared_after_sha256"]):
        fail("Capture without stability regions must compare every raw pixel")


def native_identity(attributes: dict) -> tuple | None:
    """@brief 验证 UITest 的窗口与原生拼接身份格式。 @param attributes 原始节点属性。
    @return 窗口、注册树和低位元素标识；格式不成立时不应用语义规范化。
    """
    identity, host = attributes.get("accessibilityId"), attributes.get("hostWindowId")
    if not all(isinstance(value, str) and value.isascii() and value.isdecimal() for value in (identity, host)):
        return None
    if not 0 <= int(identity) < (1 << 53) or int(host) <= 0 or attributes.get("hashcode") != f"{host}:{identity}":
        return None
    return host, int(identity) >> 40, int(identity) & ((1 << 40) - 1)


def canonical_state(value: object, scope: tuple | None = None) -> object:
    """@brief 按宿主规范注册树身份，保留树分区、元素身份及全部页面属性。 @param value 原始状态。
    @param scope 当前宿主窗口、宿主树标识及子树顺序映射。 @return 不改写原始内容、几何或顺序的比较副本。
    """
    if isinstance(value, list):
        return [canonical_state(item, scope) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: canonical_state(item) for key, item in value.items() if key != "children"}
    attributes = result.get("attributes")
    if isinstance(attributes, dict):
        if attributes.get("blur") in ("", "0.00px"):
            attributes["blur"] = "0.00px"
        identity = native_identity(attributes)
        for key in ("accessibilityId", "hashcode"):
            if key in attributes:
                attributes[key] = {"raw": attributes[key]}
        boundary = attributes.get("type") in ("Web", "XComponent")
        if scope is not None and attributes.get("hostWindowId") != scope[0]:
            scope = None
        if (not boundary and scope is not None and identity is not None
                and identity[0] == scope[0] and identity[1] > 0 and identity[1] != scope[1]):
            host, tree_id, element_id = identity
            ordinal = scope[2].setdefault(tree_id, len(scope[2]) + 1)
            attributes["accessibilityId"] = {"tree_id": ordinal, "element_id": element_id}
            attributes["hashcode"] = {"window_id": host, "tree_id": ordinal, "element_id": element_id}
        if boundary:
            scope = (identity[0], identity[1], {}) if identity is not None else None
    if "children" in value:
        result["children"] = canonical_state(value["children"], scope)
    return result


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
