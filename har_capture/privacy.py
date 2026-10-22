"""@brief 在采集完成后独立处理截图，原始控件树留在本地证据中。"""

import hashlib
import io
import json
import os
from pathlib import Path

from PIL import Image, ImageDraw

from har_capture.hdc import fail, utc_now
from har_capture.metadata import file_info, layout_bounding_box


def atomic_json(path: Path, value: dict) -> None:
    """@brief 原子替换本地 JSON。 @param path 目标文件。 @param value 数据。"""
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("wb") as stream:
        stream.write((json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def node_inventory(directory: Path, metadata: dict) -> list[dict]:
    """@brief 提取不包含正文的定位清单。 @param directory 证据目录。 @param metadata 采集信息。
    @return 包名、资源标识、类型和实际屏幕矩形。
    """
    record = metadata["files"]["accessibility"]
    data = (directory / record["file"]).read_bytes()
    if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
        fail("Accessibility evidence digest mismatch before resolving redaction selectors")
    trees = json.loads(data)
    result = []
    for root in trees:
        bundle = root["attributes"]["bundleName"]
        window_id = int(root["attributes"]["hostWindowId"])
        pending = [root]
        while pending:
            node = pending.pop()
            attributes = node["attributes"]
            resource_id = attributes.get("id")
            if resource_id:
                result.append({"bundle_name": bundle, "window_id": window_id, "resource_id": resource_id,
                               "type": attributes.get("type"),
                               "bounding_box": layout_bounding_box(attributes["bounds"])})
            pending.extend(node["children"])
    return result


def resolve_regions(directory: Path, metadata: dict, policy: dict) -> list[dict]:
    """@brief 在本次树中精确定位项目声明的正文容器。 @param directory 证据目录。
    @param metadata 采集信息。 @param policy 项目规则。 @return 需要覆盖的屏幕区域。
    """
    inventory = node_inventory(directory, metadata)
    resolved = []
    for rule in policy["regions"]:
        matches = [node for node in inventory if all(node[key] == value for key, value in rule.items())]
        if not matches:
            fail("A configured redaction selector did not match this capture")
        for node in matches:
            box = node["bounding_box"]
            width, height = metadata["display"]["width"], metadata["display"]["height"]
            left, top = max(0, box["x"]), max(0, box["y"])
            right, bottom = min(width, box["x"] + box["width"]), min(height, box["y"] + box["height"])
            if right <= left or bottom <= top:
                fail("A configured redaction region has no visible pixels")
            resolved.append({**node, "bounding_box": {"x": left, "y": top,
                                                       "width": right - left, "height": bottom - top}})
    return resolved


def masked_image(path: Path, regions: list[dict]) -> bytes:
    """@brief 用不透明黑色覆盖矩形且清除 PNG 文本元数据。 @param path 原图。 @param regions 区域。
    @return 只在内存中保存的最终 PNG 字节。
    """
    with Image.open(path) as source:
        image = source.convert("RGB")
    image.info.clear()
    drawing = ImageDraw.Draw(image)
    for region in regions:
        box = region["bounding_box"]
        drawing.rectangle((box["x"], box["y"], box["x"] + box["width"] - 1,
                           box["y"] + box["height"] - 1), fill=(0, 0, 0))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def validate_policy(policy: dict) -> bool:
    """@brief 验证显式开关及配置结构，不读取控件树或匹配选择器。 @param policy 项目隐私配置。
    @return 是否启用打码；启用但未提供选择器时拒绝后处理。
    """
    if not isinstance(policy, dict) or set(policy) - {"enabled", "reason", "regions"}:
        fail("Privacy policy accepts enabled, reason, and optional regions; mode is not supported")
    enabled = policy.get("enabled")
    if type(enabled) is not bool or not isinstance(policy.get("reason"), str) or not policy["reason"].strip():
        fail("Privacy policy requires an explicit boolean enabled and a nonempty reason")
    if "regions" in policy:
        rules = policy["regions"]
        if not isinstance(rules, list) or not rules:
            fail("Configured privacy regions must be a nonempty selector array")
        for rule in rules:
            if not isinstance(rule, dict) or set(rule) != {"bundle_name", "resource_id"}:
                fail("Privacy selectors require only bundle_name and resource_id")
            if any(not isinstance(value, str) or not value.strip() for value in rule.values()):
                fail("Privacy selector fields must be nonempty strings")
    elif enabled:
        fail("Capture awaits independent redaction; configure verified selectors before preparing delivery")
    return enabled


def verify_privacy_receipt(receipt: dict) -> None:
    """@brief 核验显式开关、处理状态和图片摘要之间的约束。 @param receipt 打码处理收据。"""
    enabled = receipt.get("enabled")
    if type(enabled) is not bool or receipt.get("status") != ("verified" if enabled else "disabled"):
        fail("Privacy receipt enabled state does not match its processing status")
    regions = receipt.get("regions")
    if not isinstance(regions, list) or bool(regions) != enabled:
        fail("Privacy receipt regions do not match its enabled state")
    for field in ("before_sha256", "after_sha256"):
        value = receipt.get(field)
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            fail("Privacy receipt has an invalid screenshot digest")
    if not enabled and receipt.get("before_sha256") != receipt.get("after_sha256"):
        fail("Disabled privacy must preserve the original screenshot digest")


def finish_redaction(directory: Path, metadata: dict, receipt: dict) -> dict:
    """@brief 核对最终图片后更新摘要，删除含旧图的派生 HTML。 @param directory 证据目录。
    @param metadata 采集信息。 @param receipt 已持久化的打码收据。 @return 最终采集信息。
    """
    screenshot = directory / metadata["files"]["screenshot"]["file"]
    verify_privacy_receipt(receipt)
    actual = file_info(screenshot)
    if actual["sha256"] != receipt["after_sha256"]:
        fail("Redaction receipt does not match the final screenshot")
    metadata["files"]["screenshot"].update(bytes=actual["bytes"], sha256=actual["sha256"])
    metadata["privacy"] = receipt
    (directory / "viewer.html").unlink(missing_ok=True)
    atomic_json(directory / "metadata.json", metadata)
    return metadata


def redact_capture(directory: Path, policy: dict) -> dict:
    """@brief 独立执行或恢复截图后处理，绝不产生第二张正式图片。 @param directory 原始采集目录。
    @param policy 项目中的明确打码规则。 @return 更新后的采集信息。
    """
    metadata = json.loads((directory / "metadata.json").read_bytes())
    if metadata.get("delivery"):
        fail("Cannot modify a prepared or published delivery")
    enabled = validate_policy(policy)
    policy_hash = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
    screenshot = directory / metadata["files"]["screenshot"]["file"]
    before = file_info(screenshot)["sha256"]
    receipt_path = directory / "privacy.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_bytes())
        verify_privacy_receipt(receipt)
        if receipt["policy_sha256"] != policy_hash or receipt["enabled"] != enabled:
            fail("Redaction policy differs from the existing receipt")
        if before == receipt["after_sha256"]:
            return finish_redaction(directory, metadata, receipt)
        if before != receipt["before_sha256"]:
            fail("Screenshot changed outside the recorded redaction operation")
    elif before != metadata["files"]["screenshot"]["sha256"]:
        fail("Screenshot digest does not match capture metadata")
    regions = resolve_regions(directory, metadata, policy) if enabled else []
    data = masked_image(screenshot, regions) if enabled else screenshot.read_bytes()
    receipt = {"enabled": enabled, "status": "verified" if enabled else "disabled",
               "rule_version": "resource-regions-v1",
               "processed_at": utc_now(), "policy_sha256": policy_hash, "reason": policy["reason"],
               "regions": regions, "before_sha256": before, "after_sha256": hashlib.sha256(data).hexdigest()}
    atomic_json(receipt_path, receipt)
    if not enabled:
        return finish_redaction(directory, metadata, receipt)
    temporary = screenshot.with_suffix(".redacting")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(screenshot)
    return finish_redaction(directory, metadata, receipt)


def viewer_tree(node: dict) -> dict:
    """@brief 仅向查看页复制结构属性，正文和原始属性仍留在本地 JSON。 @param node 原始节点。
    @return 去正文的同层级节点。
    """
    allowed = {"type", "id", "bounds", "bundleName", "hostWindowId", "displayId", "visible",
               "enabled", "clickable", "scrollable", "checked", "selected", "focused", "ID",
               "AccessibilityId", "FrameRect", "coordinate_status", "compid"}
    result = {"attributes": {key: value for key, value in node["attributes"].items() if key in allowed},
              "children": [viewer_tree(child) for child in node["children"]],
              "raw": "[Content omitted from viewer]", "source_attributes": {}}
    for key in ("source_line", "declared_child_count", "screen_bounds", "visible_bounds", "coordinate_validation"):
        if key in node:
            result[key] = node[key]
    if "geometry" in node:
        result["geometry"] = {key: value for key, value in node["geometry"].items()
                              if key in ("status", "screen_bounds", "rectangular_clip_bounds", "reason",
                                         "rendered", "render_state", "render_state_source", "clipping_status")}
    return result
