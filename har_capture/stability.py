"""@brief 以项目明确声明和系统窗口证据限定无关动态叶子的比较范围。"""

import copy

from har_capture.hdc import fail
from har_capture.metadata import layout_bounding_box


def validate_rules(value: object) -> list[dict]:
    """@brief 验证只含资源选择器的规则，不接受像素区域或容差。 @param value 项目规则。 @return 规则副本。"""
    if not isinstance(value, list):
        fail("Stability regions must be an array of resource selectors")
    seen = set()
    for rule in value:
        if not isinstance(rule, dict) or set(rule) != {"bundle_name", "resource_id"}:
            fail("Stability region requires only bundle_name and resource_id")
        if any(not isinstance(item, str) or not item.strip() or item != item.strip() for item in rule.values()):
            fail("Stability selector fields must be nonempty trimmed strings")
        identity = (rule["bundle_name"], rule["resource_id"])
        if identity in seen:
            fail("Stability region selectors must be distinct")
        seen.add(identity)
    return copy.deepcopy(value)


def matching_leaves(layout: dict, rule: dict) -> list[dict]:
    """@brief 精确匹配窗口归属下的资源叶子，保留重叠的多个节点。 @param layout 原始树。
    @param rule 单项项目声明。 @return 带节点路径的匹配记录。
    """
    pending, matches = [(layout, [], None)], []
    while pending:
        node, path, inherited_bundle = pending.pop()
        attributes = node.get("attributes", {})
        bundle = attributes.get("bundleName", inherited_bundle)
        if bundle == rule["bundle_name"] and attributes.get("id") == rule["resource_id"]:
            if node.get("children") or attributes.get("type") not in ("Text", "Image", "SymbolGlyph"):
                fail("Stability selectors must identify Text, Image, or SymbolGlyph leaves")
            host = attributes.get("hostWindowId", "")
            if not isinstance(host, str) or not host.isdecimal():
                fail("Stability leaf has no explicit window identity")
            box = layout_bounding_box(attributes.get("bounds", ""))
            matches.append({**rule, "window_id": int(host), "node_path": path,
                            "type": attributes["type"], "bounds": [box["x"], box["y"],
                                box["x"] + box["width"], box["y"] + box["height"]]})
        pending.extend((child, [*path, index], bundle) for index, child in reversed(
            list(enumerate(node.get("children", [])))))
    if not matches:
        fail(f"Stability selector matched no leaf: {rule['bundle_name']} / {rule['resource_id']}")
    return matches


def verify_system_leaf(leaf: dict, window: dict, proof: dict) -> None:
    """@brief 用原始会话身份和窗口画布边界验证系统叶子。 @param leaf 已匹配叶子。
    @param window 原始 WMS 窗口。 @param proof 原始会话与画布。
    """
    session = proof["sessions"].get(leaf["window_id"], {})
    if not window or session.get("is_system") is not True:
        fail("Stability region is not proven to belong to a system window")
    if any(session.get(key) != window[key] for key in ("window_id", "process_id", "display_id")):
        fail("Stability region system-session identity differs from WMS")
    if (window["display_id"] != proof["display"]["id"] or window["z_order"] < 0
            or session.get("is_visible") is not True or session.get("rs_visible") is False
            or session.get("visibility_state") == 2):
        fail("Stability region does not belong to an exposed system window")
    left, top, right, bottom = leaf["bounds"]
    box, display = window["bounding_box"], proof["display"]
    if not (0 <= left < right <= display["width"] and 0 <= top < bottom <= display["height"]
            and box["x"] <= left < right <= box["x"] + box["width"]
            and box["y"] <= top < bottom <= box["y"] + box["height"]):
        fail("Stability leaf bounds are outside its system window or canvas")


def resolve_stability(states: dict, proof: dict, rules: list[dict]) -> dict:
    """@brief 由首尾原始树重新核对声明、匹配数量和系统归属。 @param states 首尾状态。
    @param proof 原始会话及画布。 @param rules 项目选择器。 @return 不含正文的验证证据。
    """
    rules = validate_rules(rules)
    leaves = []
    for rule in rules:
        before = matching_leaves(states["before"]["layout"], rule)
        after = matching_leaves(states["after"]["layout"], rule)
        if before != after or len({leaf["window_id"] for leaf in before}) != 1:
            fail("Stability selector match count, type, bounds, path, or window changed")
        for leaf in before:
            for state in states.values():
                verify_system_leaf(leaf, state["windows"].get(leaf["window_id"]), proof)
        leaves.extend(before)
    return {"status": "verified", "rule_version": 1, "rules": rules, "regions": leaves,
            "allowed_text_fields": ["text", "originalText"]}


def comparison_layout(layout: dict, stability: dict) -> dict:
    """@brief 仅在比较副本中替换已验证叶子的动态正文。 @param layout 原始树。
    @param stability 经过验证的选择结果。 @return 保留其他所有字段的副本。
    """
    result = copy.deepcopy(layout)
    for leaf in stability["regions"]:
        node = result
        for index in leaf["node_path"]:
            node = node["children"][index]
        attributes = node["attributes"]
        for field in ("text", "originalText"):
            if field in attributes:
                attributes[field] = "<declared-system-dynamic-text>"
    return result


def verify_project_rules(metadata: dict, options: dict) -> None:
    """@brief 拒绝由采集结果自行扩大当前项目允许的动态范围。 @param metadata 采集信息。
    @param options 当前项目导出参数。
    """
    rules = validate_rules(options.get("stability_regions", []))
    stability = metadata.get("stability", {})
    if stability.get("status") != "verified" or stability.get("rules") != rules:
        fail("Capture stability selectors differ from the current project rules")
