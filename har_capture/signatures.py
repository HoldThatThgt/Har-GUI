"""@brief 从本地控件树生成跨运行页面和状态签名，不保存正文文本。"""

import hashlib
import json
import re

from har_capture.hdc import fail
from har_capture.metadata import layout_bounding_box

CURSOR_TYPES = {"Caret", "Cursor", "CaretNode", "CursorNode"}
CLOCK_TYPES = {"TextClock", "TextTimer"}
EDIT_TYPES = {"TextInput", "TextArea", "TextField", "RichEditor", "SearchField", "EditText"}
CONTENT_KEYS = ("text", "originalText", "description", "hint", "Content", "Hint", "textContent")
TEXT_KEYS = ("text", "originalText", "Content", "textContent")
STATE_KEYS = ("checked", "selected", "enabled", "focused", "editable", "editing", "scrollable",
              "scrollOffset", "scrollX", "scrollY", "currentOffset", "totalOffset", "selectionStart",
              "selectionEnd", "CurrentOffset", "TotalOffset", "ScrollOffset", "IsEditing", "CaretPosition")
RESOURCE_KEYS = ("id", "resource-id", "resourceId", "inspectorId")
CLOCK_TEXT = re.compile(r"(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?|\d{4}[-/]\d{1,2}[-/]\d{1,2})", re.I)
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def digest(value: object) -> str:
    """@brief 对规范 JSON 计算 SHA256。 @param value 可序列化的数据。 @return 十六进制摘要。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stable_resources(attributes: dict) -> dict:
    """@brief 保留开发者资源名，排除运行数值标识及 UUID。 @param attributes 属性。 @return 稳定资源名。"""
    result = {}
    for key in RESOURCE_KEYS:
        value = attributes.get(key)
        if isinstance(value, str) and value and not value.isdecimal():
            result[key] = UUID_TEXT.sub("<uuid>", value)
    return result


def structural_tree(node: dict) -> object:
    """@brief 保留组件类型和层次，去除文本、运行身份、绝对坐标及光标节点。 @param node 树节点。 @return 结构。"""
    attributes = node["attributes"]
    if attributes.get("type") in CURSOR_TYPES:
        return None
    children = [structural_tree(child) for child in node["children"]]
    return {"type": attributes.get("type"), "role": attributes.get("role"),
            "resources": stable_resources(attributes), "children": [item for item in children if item is not None]}


def relative_bounds(value: str, window_box: dict) -> dict:
    """@brief 将无障碍矩形转换为窗口内相对位置用于识别滚动。 @param value 矩形。
    @param window_box 窗口屏幕矩形。 @return 相对矩形。
    """
    box = layout_bounding_box(value)
    return {**box, "x": box["x"] - window_box["x"], "y": box["y"] - window_box["y"]}


def node_state(node: dict, window_box: dict) -> dict:
    """@brief 读取状态事实；阅读和编辑正文仅留本地摘要，时钟变化不计状态。 @param node 树节点。
    @param window_box 窗口矩形。 @return 节点状态。
    """
    attrs = node["attributes"]
    kind = str(attrs.get("type", "")).split(",", 1)[0]
    editing = kind in EDIT_TYPES or str(attrs.get("editable", "")).lower() == "true"
    clock_text = any(CLOCK_TEXT.fullmatch(str(attrs.get(key, "")).strip()) for key in TEXT_KEYS)
    clock = kind in CLOCK_TYPES or (not editing and clock_text)
    state = {key: attrs[key] for key in STATE_KEYS if key in attrs}
    if not clock and isinstance(attrs.get("bounds"), str):
        state["bounds"] = relative_bounds(attrs["bounds"], window_box)
    content = {key: attrs[key] for key in CONTENT_KEYS if key in attrs}
    if content and not clock:
        state["content_sha256"] = digest(content)
    return state


def state_tree(node: dict, window_box: dict) -> object:
    """@brief 保存可检验状态和相对布局，忽略光标出现与消失。 @param node 树节点。
    @param window_box 窗口矩形。 @return 规范状态树。
    """
    if node["attributes"].get("type") in CURSOR_TYPES:
        return None
    children = [state_tree(child, window_box) for child in node["children"]]
    return {"state": node_state(node, window_box), "children": [item for item in children if item is not None]}


def index_trees(document: dict) -> dict:
    """@brief 验证窗口树唯一性并建立本次关联。 @param document 导出树文件。 @return 窗口到树的映射。"""
    result = {}
    for item in document["windows"]:
        window_id = item["window_id"]
        if window_id in result:
            fail(f"Duplicate signature window: {window_id}")
        result[window_id] = item
    return result


def window_signature(window: dict, accessibility: dict, arkui: dict) -> dict:
    """@brief 生成不依赖窗口 ID 和进程 ID 的窗口签名。 @param window 窗口事实。
    @param accessibility 无障碍窗口树。 @param arkui ArkUI 窗口树。 @return 页面和状态字段。
    """
    bundle = window["bundle_name"]
    if any(tree["bundle_name"] != bundle for tree in (accessibility, arkui)):
        fail(f"Signature bundle mismatch for window {window['window_id']}")
    trees = (accessibility["tree"], arkui["tree"])
    structure = digest([structural_tree(tree) for tree in trees])
    box = window["bounding_box"]
    return {"bundle_name": bundle, "window_type": window["window_type"], "page_hash": structure,
            "window_mode": window["window_mode"], "bounding_box": box, "transform": window["transform"],
            "state": digest([state_tree(tree, box) for tree in trees])}


def build_signatures(wms: dict, accessibility: dict, arkui: dict, target_bundle_name: str) -> dict:
    """@brief 对目标应用分组，对整屏应用状态去重；相对层级替代易变 ZOrd。
    @param wms 窗口文件。 @param accessibility 应用无障碍文件。 @param arkui 应用 ArkUI 文件。
    @param target_bundle_name 配额归属的应用。 @return 页面、状态摘要及各窗口页面摘要。
    """
    windows = wms["app_windows"]
    indexed = [index_trees(accessibility), index_trees(arkui)]
    window_ids = {window["window_id"] for window in windows}
    if not windows or len(window_ids) != len(windows) or any(set(tree) != window_ids for tree in indexed):
        fail("Signature trees do not cover exactly the visible application windows")
    values = [window_signature(window, indexed[0][window["window_id"]], indexed[1][window["window_id"]])
              for window in windows]
    target = sorted(item["page_hash"] for item in values if item["bundle_name"] == target_bundle_name)
    if not target:
        fail("Signature target application is not visible")
    ranked = sorted(zip(windows, values), key=lambda pair: (pair[0]["z_order"], digest(pair[1])))
    state = [{**value, "z_rank": rank} for rank, (_, value) in enumerate(ranked)]
    pages = [{"bundle_name": item["bundle_name"], "page_hash": item["page_hash"]} for item in values]
    return {"signature_version": 1, "page_hash": digest({"bundle_name": target_bundle_name, "pages": target}),
            "state_hash": digest({"canvas": wms["canvas"], "window_mode": wms["window_mode"], "windows": state}),
            "window_pages": sorted(pages, key=digest)}
