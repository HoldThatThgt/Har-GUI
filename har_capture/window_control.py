"""@brief 从当前系统装饰层唯一定位窗口最大化或还原按钮，不执行界面操作。"""

from har_capture.hdc import Hdc, fail
from har_capture.window_layout import read_current_window_layout
from har_capture.window_title_bar import button_row_path, contains_point, title_bar_nodes, visible_bounds

CONTROL_IDS = frozenset({"EnhanceMaximizeBtn", "EnhanceCompatibleMaximizeBtn"})


def size_control_paths(buttons: dict, window_id: int) -> list[list[dict]]:
    """@brief 在系统按钮行内同时筛选已核实的最大化控件。 @param buttons 系统按钮行。
    @param window_id 目标窗口。 @return 所有可操作候选的祖先路径，不按控件名称设置优先级。
    """
    pending, matches = [(buttons, [])], []
    while pending:
        node, ancestors = pending.pop()
        attributes = node["attributes"]
        path = ancestors + [node]
        if (attributes.get("hostWindowId") == str(window_id) and attributes.get("id") in CONTROL_IDS
                and attributes.get("type") == "Button"
                and all(attributes.get(field) == "true" for field in ("visible", "enabled", "clickable"))):
            matches.append(path)
        pending.extend((child, path) for child in node["children"])
    return matches


def select_window_control(layout: dict, window_id: int) -> dict:
    """@brief 严格选取当前窗口系统标题栏内唯一可操作按钮。 @param layout 当前原始树。
    @param window_id 目标窗口。 @return 控件真实身份、矩形及中心坐标。
    """
    if type(window_id) is not int or window_id <= 0:
        fail("window_id must be a positive integer")
    path = button_row_path(layout, window_id)
    title_bar_nodes(path, window_id)
    matches = size_control_paths(path[-1], window_id)
    if len(matches) != 1:
        fail(f"Expected one actionable system maximize control in window {window_id}; found {len(matches)}")
    control_path = matches[0]
    attributes = control_path[-1]["attributes"]
    bounds = visible_bounds(control_path[-1])
    center = {"x": bounds["x"] + bounds["width"] // 2, "y": bounds["y"] + bounds["height"] // 2}
    if center["x"] < 0 or center["y"] < 0:
        fail("Window size control is outside nonnegative screen coordinates")
    for node in control_path:
        ancestor = node["attributes"]
        if ancestor.get("hostWindowId") != str(window_id) or ancestor.get("visible") != "true":
            fail("Window size control ancestry has invalid window ownership or visibility")
    if any(not contains_point(visible_bounds(node), center) for node in path[:-1] + control_path):
        fail("Window size control center is clipped by an ancestor")
    accessibility_id = attributes.get("accessibilityId")
    if not isinstance(accessibility_id, str) or not accessibility_id.isdecimal():
        fail("Window size control has no valid accessibility ID")
    return {"window_id": window_id, "control_id": attributes["id"], "bounds": bounds,
            "center": center, "accessibility_id": accessibility_id}


def window_size_control(hdc: Hdc, request: dict) -> dict:
    """@brief 从最新布局返回最大化或还原按钮。 @param hdc 连接。
    @param request 含窗口标识的请求。 @return 当前唯一最大化或还原按钮。
    """
    window_id = request.get("window_id")
    if type(window_id) is not int or window_id <= 0:
        fail("window_id must be a positive integer")
    return select_window_control(read_current_window_layout(hdc), window_id)
