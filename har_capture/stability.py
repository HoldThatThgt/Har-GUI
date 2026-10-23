"""@brief 记录项目原始动态声明，不将声明匹配或画面相等当作页面语义。"""

import copy

from har_capture.hdc import fail


def validate_rules(value: object) -> list[dict]:
    """@brief 验证原始资源声明格式，不接受像素容差。 @param value 项目声明。 @return 声明副本。"""
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


def observe_stability(rules: list[dict]) -> dict:
    """@brief 核验并保存声明来源，不匹配、遮罩或宣称页面不变。 @param rules 项目声明。 @return 来源记录。"""
    return {"status": "verified", "method": "observation_integrity", "rule_version": 2,
            "rules": validate_rules(rules), "scope": "Project declarations only; no page stability claim or masks."}


def verify_project_rules(metadata: dict, options: dict) -> None:
    """@brief 核对新采集记录的声明来源，不从声明推导页面验收。 @param metadata 采集信息。
    @param options 当前项目导出参数。
    """
    expected = observe_stability(options.get("stability_regions", []))
    if metadata.get("stability") != expected:
        fail("Capture observation declarations differ from the current project rules")
