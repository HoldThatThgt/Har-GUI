"""@brief 根据系统窗口装饰层的真实祖先关系定位标题栏，不执行输入。"""

from har_capture.hdc import Hdc, fail
from har_capture.metadata import layout_bounding_box
from har_capture.window_layout import read_current_window_layout

BUTTON_ROW_ID = "containerModalButtonRowId"


def button_row_path(layout: dict, window_id: int) -> list[dict]:
    """@brief 唯一选择指定窗口可见系统按钮行。 @param layout 当前布局。
    @param window_id 窗口标识。 @return 从布局根到按钮行的节点路径。
    """
    pending, matches = [(layout, [])], []
    while pending:
        node, ancestors = pending.pop()
        if not isinstance(node, dict) or not isinstance(node.get("attributes"), dict):
            fail("Invalid accessibility node while locating the window title bar")
        if not isinstance(node.get("children"), list):
            fail("Invalid accessibility children while locating the window title bar")
        attributes = node["attributes"]
        path = ancestors + [node]
        if attributes.get("hostWindowId") == str(window_id) and attributes.get("id") == BUTTON_ROW_ID:
            if attributes.get("visible") == "true":
                matches.append(path)
        pending.extend((child, path) for child in node["children"])
    if len(matches) != 1:
        fail(f"Expected one visible {BUTTON_ROW_ID} in window {window_id}; found {len(matches)}")
    if matches[0][-1]["attributes"].get("type") != "Row":
        fail("System window button row has an unexpected component type")
    return matches[0]


def title_bar_nodes(path: list[dict], window_id: int) -> tuple[dict, dict, dict]:
    """@brief 验证按钮行属于唯一系统装饰层的首行。 @param path 按钮行祖先路径。
    @param window_id 窗口标识。 @return 装饰层、标题栏及按钮行。
    """
    positions = [index for index, node in enumerate(path) if node["attributes"].get("type") == "ContainerModal"]
    if len(positions) != 1:
        fail("System button row must belong to exactly one ContainerModal")
    index = positions[0]
    if index + 2 >= len(path):
        fail("System button row is missing its enclosing title bar")
    modal, title = path[index:index + 2]
    rows = [child for child in modal["children"] if child["attributes"].get("type") == "Row"]
    if not rows or rows[0] is not title:
        fail("System button row does not belong to the first direct ContainerModal Row")
    for node in path[index:]:
        attributes = node["attributes"]
        if attributes.get("hostWindowId") != str(window_id) or attributes.get("visible") != "true":
            fail("System title bar ancestry has invalid window ownership or visibility")
    return modal, title, path[-1]


def visible_bounds(node: dict) -> dict:
    """@brief 读取非空的屏幕坐标矩形。 @param node 已确认可见的节点。 @return 矩形。"""
    value = node["attributes"].get("bounds")
    if not isinstance(value, str):
        fail("System title bar node has no valid bounds string")
    bounds = layout_bounding_box(value)
    if bounds["width"] <= 0 or bounds["height"] <= 0:
        fail("System title bar node has empty bounds")
    return bounds


def contains_point(bounds: dict, point: dict) -> bool:
    """@brief 按左闭右开的像素边界判断坐标归属。 @param bounds 矩形。 @param point 坐标。 @return 是否位于内部。"""
    return (bounds["x"] <= point["x"] < bounds["x"] + bounds["width"]
            and bounds["y"] <= point["y"] < bounds["y"] + bounds["height"])


def select_window_title_bar(layout: dict, window_id: int) -> dict:
    """@brief 定位可见系统标题栏并排除按钮区域。 @param layout 当前过滤后布局。
    @param window_id 窗口标识。 @return 窗口、标题栏、按钮行矩形及确定的拖动点。
    """
    if type(window_id) is not int or window_id <= 0:
        fail("window_id must be a positive integer")
    path = button_row_path(layout, window_id)
    modal, title, buttons = title_bar_nodes(path, window_id)
    bounds, title_bounds, button_bounds = map(visible_bounds, (modal, title, buttons))
    point = {"x": bounds["x"] + 30, "y": title_bounds["y"] + title_bounds["height"] // 2}
    if point["x"] < 0 or point["y"] < 0:
        fail("System title bar drag point is outside nonnegative screen coordinates")
    if not contains_point(bounds, point) or not contains_point(title_bounds, point):
        fail("System title bar has no valid drag point at the required left inset")
    if contains_point(button_bounds, point):
        fail("System title bar drag point intersects the window control buttons")
    for ancestor in path:
        if ancestor is title:
            break
        if not contains_point(visible_bounds(ancestor), point):
            fail("System title bar drag point is clipped by an ancestor")
    return {"window_id": window_id, "bounds": bounds, "title_bar_bounds": title_bounds,
            "button_row_bounds": button_bounds, "drag_point": point}


def window_title_bar(hdc: Hdc, request: dict) -> dict:
    """@brief 读取最新布局并返回窗口标题栏，不要求最大化按钮存在。 @param hdc 连接。
    @param request 含窗口标识的请求。 @return 确定的标题栏及拖动点。
    """
    window_id = request.get("window_id")
    if type(window_id) is not int or window_id <= 0:
        fail("window_id must be a positive integer")
    return select_window_title_bar(read_current_window_layout(hdc), window_id)
