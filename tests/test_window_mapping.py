"""@brief 验证分数原点下的 SCB 精确窗口映射及跨来源拒收。"""

import copy
import re
import unittest

from har_capture.coordinates import float32, float32_interval_near_equal, normalize_arkui, wms_scale_interval


def component(node_id, tag, local, properties=None):
    """@brief 构造保留自身属性及原文的匿名组件。"""
    attributes = {"ID": str(node_id), "type": tag, "AccessibilityId": str(node_id),
                  "FrameRect": local, "PaintRect without transform": local, "Center": "x:0 y:0"}
    attributes.update(properties or {})
    return {"attributes": attributes, "source_attributes": dict(attributes), "children": [],
            "source_line": node_id + 1, "raw": ""}


def render_line(depth, kind, node_id, bounds, suffix=""):
    """@brief 构造包含完整修饰器字段的渲染节点行。"""
    markers = (suffix.find(name) for name in ("DrawCmdModifiers", "OtherModifiers:") if name in suffix)
    boundary = min(markers, default=len(suffix))
    pattern = (r"\b(?:PrepareSeq|PostPrepareSeq|VsyncId|IsSubTreeSkipped|ReverseChildren|"
               r"hasDrawContent|hasChildrenDrawContent):\s*(?:\d+|true|false)\s*,?\s*")
    prefix = re.findall(pattern, suffix[:boundary])
    properties = re.sub(pattern, "", suffix[:boundary]) + suffix[boundary:]
    header = "".join(value.strip().rstrip(",") + ", " for value in prefix)
    return ("  " * depth + f"| {kind}[{node_id}], " + header
            + f"Properties: Bounds[{bounds}] Frame[{bounds}], " + properties + "\n")


def replace_render_text(text, original, replacement):
    """@brief 确认匿名渲染证据实际被修改，防止格式调整后反例空转。"""
    if original not in text:
        raise AssertionError("Synthetic render mutation source is missing")
    return text.replace(original, replacement)


def mapping_fixture(system=False):
    """@brief 构造与实机一致的分数原点、真实缩放及 WMS 整数外包比例。"""
    screen = component(10, "Screen", "RectT (0.00, 0.00) - [3120.00 x 2080.00]")
    screen["raw"] = "DisplayNode[6962141986818] child[] lazyLoad[0] nodeState[ACTIVE]"
    scene = component(700, "WindowScene", "RectT (203.30, 0.00) - [2048.00 x 2230.00]", {
        "AccessibilityId": "7818", "compid": "session106", "scale(x,y)": "0.876682,0.876682"})
    scene["raw"] = "SurfaceNode[19649475381948] child[] lazyLoad[0] nodeState[ACTIVE]"
    native_scene = {"$ID": 700, "$type": "WindowScene", "compid": "session106",
                    "$rect": "[203.30,0.00],[1998.74,1955.00]"}
    scene_native = {"$ID": 10, "$type": "Screen", "$rect": "[0.00,0.00],[3120.00,2080.00]",
                    "$children": [native_scene]}
    root, native = application_fixture()
    window = {"window_id": 106, "process_id": 60090, "display_id": 0, "ui_node_id": 7818,
              "is_system": system, "orientation": 0,
              "bounding_box": {"x": 203, "y": 0, "width": 2048, "height": 2230},
              "transform": {"offset_x": 0, "offset_y": 0, "scale_x": 0.876953,
                            "scale_y": 0.876682, "pivot_x": 0, "pivot_y": 0}}
    evidence = {"arkui_geometry": native, "render_tree": mapping_render_tree(),
                "render_detached_tree": "-- Render Node Not On Tree\n",
                "scene": {"path": [screen, scene], "native": scene_native,
                          "source": {"process_id": 4575, "display_id": 0, "source_window_id": 5}}}
    if system:
        root, evidence["arkui_geometry"] = system_fixture(scene, scene_native)
        window["process_id"] = 4575
        evidence["render_tree"] = mapping_render_tree(system=True)
    return root, window, {"width": 3120, "height": 2080}, evidence


def application_fixture():
    """@brief 构造独立于 SCB 坐标系的应用局部树。"""
    root = component(1, "root", "RectT (0.00, 0.00) - [2048.00 x 2230.00]")
    modal = component(3, "ContainerModal", "RectT (0.00, 0.00) - [2048.00 x 2230.00]")
    text = component(4, "Text", "RectT (10.00, 20.00) - [100.00 x 30.00]")
    for node, render_id in ((root, 100), (modal, 103), (text, 104)):
        node["raw"] = f"CanvasNode[{render_id}] child[] lazyLoad[0] nodeState[ACTIVE]"
    root["children"], modal["children"] = [modal], [text]
    native_text = {"$ID": 4, "$type": "Text", "$rect": "[10.00,20.00],[110.00,50.00]"}
    native_modal = {"$ID": 3, "$type": "ContainerModal", "$rect": "[0.00,0.00],[2048.00,2230.00]",
                    "$children": [native_text]}
    native = {"$ID": 1, "$type": "root", "$rect": "[0.00,0.00],[2048.00,2230.00]",
              "$children": [native_modal]}
    return root, native


def moved_window_fixture():
    """@brief 构造冻结 UC 仅移动原点而 WMS 按官方门槛保留旧比例的场景。"""
    args = mapping_fixture()
    args[1]["bounding_box"].update(x=300, y=300)
    args[1]["transform"].update(scale_x=0.71875, scale_y=0.718386)
    scene = args[3]["scene"]
    node = scene["path"][-1]
    node["attributes"].update({"FrameRect": "RectT (0.00, 0.00) - [2048.00 x 2230.00]",
                                "PaintRect without transform": "RectT (300.00, 300.00) - [2048.00 x 2230.00]",
                                "scale(x,y)": "0.718206,0.718206"})
    scene["native"]["$children"][0]["$rect"] = "[300.00,300.00],[1770.89,1901.60]"
    args[3]["render_tree"] = args[3]["render_tree"].replace("203.3 0 2048 2230", "300 300 2048 2230")
    args[3]["render_tree"] = args[3]["render_tree"].replace("0.876682", "0.718206")
    return args


def system_fixture(scene, full_native):
    """@brief 构造自身已处于 SCB 屏幕空间的系统窗口树。"""
    root = copy.deepcopy(scene)
    text = component(701, "Text", "RectT (10.00, 20.00) - [100.00 x 30.00]")
    text["raw"] = "CanvasNode[701] child[] lazyLoad[0] nodeState[ACTIVE]"
    root["children"] = [text]
    full_native["$children"][0]["$children"] = [
        {"$ID": 701, "$type": "Text", "$rect": "[212.07,17.53],[299.74,43.83]"}]
    return root, full_native


def mapping_render_tree(system=False):
    """@brief 保留屏幕、逻辑显示器、会话 Surface 和应用根的真实祖先关系。"""
    rows = [render_line(0, "SCREEN_NODE", 90, "0 0 3120 2080", "screenId[0], OtherModifiers:[]"),
            render_line(1, "LOGICAL_DISPLAY_NODE", 6962141986818, "0 0 3120 2080",
                        "OtherModifiers:[Pivot:[x:0 y:0]]"),
            render_line(2, "SURFACE_NODE", 19649475381948, "203.3 0 2048 2230",
                        "nodeName: [session106], ClipToFrame[True], OtherModifiers:[Scale:[x:0.876682 y:0.876682], "
                        "Pivot:[x:0 y:0], ClipToFrame:[true]]")]
    if system:
        rows.append(render_line(3, "CANVAS_NODE", 701, "10 20 100 30", "OtherModifiers:[]"))
    else:
        rows.extend([render_line(3, "ROOT_NODE", 100, "0 0 2048 2230", "OtherModifiers:[]"),
                     render_line(4, "CANVAS_NODE", 103, "0 0 2048 2230", "OtherModifiers:[]"),
                     render_line(5, "CANVAS_NODE", 104, "10 20 100 30", "OtherModifiers:[]")])
    return "".join(rows)


def ancestor_mapping_fixture(reversed_render_order=False):
    """@brief 构造父组缩放，使交换 RS 父序会产生不同屏幕原点。"""
    args = mapping_fixture()
    scene = args[3]["scene"]
    group = component(11, "Column", "RectT (0.00, 0.00) - [3120.00 x 2080.00]",
                      {"scale(x,y)": "0.5,0.5"})
    group["raw"] = "CanvasNode[111] child[] lazyLoad[0] nodeState[ACTIVE]"
    scene["path"].insert(1, group)
    target = scene["native"]["$children"][0]
    target["$rect"] = "[101.65,0.00],[999.37,977.50]"
    scene["native"]["$children"] = [{"$ID": 11, "$type": "Column", "$rect": "[0.00,0.00],[1560.00,1040.00]",
                                      "$children": [target]}]
    args[1]["bounding_box"]["x"] = 101
    args[1]["transform"].update(scale_x=899 / 2048, scale_y=978 / 2230)
    rows = args[3]["render_tree"].splitlines(keepends=True)
    group_row = render_line(2 if not reversed_render_order else 3, "CANVAS_NODE", 111, "0 0 3120 2080",
                            "OtherModifiers:[Scale:[x:0.5 y:0.5], Pivot:[x:0 y:0]]")
    if reversed_render_order:
        rows = rows[:3] + [group_row] + ["  " + row for row in rows[3:]]
    else:
        rows = rows[:2] + [group_row] + ["  " + row for row in rows[2:]]
    args[3]["render_tree"] = "".join(rows)
    return args


def omitted_ancestor_fixture(state, transformed=False):
    """@brief 为缺失渲染祖先提供明确懒加载或未挂载证据。"""
    args = ancestor_mapping_fixture()
    scene = args[3]["scene"]
    group = scene["path"][1]
    rows = args[3]["render_tree"].splitlines(keepends=True)
    detached_row = rows[2]
    if not transformed:
        group["attributes"]["scale(x,y)"] = "1,1"
        native_group = scene["native"]["$children"][0]
        native_group["$rect"] = "[0.00,0.00],[3120.00,2080.00]"
        native_group["$children"][0]["$rect"] = "[203.30,0.00],[1998.74,1955.00]"
        args[1]["bounding_box"]["x"] = 203
        args[1]["transform"].update(scale_x=0.876953, scale_y=0.876682)
        detached_row = detached_row.replace("Scale:[x:0.5 y:0.5]", "Scale:[x:1 y:1]")
    args[3]["render_tree"] = "".join(rows[:2]) + "".join(row[2:] for row in rows[3:])
    if state == "lazy":
        group["raw"] = group["raw"].replace("lazyLoad[0] nodeState[ACTIVE]", "lazyLoad[1] nodeState[LAZY_LOAD]")
    elif state == "detached":
        args[3]["render_detached_tree"] += detached_row.strip() + "\n"
    return args


def no_draw_leaf_fixture():
    """@brief 构造零面积、非有限 RS 矩形且有完整无绘制叶节点证据的组件。"""
    args = mapping_fixture()
    leaf = args[0]["children"][0]["children"][0]
    local = "RectT (10.00, 20.00) - [0.00 x 0.00]"
    leaf["attributes"].update({"FrameRect": local, "PaintRect without transform": local})
    args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[10.00,20.00],[10.00,20.00]"
    args[3]["render_tree"] = replace_render_text(args[3]["render_tree"],
        render_line(5, "CANVAS_NODE", 104, "10 20 100 30", "OtherModifiers:[]"),
        render_line(5, "CANVAS_NODE", 104, "-inf -inf -inf -inf", "PrepareSeq: 130, PostPrepareSeq: 129, "
                    "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]"))
    return args


def mixed_leaf_fixture(width=-1, height=11, structural_children=False):
    """@brief 构造符号一致、原生端点闭合且 RS 明确无绘制的混合尺寸叶节点。"""
    args = mapping_fixture()
    leaf = args[0]["children"][0]["children"][0]
    local = f"RectT (10.00, 20.00) - [{width:.2f} x {height:.2f}]"
    leaf["attributes"].update({"FrameRect": local, "PaintRect without transform": local})
    native = args[3]["arkui_geometry"]["$children"][0]["$children"][0]
    left, right = min(10, 10 + width), max(10, 10 + width)
    top, bottom = min(20, 20 + height), max(20, 20 + height)
    native["$rect"] = f"[{left:.2f},{top:.2f}],[{right:.2f},{bottom:.2f}]"
    bounds = f"10 20 {width:g} {height:g}"
    args[3]["render_tree"] = replace_render_text(args[3]["render_tree"],
        render_line(5, "CANVAS_NODE", 104, "10 20 100 30", "OtherModifiers:[]"),
        render_line(5, "CANVAS_NODE", 104, bounds,
                    "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]"))
    if structural_children:
        for node_id in (5, 6, 7):
            attrs = {"ID": str(node_id), "type": "IfElse"}
            leaf["children"].append({"attributes": attrs, "source_attributes": dict(attrs),
                                     "children": [], "source_line": node_id + 1, "raw": ""})
        native["$children"] = [{"$ID": node_id, "$type": "IfElse"} for node_id in (5, 6, 7)]
    return args


def transformed_mixed_leaf_fixture():
    """@brief 将带符号端点依次经过父缩放和叶缩放、中心点、平移，提供独立原生矩形。"""
    args = mixed_leaf_fixture()
    parent = args[0]["children"][0]
    parent_rect = "RectT (20.00, 30.00) - [200.00 x 100.00]"
    parent["attributes"].update({"FrameRect": parent_rect, "PaintRect without transform": parent_rect,
                                 "scale(x,y)": "2,0.5", "Center": "x:0.5 y:0.5"})
    parent["children"][0]["attributes"].update({"scale(x,y)": "2,0.5", "Center": "x:0.25 y:0.75",
                                              "translate(x,y)": "3,-2"})
    native = args[3]["arkui_geometry"]["$children"][0]
    native["$rect"] = "[-80.00,55.00],[320.00,105.00]"
    native["$children"][0]["$rect"] = "[-57.50,66.06],[-53.50,68.81]"
    args[3]["render_tree"] = args[3]["render_tree"].replace(
        "CANVAS_NODE[103], Properties: Bounds[0 0 2048 2230] Frame[0 0 2048 2230], OtherModifiers:[]",
        "CANVAS_NODE[103], Properties: Bounds[20 30 200 100] Frame[20 30 200 100], "
        "OtherModifiers:[Scale:[x:2 y:0.5], Pivot:[x:0.5 y:0.5]]")
    args[3]["render_tree"] = replace_render_text(args[3]["render_tree"],
        "Frame[10 20 -1 11], OtherModifiers:[]", "Frame[10 20 -1 11], "
        "OtherModifiers:[Scale:[x:2 y:0.5], Pivot:[x:0.25 y:0.75], Translate:[x:3 y:-2]]")
    return args


class WindowMappingTests(unittest.TestCase):
    def assert_failed(self, args, code=None):
        with self.assertLogs(level="ERROR"):
            result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        if code is not None:
            self.assertTrue(any(code in item["code"] for item in result["coordinate_validation"]["issues"]),
                            result["coordinate_validation"]["issues"])
        return result

    def test_fractional_origin_uses_exact_scene_scale_instead_of_wms_outer_ratio(self):
        args = mapping_fixture()
        before = copy.deepcopy(args)
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        bounds = result["geometry"]["screen_bounds"]
        for key, value in {"x": 203.3, "y": 0, "width": 1795.444736, "height": 1955.00086}.items():
            self.assertAlmostEqual(bounds[key], value, places=5)
        self.assertNotAlmostEqual(bounds["width"], 2048 * args[1]["transform"]["scale_x"], places=2)
        text = result["children"][0]["children"][0]["geometry"]
        for key, value in {"x": 212.06682, "y": 17.53364, "width": 87.6682, "height": 26.30046}.items():
            self.assertAlmostEqual(text["screen_bounds"][key], value, places=5)
        self.assertEqual(text["source"]["window_mapping"], {
            "method": "SceneBoard.ArkUI.PaintRectAncestorComposition", "source": args[3]["scene"]["source"],
            "session_node_id": "700", "session_render_node_id": "19649475381948",
            "viewport_node_id": "700", "viewport_render_node_id": "19649475381948",
            "display_render_node_id": "6962141986818", "screen_render_node_id": "90",
            "coordinate_space": "window"})
        clip = next(item for item in text["clip_chain"] if item.get("render_node_id") == "19649475381948")
        for key in bounds:
            self.assertAlmostEqual(clip["bounds"][key], bounds[key], places=5)
        self.assertEqual(args, before)

    def test_system_scene_native_bounds_are_not_scaled_twice(self):
        result = normalize_arkui(*mapping_fixture(system=True))
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        geometry = result["children"][0]["geometry"]
        self.assertAlmostEqual(geometry["screen_bounds"]["x"], 212.06682, places=5)
        self.assertAlmostEqual(geometry["screen_bounds"]["width"], 87.6682, places=5)
        self.assertEqual(geometry["source"]["window_mapping"]["coordinate_space"], "scene")
        self.assertEqual(geometry["source"]["window_mapping"]["viewport_node_id"], "700")
        self.assertEqual(geometry["source"]["window_mapping"]["viewport_render_node_id"], "19649475381948")

    def test_intermediate_application_surface_clip_requires_its_scb_native_projection(self):
        args = mapping_fixture()
        child = component(701, "WindowScene", "RectT (0.00, 0.00) - [2048.00 x 2230.00]")
        child["raw"] = "SurfaceNode[110] child[] lazyLoad[0] nodeState[ACTIVE]"
        args[3]["scene"]["path"][-1]["children"] = [child]
        args[3]["scene"]["native"]["$children"][0]["$children"] = [
            {"$ID": 701, "$type": "WindowScene", "$rect": "[203.30,0.00],[1998.74,1955.00]"}]
        rows = args[3]["render_tree"].splitlines(keepends=True)
        surface = render_line(3, "SURFACE_NODE", 110, "0 0 2048 2230",
                              "ClipToFrame[True], OtherModifiers:[Pivot:[x:0 y:0], ClipToFrame:[true]]")
        args[3]["render_tree"] = "".join(rows[:3]) + surface + "".join("  " + row for row in rows[3:])
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        geometry = result["children"][0]["children"][0]["geometry"]
        clip = next(item for item in geometry["clip_chain"] if item.get("render_node_id") == "110")
        for key, value in result["geometry"]["screen_bounds"].items():
            self.assertAlmostEqual(clip["bounds"][key], value, places=5)
        args[3]["scene"]["native"]["$children"][0]["$children"][0]["$rect"] = (
            "[204.30,0.00],[1999.74,1955.00]")
        self.assert_failed(args, "projection")

    def test_scaled_application_surface_maps_the_actual_native_viewport(self):
        args = mapping_fixture()
        child = component(701, "WindowScene", "RectT (0.00, 0.00) - [2048.00 x 2230.00]",
                          {"scale(x,y)": "0.5,0.5"})
        child["raw"] = "SurfaceNode[110] child[] lazyLoad[0] nodeState[ACTIVE]"
        args[3]["scene"]["path"][-1]["children"] = [child]
        args[3]["scene"]["native"]["$children"][0]["$children"] = [
            {"$ID": 701, "$type": "WindowScene", "$rect": "[203.30,0.00],[1101.02,977.50]"}]
        rows = args[3]["render_tree"].splitlines(keepends=True)
        surface = render_line(3, "SURFACE_NODE", 110, "0 0 2048 2230",
                              "ClipToFrame[True], OtherModifiers:[Scale:[x:0.5 y:0.5], "
                              "Pivot:[x:0 y:0], ClipToFrame:[true]]")
        args[3]["render_tree"] = "".join(rows[:3]) + surface + "".join("  " + row for row in rows[3:])
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        text = result["children"][0]["children"][0]["geometry"]
        for key, value in {"x": 207.68341, "y": 8.76682, "width": 43.8341, "height": 13.15023}.items():
            self.assertAlmostEqual(text["screen_bounds"][key], value, places=5)
        self.assertEqual(text["source"]["window_mapping"]["viewport_node_id"], "701")
        self.assertEqual(text["source"]["window_mapping"]["viewport_render_node_id"], "110")

    def test_nonzero_application_root_origin_is_kept_in_window_local_coordinates(self):
        args = mapping_fixture()
        local = "RectT (100.00, 0.00) - [2048.00 x 2230.00]"
        args[0]["attributes"].update({"FrameRect": local, "PaintRect without transform": local})
        native = args[3]["arkui_geometry"]
        native["$rect"] = "[100.00,0.00],[2148.00,2230.00]"
        native["$children"][0]["$rect"] = "[100.00,0.00],[2148.00,2230.00]"
        native["$children"][0]["$children"][0]["$rect"] = "[110.00,20.00],[210.00,50.00]"
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "ROOT_NODE[100], Properties: Bounds[0 0 2048 2230] Frame[0 0 2048 2230]",
            "ROOT_NODE[100], Properties: Bounds[100 0 2048 2230] Frame[100 0 2048 2230]")
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        self.assertAlmostEqual(result["geometry"]["screen_bounds"]["x"], 290.9682, places=5)
        text = result["children"][0]["children"][0]["geometry"]
        self.assertAlmostEqual(text["screen_bounds"]["x"], 299.73502, places=5)
        self.assertAlmostEqual(text["screen_bounds"]["width"], 87.6682, places=5)

    def test_application_frame_cannot_ignore_a_scale_changed_only_in_rs(self):
        args = mapping_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "Bounds[10 20 100 30] Frame[10 20 100 30], OtherModifiers:[]",
            "Bounds[10 20 100 30] Frame[10 20 100 30], OtherModifiers:[Scale:[x:2 y:1]]")
        self.assert_failed(args, "render_")

    def test_empty_nonfinite_rs_leaf_with_complete_no_draw_evidence_is_not_rendered(self):
        result = normalize_arkui(*no_draw_leaf_fixture())
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        geometry = result["children"][0]["children"][0]["geometry"]
        self.assertEqual(geometry["render_state"], "not_rendered")
        self.assertFalse(geometry["rendered"])
        self.assertEqual(geometry["screen_bounds"]["width"], 0)
        self.assertEqual(geometry["screen_bounds"]["height"], 0)

    def test_nonfinite_frame_needs_complete_no_draw_leaf_and_empty_native_geometry(self):
        changes = (("hasDrawContent: 0, ", ""), ("hasChildrenDrawContent: 0, ", ""),
                   ("hasDrawContent: 0", "hasDrawContent: 1"),
                   ("hasChildrenDrawContent: 0", "hasChildrenDrawContent: 1"))
        for original, replacement in changes:
            with self.subTest(original=original, replacement=replacement):
                args = no_draw_leaf_fixture()
                args[3]["render_tree"] = args[3]["render_tree"].replace(original, replacement, 1)
                self.assert_failed(args)
        args = no_draw_leaf_fixture()
        args[3]["render_tree"] += render_line(6, "CANVAS_NODE", 105, "0 0 1 1", "OtherModifiers:[]")
        self.assert_failed(args)
        args = no_draw_leaf_fixture()
        leaf = args[0]["children"][0]["children"][0]
        local = "RectT (10.00, 20.00) - [100.00 x 30.00]"
        leaf["attributes"].update({"FrameRect": local, "PaintRect without transform": local})
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[10.00,20.00],[110.00,50.00]"
        self.assert_failed(args)

    def test_matching_negative_raw_and_rs_dimensions_use_native_corner_envelope(self):
        args = mapping_fixture()
        leaf = args[0]["children"][0]["children"][0]
        local = "RectT (10.00, 20.00) - [-1.00 x -1.00]"
        leaf["attributes"].update({"FrameRect": local, "PaintRect without transform": local})
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[9.00,19.00],[10.00,20.00]"
        args[3]["render_tree"] = replace_render_text(args[3]["render_tree"],
            render_line(5, "CANVAS_NODE", 104, "10 20 100 30", "OtherModifiers:[]"),
            render_line(5, "CANVAS_NODE", 104, "10 20 -1 -1",
                        "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]"))
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        geometry = result["children"][0]["children"][0]["geometry"]
        self.assertEqual(geometry["render_state"], "not_rendered")
        self.assertFalse(geometry["rendered"])
        for key, value in {"x": 211.190138, "y": 16.656958, "width": 0.876682, "height": 0.876682}.items():
            self.assertAlmostEqual(geometry["screen_bounds"][key], value, places=5)
        for mutation in ("missing_draw", "draw_content", "rs_child"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(args)
                if mutation == "missing_draw":
                    changed[3]["render_tree"] = changed[3]["render_tree"].replace("hasDrawContent: 0, ", "")
                elif mutation == "draw_content":
                    changed[3]["render_tree"] = changed[3]["render_tree"].replace(
                        "hasDrawContent: 0", "hasDrawContent: 1")
                else:
                    changed[3]["render_tree"] += render_line(6, "CANVAS_NODE", 105, "0 0 1 1", "OtherModifiers:[]")
                self.assert_failed(changed)

    def test_negative_rs_dimensions_cannot_match_positive_raw_dimensions_within_tolerance(self):
        args = mapping_fixture()
        leaf = args[0]["children"][0]["children"][0]
        local = "RectT (10.00, 20.00) - [0.05 x 0.05]"
        leaf["attributes"].update({"FrameRect": local, "PaintRect without transform": local})
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[10.00,20.00],[10.05,20.05]"
        args[3]["render_tree"] = replace_render_text(args[3]["render_tree"],
            render_line(5, "CANVAS_NODE", 104, "10 20 100 30", "OtherModifiers:[]"),
            render_line(5, "CANVAS_NODE", 104, "10 20 -0.05 -0.05",
                        "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]"))
        self.assert_failed(args)

    def test_noncanonical_mixed_negative_dimensions_still_require_refusal(self):
        for width, height in ((-2, 11), (-0.5, 11), (11, -2), (11, -0.5)):
            with self.subTest(width=width, height=height):
                self.assert_failed(mixed_leaf_fixture(width, height))

    def test_mixed_sentinel_dimensions_preserve_signed_endpoints_and_empty_syntax_children(self):
        cases = [(-1, 11, False, (211.190138, 17.53364, 0.876682, 9.643502)),
                 (-1, 38, True, (211.190138, 17.53364, 0.876682, 33.313916)),
                 (11, -1, False, (212.06682, 16.656958, 9.643502, 0.876682)),
                 (38, -1, True, (212.06682, 16.656958, 33.313916, 0.876682))]
        for width, height, syntax, expected in cases:
            with self.subTest(width=width, height=height, syntax=syntax):
                args = mixed_leaf_fixture(width, height, syntax)
                before = copy.deepcopy(args)
                result = normalize_arkui(*args)
                self.assertEqual(result["coordinate_validation"]["status"], "verified",
                                 result["coordinate_validation"]["issues"])
                node = result["children"][0]["children"][0]
                geometry = node["geometry"]
                self.assertEqual(geometry["render_state"], "not_rendered")
                self.assertFalse(geometry["rendered"])
                for key, value in zip(("x", "y", "width", "height"), expected):
                    self.assertAlmostEqual(geometry["screen_bounds"][key], value, places=5)
                self.assertEqual(len(node["children"]), 3 if syntax else 0)
                if syntax:
                    self.assertTrue(all(child["geometry"]["status"] == "not_applicable"
                                        for child in node["children"]))
                self.assertEqual(args, before)

    def test_mixed_geometry_uses_paint_rect_without_rewriting_different_frame_rect(self):
        args = mixed_leaf_fixture(-1, 38, True)
        leaf = args[0]["children"][0]["children"][0]
        leaf["attributes"]["FrameRect"] = "RectT (10.40, 20.00) - [-1.00 x 38.00]"
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        node = result["children"][0]["children"][0]
        self.assertEqual(node["attributes"]["FrameRect"], leaf["attributes"]["FrameRect"])
        self.assertAlmostEqual(node["geometry"]["screen_bounds"]["x"], 211.190138, places=5)

    def test_mixed_no_draw_requires_real_rs_leaf_flags_and_matching_bounds_and_frame(self):
        changes = [("hasDrawContent: 0, ", ""), ("hasChildrenDrawContent: 0, ", ""),
                   ("hasDrawContent: 0", "hasDrawContent: 1"),
                   ("hasChildrenDrawContent: 0", "hasChildrenDrawContent: 1"),
                   ("hasDrawContent: 0,", "hasDrawContent: 0, hasDrawContent: 1,"),
                   ("hasChildrenDrawContent: 0,", "hasChildrenDrawContent: 0, hasChildrenDrawContent: 0,"),
                   ("Bounds[10 20 -1 11]", "Bounds[10 20 1 11]"),
                   ("Frame[10 20 -1 11]", "Frame[10 20 1 11]"),
                   ("Frame[10 20 -1 11]", "Frame[10 20 -1 11.2]"),
                   ("Bounds[10 20 -1 11]", "Bounds[10 20 -1 11.2]")]
        for original, replacement in changes:
            with self.subTest(replacement=replacement):
                args = mixed_leaf_fixture(structural_children=True)
                args[3]["render_tree"] = args[3]["render_tree"].replace(original, replacement, 1)
                self.assert_failed(args)
        args = mixed_leaf_fixture(structural_children=True)
        args[3]["render_tree"] += render_line(6, "CANVAS_NODE", 105, "0 0 1 1", "OtherModifiers:[]")
        self.assert_failed(args)

    def test_mixed_dimensions_keep_native_endpoint_order_and_independent_projection_checks(self):
        for bounds in ("[10.00,20.00],[9.00,31.00]", "[10.00,20.00],[11.00,31.00]"):
            with self.subTest(bounds=bounds):
                args = mixed_leaf_fixture()
                args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = bounds
                self.assert_failed(args)

    def test_mixed_dimensions_keep_signed_ancestor_scale_pivot_and_translation(self):
        args = transformed_mixed_leaf_fixture()
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        geometry = result["children"][0]["children"][0]["geometry"]
        expected = {"x": 152.890785, "y": 57.915804625, "width": 3.506728, "height": 2.4108755}
        for key, value in expected.items():
            self.assertAlmostEqual(geometry["screen_bounds"][key], value, places=5)
        for original, replacement in (("Scale:[x:2 y:0.5], Pivot:[x:0.25", "Scale:[x:-2 y:0.5], Pivot:[x:0.25"),
                                      ("Pivot:[x:0.25 y:0.75]", "Pivot:[x:0.5 y:0.75]"),
                                      ("Translate:[x:3 y:-2]", "Translate:[x:4 y:-2]")):
            changed = copy.deepcopy(args)
            changed[3]["render_tree"] = changed[3]["render_tree"].replace(original, replacement)
            self.assert_failed(changed)

    def test_mixed_no_draw_does_not_bypass_unsupported_local_or_ancestor_modifiers(self):
        for modifier in ("Skew:[x:1 y:0]", "Rotation:[1]", "CustomClipPath:[path]"):
            with self.subTest(modifier=modifier):
                args = mixed_leaf_fixture()
                args[3]["render_tree"] = replace_render_text(args[3]["render_tree"],
                    "Frame[10 20 -1 11], OtherModifiers:[]", f"Frame[10 20 -1 11], OtherModifiers:[{modifier}]")
                self.assert_failed(args)
                args = mixed_leaf_fixture()
                args[3]["render_tree"] = args[3]["render_tree"].replace(
                    "CANVAS_NODE[103], Properties: Bounds[0 0 2048 2230] Frame[0 0 2048 2230], OtherModifiers:[]",
                    "CANVAS_NODE[103], Properties: Bounds[0 0 2048 2230] Frame[0 0 2048 2230], "
                    f"OtherModifiers:[{modifier}]")
                self.assert_failed(args)

    def test_mixed_dimensions_reject_own_negative_clip_but_preserve_a_proven_positive_parent_clip(self):
        for clip in ("ClipToBounds[True], OtherModifiers:[ClipToBounds:[true]]",
                     "ClipToFrame[True], OtherModifiers:[ClipToFrame:[true]]",
                     "OtherModifiers:[CustomClipToFrame:[x:0 y:0 z:1 w:11]]"):
            with self.subTest(clip=clip):
                args = mixed_leaf_fixture()
                args[3]["render_tree"] = replace_render_text(args[3]["render_tree"],
                    "Frame[10 20 -1 11], OtherModifiers:[]", "Frame[10 20 -1 11], " + clip)
                self.assert_failed(args)
        args = mixed_leaf_fixture()
        parent = args[0]["children"][0]
        local = "RectT (0.00, 0.00) - [20.00 x 25.00]"
        parent["attributes"].update({"FrameRect": local, "PaintRect without transform": local})
        args[3]["arkui_geometry"]["$children"][0]["$rect"] = "[0.00,0.00],[20.00,25.00]"
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "CANVAS_NODE[103], Properties: Bounds[0 0 2048 2230] Frame[0 0 2048 2230], OtherModifiers:[]",
            "CANVAS_NODE[103], Properties: Bounds[0 0 20 25] Frame[0 0 20 25], "
            "ClipToBounds[True], OtherModifiers:[ClipToBounds:[true]]")
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        geometry = result["children"][0]["children"][0]["geometry"]
        self.assertAlmostEqual(geometry["rectangular_clip_bounds"]["height"], 4.38341, places=5)
        self.assertTrue(any(item.get("render_node_id") == "103" for item in geometry["clip_chain"]))

    def test_scene_evidence_is_required_without_wms_mapping_substitution(self):
        args = mapping_fixture()
        del args[3]["scene"]
        self.assert_failed(args, "scene")
        for field, code in (("path", "missing_scene_coordinate_evidence"), ("native", "native"),
                            ("source", "source")):
            with self.subTest(field=field):
                args = mapping_fixture()
                del args[3]["scene"][field]
                self.assert_failed(args, code)

    def test_session_component_and_accessibility_identity_must_match(self):
        for field, value in (("compid", "session107"), ("AccessibilityId", "7819")):
            with self.subTest(field=field):
                args = mapping_fixture()
                args[3]["scene"]["path"][-1]["attributes"][field] = value
                self.assert_failed(args, "identity")

    def test_native_scene_parent_projection_conflict_remains_rejected(self):
        args = mapping_fixture()
        args[3]["scene"]["native"]["$children"][0]["$rect"] = "[204.30,0.00],[1999.74,1955.00]"
        self.assert_failed(args, "scene_native_projection_mismatch")

    def test_scene_render_surface_must_be_actual_application_ancestor(self):
        args = mapping_fixture()
        args[3]["scene"]["path"][-1]["raw"] = (
            "SurfaceNode[19649475381949] child[] lazyLoad[0] nodeState[ACTIVE]")
        self.assert_failed(args, "render")

    def test_matching_surface_cannot_name_another_window(self):
        args = mapping_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace("nodeName: [session106]", "nodeName: [session107]")
        self.assert_failed(args, "session")

    def test_wms_integer_outer_envelope_must_agree_with_exact_projection(self):
        for field, code in (("x", "scene_wms_outer_origin_mismatch"), ("scale_x", "scene_wms_outer_scale_mismatch"),
                            ("width", "scene_window_size_mismatch")):
            with self.subTest(field=field):
                args = mapping_fixture()
                if field in ("x", "width"):
                    args[1]["bounding_box"][field] += 1
                else:
                    args[1]["transform"]["scale_x"] = 0.88
                self.assert_failed(args, code)

    def test_moved_window_uses_exact_scene_bounds_when_wms_retains_the_previous_envelope_scale(self):
        args = moved_window_fixture()
        before = copy.deepcopy(args)
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        bounds = result["geometry"]["screen_bounds"]
        for key, expected in {"x": 300, "y": 300, "width": 1470.885888, "height": 1601.59938}.items():
            self.assertAlmostEqual(bounds[key], expected, places=5)
        self.assertEqual(args[1]["bounding_box"]["width"] * args[1]["transform"]["scale_x"], 1472)
        self.assertEqual(args, before)

    def test_wms_retention_comparison_uses_the_printed_interval_and_rejects_outside_the_official_threshold(self):
        args = moved_window_fixture()
        args[1]["transform"]["scale_x"] = 0.719262
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        args[1]["transform"]["scale_x"] = 0.719263
        self.assert_failed(args, "scene_wms_outer_scale_mismatch:x")

    def test_official_float_near_equal_is_inclusive_and_rejects_the_next_float(self):
        threshold = float32(0.001)
        self.assertTrue(float32_interval_near_equal((threshold, threshold), 0.0))
        beyond = float32(0.0010000002)
        self.assertGreater(beyond, threshold)
        self.assertFalse(float32_interval_near_equal((beyond, beyond), 0.0))

    def test_wms_scale_printing_uses_six_significant_digits_and_eight_character_prefix(self):
        lower, upper = wms_scale_interval(0.71875)
        self.assertAlmostEqual(lower, 0.7187495, places=10)
        self.assertAlmostEqual(upper, 0.7187505, places=10)
        for original in (0.7187503, 0.01234569, 0.00010042, 1.000004):
            with self.subTest(original=original):
                stored = float32(original)
                printed = float(format(stored, ".6g")[:8])
                lower, upper = wms_scale_interval(printed)
                self.assertLessEqual(lower, stored)
                self.assertLessEqual(stored, upper)

    def test_display_identity_and_canvas_are_independently_verified(self):
        for scope in ("source", "screen", "canvas"):
            with self.subTest(scope=scope):
                args = mapping_fixture()
                if scope == "source":
                    args[3]["scene"]["source"]["display_id"] = 1
                elif scope == "screen":
                    args[3]["render_tree"] = args[3]["render_tree"].replace("screenId[0]", "screenId[1]")
                else:
                    args[2]["width"] = 3119
                self.assert_failed(args, "display" if scope != "canvas" else "canvas")

    def test_outer_render_clip_origin_conflict_still_fails_at_exact_origin(self):
        args = mapping_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace("203.3 0 2048 2230", "204.3 0 2048 2230")
        self.assert_failed(args, "scene_render_bounds_mismatch")

    def test_nonzero_outer_surface_rotation_is_not_exempted(self):
        args = mapping_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "OtherModifiers:[Scale:", "OtherModifiers:[Rotation:[1], Scale:")
        self.assert_failed(args, "unsupported_render_transform:Rotation")

    def test_system_session_surface_still_requires_its_own_rs_projection(self):
        for old, new in (("Scale:[x:0.876682 y:0.876682]", "Scale:[x:0.5 y:0.5]"),
                         ("203.3 0 2048 2230", "204.3 0 2048 2230")):
            with self.subTest(modification=new):
                args = mapping_fixture(system=True)
                args[3]["render_tree"] = args[3]["render_tree"].replace(old, new)
                self.assert_failed(args, "scene_")

    def test_physical_screen_transform_cannot_be_ignored_by_scene_mapping(self):
        for modifier in ("Scale:[x:2 y:1]", "Translate:[x:10 y:0]"):
            with self.subTest(modifier=modifier):
                args = mapping_fixture()
                args[3]["render_tree"] = args[3]["render_tree"].replace(
                    "screenId[0], OtherModifiers:[]", f"screenId[0], OtherModifiers:[{modifier}]")
                self.assert_failed(args, "screen")

    def test_scb_native_session_component_identity_cannot_conflict_with_raw_path(self):
        args = mapping_fixture()
        args[3]["scene"]["native"]["$children"][0]["compid"] = "session107"
        self.assert_failed(args, "scene_")

    def test_render_frame_ancestor_order_must_match_the_native_scb_parent_chain(self):
        result = normalize_arkui(*ancestor_mapping_fixture())
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        self.assertAlmostEqual(result["geometry"]["screen_bounds"]["x"], 101.65, places=5)
        self.assert_failed(ancestor_mapping_fixture(reversed_render_order=True), "scene_")

    def test_system_coordinate_space_requires_the_actual_session_root(self):
        args = mapping_fixture()
        args[1]["is_system"] = True
        self.assert_failed(args, "scene_")

    def test_reflected_scene_keeps_axis_direction_and_rejects_conflicting_rs_scale(self):
        args = mapping_fixture()
        args[3]["scene"]["path"][-1]["attributes"]["scale(x,y)"] = "-0.876682,0.876682"
        args[3]["scene"]["native"]["$children"][0]["$rect"] = "[-1592.14,0.00],[203.30,1955.00]"
        args[1]["bounding_box"]["x"] = -1593
        args[1]["transform"]["scale_x"] = 1797 / 2048
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "Scale:[x:0.876682 y:0.876682]", "Scale:[x:-0.876682 y:0.876682]")
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        text = result["children"][0]["children"][0]["geometry"]
        self.assertAlmostEqual(text["screen_bounds"]["x"], 106.86498, places=5)
        clip = next(item for item in text["clip_chain"] if item.get("render_node_id") == "19649475381948")
        self.assertEqual(clip["local_axis_directions"], {"x": -1, "y": 1})
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "Scale:[x:-0.876682 y:0.876682]", "Scale:[x:0.876682 y:0.876682]")
        self.assert_failed(args, "scene_render_vector_mismatch:Scale")

    def test_scene_ancestor_render_identity_cannot_be_reused(self):
        args = ancestor_mapping_fixture()
        scene = args[3]["scene"]
        group = copy.deepcopy(scene["path"][1])
        group["attributes"]["ID"] = "12"
        scene["path"].insert(2, group)
        native_group = scene["native"]["$children"][0]
        target = native_group["$children"][0]
        target["$rect"] = "[50.83,0.00],[499.69,488.75]"
        native_group["$children"] = [{"$ID": 12, "$type": "Column", "$rect": "[0.00,0.00],[780.00,520.00]",
                                       "$children": [target]}]
        args[1]["bounding_box"]["x"] = 50
        args[1]["transform"].update(scale_x=450 / 2048, scale_y=489 / 2230)
        self.assert_failed(args, "ambiguous_scene_render_mapping")

    def test_transformed_scene_frame_ancestor_cannot_be_missing_from_rs_chain(self):
        args = ancestor_mapping_fixture()
        rows = args[3]["render_tree"].splitlines(keepends=True)
        args[3]["render_tree"] = "".join(rows[:2]) + "".join(row[2:] for row in rows[3:])
        self.assert_failed(args, "scene_")

    def test_omitted_identity_scene_ancestor_requires_explicit_lazy_or_detached_evidence(self):
        for state in ("lazy", "detached"):
            with self.subTest(state=state):
                args = omitted_ancestor_fixture(state)
                result = normalize_arkui(*args)
                self.assertEqual(result["coordinate_validation"]["status"], "verified",
                                 result["coordinate_validation"]["issues"])
                bounds = result["geometry"]["screen_bounds"]
                self.assertAlmostEqual(bounds["x"], 203.3, places=5)
                self.assertAlmostEqual(bounds["width"], 1795.444736, places=5)

    def test_lazy_or_detached_evidence_cannot_exempt_a_transformed_scene_ancestor(self):
        for state in ("lazy", "detached"):
            with self.subTest(state=state):
                self.assert_failed(omitted_ancestor_fixture(state, transformed=True),
                                   "scene_render_frame_ancestor_missing:11")

    def test_detached_identity_scene_ancestor_does_not_use_unapplied_bounds(self):
        for bounds in ("1 0 3120 2080", "-inf -inf -inf -inf"):
            with self.subTest(bounds=bounds):
                args = omitted_ancestor_fixture("detached")
                args[3]["render_detached_tree"] = args[3]["render_detached_tree"].replace("0 0 3120 2080", bounds)
                result = normalize_arkui(*args)
                self.assertEqual(result["coordinate_validation"]["status"], "verified",
                                 result["coordinate_validation"]["issues"])
                self.assertAlmostEqual(result["geometry"]["screen_bounds"]["x"], 203.3, places=5)

    def test_detached_identity_scene_ancestor_rejects_conflicting_rs_scale(self):
        args = omitted_ancestor_fixture("detached")
        args[3]["render_detached_tree"] = args[3]["render_detached_tree"].replace(
            "Scale:[x:1 y:1]", "Scale:[x:2 y:1]")
        self.assert_failed(args, "scene_render_frame_ancestor_missing:11")

    def test_application_nodes_cannot_reuse_scene_session_or_ancestor_render_identity(self):
        for owner, render_id in (("root", "19649475381948"), ("leaf", "19649475381948"),
                                 ("leaf", "6962141986818")):
            with self.subTest(owner=owner, render_id=render_id):
                args = mapping_fixture()
                node = args[0] if owner == "root" else args[0]["children"][0]["children"][0]
                node["raw"] = f"CanvasNode[{render_id}] child[] lazyLoad[0] nodeState[ACTIVE]"
                self.assert_failed(args, "scene_")


if __name__ == "__main__":
    unittest.main()
