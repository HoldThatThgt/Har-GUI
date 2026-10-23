"""父 Surface 的尺寸不决定应用根尺寸，原始边界与实际裁剪分别保留。"""

import copy
import unittest

from har_capture.coordinates import normalize_arkui
from tests.test_window_mapping import component, render_line, replace_render_text


def raw_rectangle(x, y, width, height):
    """构造无正文的官方矩形文本。"""
    return f"RectT ({x:.2f}, {y:.2f}) - [{width:.2f} x {height:.2f}]"


def native_rectangle(x, y, width, height):
    """构造原生已投影的矩形端点。"""
    return f"[{x:.2f},{y:.2f}],[{x + width:.2f},{y + height:.2f}]"


def viewport_fixture(surface_size=(2090, 1394), application_size=(2091, 1394), transform=(1, 1, 0, 0)):
    """构造完整 Screen、会话、内层 Surface 与独立应用树，不复制采集正文。"""
    width, height = application_size
    surface_width, surface_height = surface_size
    sx, sy, tx, ty = transform
    screen = component(10, "Screen", raw_rectangle(0, 0, 3120, 2080))
    outer = component(9664, "WindowScene", raw_rectangle(546, 204, width, height),
                      {"AccessibilityId": "1260", "compid": "session126"})
    inner = component(9665, "WindowScene", raw_rectangle(0, 0, surface_width, surface_height), {
        "FrameRect": raw_rectangle(0, 0, surface_width + 0.4, surface_height - 0.4),
        "scale(x,y)": f"{sx},{sy}", "translate(x,y)": f"{tx},{ty}"})
    root = component(1, "root", raw_rectangle(0, 0, width, height))
    child = component(2, "Text", raw_rectangle(width - 11, 10, 20, 20))
    for node, kind, render_id in ((screen, "Display", 900), (outer, "Surface", 901),
                                   (inner, "Surface", 902), (root, "Canvas", 100), (child, "Canvas", 101)):
        node["raw"] = f"{kind}Node[{render_id}] child[] lazyLoad[0] nodeState[ACTIVE]"
    outer["children"], root["children"] = [inner], [child]
    projected_x = 546 + tx + min(0, surface_width * sx)
    projected_y = 204 + ty + min(0, surface_height * sy)
    native_inner = {"$ID": 9665, "$type": "WindowScene", "$rect": native_rectangle(
        projected_x, projected_y, abs(surface_width * sx), abs(surface_height * sy))}
    scene_native = {"$ID": 10, "$type": "Screen", "$rect": native_rectangle(0, 0, 3120, 2080), "$children": [
        {"$ID": 9664, "$type": "WindowScene", "compid": "session126",
         "$rect": native_rectangle(546, 204, width, height), "$children": [native_inner]}]}
    native = {"$ID": 1, "$type": "root", "$rect": native_rectangle(0, 0, width, height), "$children": [
        {"$ID": 2, "$type": "Text", "$rect": native_rectangle(width - 11, 10, 20, 20)}]}
    window = {"window_id": 126, "process_id": 2000, "display_id": 0, "ui_node_id": 1260,
              "is_system": False, "bounding_box": {"x": 546, "y": 204, "width": width, "height": height},
              "transform": {"offset_x": 0, "offset_y": 0, "scale_x": 1, "scale_y": 1,
                            "pivot_x": 0, "pivot_y": 0}}
    scene = {"path": [screen, outer], "native": scene_native,
             "source": {"process_id": 1000, "display_id": 0, "source_window_id": 9}}
    render = viewport_render_tree(surface_size, application_size, transform)
    evidence = {"arkui_geometry": native, "scene": scene, "render_tree": render,
                "render_detached_tree": "-- Render Node Not On Tree\n"}
    return root, window, {"width": 3120, "height": 2080}, evidence


def viewport_render_tree(surface_size, application_size, transform):
    """保留每层真实 RS 父关系，内层生效 Frame 等于其 Bounds。"""
    width, height = application_size
    surface_width, surface_height = surface_size
    sx, sy, tx, ty = transform
    rows = [render_line(0, "SCREEN_NODE", 899, "0 0 3120 2080", "screenId[0], OtherModifiers:[]"),
            render_line(1, "LOGICAL_DISPLAY_NODE", 900, "0 0 3120 2080", "OtherModifiers:[]"),
            render_line(2, "SURFACE_NODE", 901, f"546 204 {width} {height}",
                        "nodeName: [session126], ClipToFrame[True], OtherModifiers:[ClipToFrame:[true]]"),
            render_line(3, "SURFACE_NODE", 902, f"0 0 {surface_width} {surface_height}",
                        f"Scale[{sx} {sy}] Pivot[0 0] Translate[{tx} {ty}], ClipToFrame[True], "
                        f"OtherModifiers:[Scale:[x:{sx} y:{sy}], Pivot:[x:0 y:0], "
                        f"Translate:[x:{tx} y:{ty}], ClipToFrame:[true]]"),
            render_line(4, "ROOT_NODE", 100, f"0 0 {width} {height}", "OtherModifiers:[]"),
            render_line(5, "CANVAS_NODE", 101, f"{width - 11} 10 20 20", "OtherModifiers:[]")]
    return "".join(rows)


def empty_subtree_fixture():
    """匿名复现有真实父偏移的零尺寸 NodeContainer 与 CustomFrameNode 子树。"""
    args = viewport_fixture()
    modal = component(2, "ContainerModal", raw_rectangle(0, 169, 2091, 1225))
    parent = component(1363, "NodeContainer", raw_rectangle(0, 0, 0, 0),
                       {"Description": "Children", "LastParentId": "2"})
    child = component(1364, "CustomFrameNode", raw_rectangle(0, 0, 0, 0),
                      {"Description": "Children", "LastParentId": "1363"})
    for node in (modal, parent, child):
        render_id = 101 if node is modal else int(node["attributes"]["ID"])
        node["raw"] = f"CanvasNode[{render_id}] child[] lazyLoad[0] nodeState[ACTIVE]"
    args[0]["children"], modal["children"], parent["children"] = [modal], [parent], [child]
    native_child = {"$ID": 1364, "$type": "CustomFrameNode", "$rect": native_rectangle(0, 169, 0, 0)}
    native_parent = {"$ID": 1363, "$type": "NodeContainer", "$rect": native_rectangle(0, 169, 0, 0),
                     "$children": [native_child]}
    args[3]["arkui_geometry"]["$children"] = [{"$ID": 2, "$type": "ContainerModal",
        "$rect": native_rectangle(0, 169, 2091, 1225), "$children": [native_parent]}]
    rows = args[3]["render_tree"].splitlines(keepends=True)[:5]
    rows.append(render_line(5, "CANVAS_NODE", 101, "0 169 2091 1225", "OtherModifiers:[]"))
    for depth, render_id in ((6, 1363), (7, 1364)):
        children = ", sortedChildren: 1(1364 )" if render_id == 1363 else ""
        rows.append(render_line(depth, "CANVAS_NODE", render_id, "-inf -inf -inf -inf",
                                "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]" + children))
    args[3]["render_tree"] = "".join(rows)
    return args


def omitted_empty_ancestor_fixture(proof="detached"):
    """为真实树中被省略的恒等 Stack 提供明确挂载状态，不把任意祖先视为等价父。"""
    args = empty_subtree_fixture()
    modal = args[0]["children"][0]
    parent = modal["children"][0]
    wrapper = component(1360, "Stack", raw_rectangle(0, 0, 2091, 1141))
    wrapper["raw"] = "CanvasNode[1360] child[] lazyLoad[0] nodeState[ACTIVE]"
    parent["attributes"]["LastParentId"] = "1360"
    modal["children"], wrapper["children"] = [wrapper], [parent]
    native_modal = args[3]["arkui_geometry"]["$children"][0]
    native_parent = native_modal["$children"][0]
    native_modal["$children"] = [{"$ID": 1360, "$type": "Stack",
        "$rect": native_rectangle(0, 169, 2091, 1141), "$children": [native_parent]}]
    row = render_line(0, "CANVAS_NODE", 1360, "0 0 2091 1141", "OtherModifiers:[]")
    if proof == "detached":
        args[3]["render_detached_tree"] += row
    elif proof == "lazy":
        wrapper["raw"] = wrapper["raw"].replace("lazyLoad[0] nodeState[ACTIVE]", "lazyLoad[1] nodeState[LAZY_LOAD]")
    elif proof == "active":
        rows = args[3]["render_tree"].splitlines(keepends=True)
        rows.insert(6, "            " + row)
        args[3]["render_tree"] = "".join(rows)
    elif proof != "missing":
        raise AssertionError("Unknown anonymous ancestor proof fixture")
    return args


class ApplicationViewportTests(unittest.TestCase):
    def verified(self, args):
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        return result

    def assert_failed(self, args, code):
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        issues = result["coordinate_validation"]["issues"]
        self.assertTrue(any(code in issue["code"] for issue in issues), issues)
        return result

    def test_actual_unequal_surface_preserves_raw_root_and_uses_real_rs_frame_clip(self):
        args = viewport_fixture()
        original = copy.deepcopy(args)
        result = self.verified(args)
        root, child = result["geometry"], result["children"][0]["geometry"]
        self.assertEqual(root["screen_bounds"], {"x": 546, "y": 204, "width": 2091, "height": 1394})
        self.assertEqual(root["rectangular_clip_bounds"], {"x": 546, "y": 204, "width": 2090, "height": 1394})
        self.assertEqual(child["screen_bounds"], {"x": 2626, "y": 214, "width": 20, "height": 20})
        self.assertEqual(child["rectangular_clip_bounds"], {"x": 2626, "y": 214, "width": 10, "height": 20})
        clip = next(item for item in root["clip_chain"] if item.get("render_node_id") == "902")
        self.assertEqual(clip["mode"], "frame")
        self.assertEqual(clip["bounds"], root["rectangular_clip_bounds"])
        self.assertEqual(root["source"]["window_mapping"]["viewport_node_id"], "9665")
        self.assertEqual(root["source"]["window_mapping"]["viewport_render_node_id"], "902")
        self.assertEqual(args, original)

    def test_large_parent_child_size_difference_is_not_an_expanded_rounding_threshold(self):
        result = self.verified(viewport_fixture((120, 100), (200, 150)))
        self.assertEqual(result["geometry"]["screen_bounds"], {"x": 546, "y": 204, "width": 200, "height": 150})
        self.assertEqual(result["geometry"]["rectangular_clip_bounds"],
                         {"x": 546, "y": 204, "width": 120, "height": 100})

    def test_parent_larger_than_application_preserves_its_clip_source_and_outer_intersection(self):
        result = self.verified(viewport_fixture((300, 250), (200, 150)))
        geometry = result["geometry"]
        self.assertEqual(geometry["screen_bounds"], {"x": 546, "y": 204, "width": 200, "height": 150})
        self.assertEqual(geometry["rectangular_clip_bounds"], geometry["screen_bounds"])
        clip = next(item for item in geometry["clip_chain"] if item.get("render_node_id") == "902")
        self.assertEqual(clip["bounds"], {"x": 546, "y": 204, "width": 300, "height": 250})
        self.assertEqual(clip["mode"], "frame")

    def test_unequal_parent_scale_and_translation_compose_without_resizing_native_root(self):
        args = viewport_fixture(transform=(0.5, 0.75, 10, 20))
        result = self.verified(args)
        self.assertEqual(result["geometry"]["screen_bounds"],
                         {"x": 556, "y": 224, "width": 1045.5, "height": 1045.5})
        self.assertEqual(result["geometry"]["rectangular_clip_bounds"],
                         {"x": 556, "y": 224, "width": 1045, "height": 1045.5})
        self.assertEqual(result["children"][0]["geometry"]["screen_bounds"],
                         {"x": 1596, "y": 231.5, "width": 10, "height": 15})
        self.assertEqual(result["children"][0]["geometry"]["rectangular_clip_bounds"],
                         {"x": 1596, "y": 231.5, "width": 5, "height": 15})
        self.assertEqual(args[3]["arkui_geometry"]["$rect"], "[0.00,0.00],[2091.00,1394.00]")

    def test_reflected_unequal_parent_preserves_signed_axes_and_independent_clipping(self):
        result = self.verified(viewport_fixture(transform=(-1, 1, 2090, 0)))
        root, child = result["geometry"], result["children"][0]["geometry"]
        self.assertEqual(root["screen_bounds"], {"x": 545, "y": 204, "width": 2091, "height": 1394})
        self.assertEqual(root["rectangular_clip_bounds"], {"x": 546, "y": 204, "width": 2090, "height": 1394})
        self.assertEqual(child["screen_bounds"], {"x": 536, "y": 214, "width": 20, "height": 20})
        self.assertEqual(child["rectangular_clip_bounds"], {"x": 546, "y": 214, "width": 10, "height": 20})
        clip = next(item for item in root["clip_chain"] if item.get("render_node_id") == "902")
        self.assertLess(clip["local_to_screen"][0], 0)
        self.assertGreater(clip["local_to_screen"][3], 0)

    def test_session_identity_and_application_render_ownership_remain_strict(self):
        args = viewport_fixture()
        args[3]["scene"]["path"][-1]["attributes"]["compid"] = "session127"
        self.assert_failed(args, "scene_session_identity_mismatch")
        args = viewport_fixture()
        args[0]["raw"] = "CanvasNode[902] child[] lazyLoad[0] nodeState[ACTIVE]"
        self.assert_failed(args, "scene_application_render_identity_overlap")

    def test_native_scb_parent_identity_and_local_projection_remain_strict(self):
        args = viewport_fixture()
        scene = args[3]["scene"]["native"]
        scene["$children"].append(scene["$children"][0]["$children"].pop())
        self.assert_failed(args, "scene_native_identity_or_parent_mismatch")
        args = viewport_fixture()
        args[3]["scene"]["native"]["$children"][0]["$children"][0]["$rect"] = "[547,204],[2637,1598]"
        self.assert_failed(args, "scene_native_projection_mismatch")

    def test_application_native_projection_cannot_be_replaced_by_parent_dimensions(self):
        args = viewport_fixture()
        args[3]["arkui_geometry"]["$children"][0]["$rect"] = "[2081,10],[2101,30]"
        self.assert_failed(args, "native_geometry_local_projection_mismatch")

    def test_root_must_still_have_the_target_session_in_its_actual_render_ancestor_chain(self):
        args = viewport_fixture()
        rows = args[3]["render_tree"].splitlines(keepends=True)
        rows[4], rows[5] = rows[4][4:], rows[5][4:]
        args[3]["render_tree"] = "".join(rows)
        self.assert_failed(args, "scene_render_window_mismatch")

    def test_parent_bounds_and_transforms_still_require_their_independent_raw_evidence(self):
        args = viewport_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace("0 0 2090 1394", "0 0 2089 1394")
        self.assert_failed(args, "scene_render_bounds_mismatch")
        args = viewport_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace("Scale:[x:1 y:1]", "Scale:[x:0.5 y:1]")
        self.assert_failed(args, "scene_render_vector_mismatch:Scale")

    def test_unknown_parent_transforms_and_clips_are_not_exempted_by_unequal_sizes(self):
        for modifier, code in (("Rotation:[5]", "scene_render_scalar_mismatch:Rotation"),
                               ("CustomClip:[1 2 3 4]", "unsupported_render_modifier:CustomClip")):
            with self.subTest(modifier=modifier):
                args = viewport_fixture()
                args[3]["render_tree"] = args[3]["render_tree"].replace(
                    "Scale:[x:1 y:1]", "Scale:[x:1 y:1], " + modifier)
                self.assert_failed(args, code)

    def test_distinct_rs_frame_clip_is_still_unsupported_independently_of_scb_frame_rect(self):
        args = viewport_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace("Frame[0 0 2090 1394]", "Frame[0 0 2090.4 1393.6]")
        self.assert_failed(args, "unsupported_distinct_frame_clip")

    def test_application_root_native_size_must_still_match_its_wms_logical_window(self):
        args = viewport_fixture()
        for key in ("FrameRect", "PaintRect without transform"):
            args[0]["attributes"][key] = raw_rectangle(0, 0, 2090, 1394)
        args[3]["arkui_geometry"]["$rect"] = native_rectangle(0, 0, 2090, 1394)
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "ROOT_NODE[100], Properties: Bounds[0 0 2091 1394] Frame[0 0 2091 1394]",
            "ROOT_NODE[100], Properties: Bounds[0 0 2090 1394] Frame[0 0 2090 1394]")
        self.assert_failed(args, "native_root_window_size_mismatch")

    def test_zero_area_nonfinite_nonleaf_needs_more_than_its_own_no_draw_flags(self):
        args = viewport_fixture()
        node = args[0]["children"][0]
        zero = raw_rectangle(2080, 10, 0, 0)
        node["attributes"].update({"type": "Column", "FrameRect": zero, "PaintRect without transform": zero})
        child = component(3, "Text", raw_rectangle(0, 0, 1, 1))
        child["raw"] = "CanvasNode[102] child[] lazyLoad[0] nodeState[ACTIVE]"
        node["children"] = [child]
        native = args[3]["arkui_geometry"]["$children"][0]
        native.update({"$type": "Column", "$rect": native_rectangle(2080, 10, 0, 0), "$children": [
            {"$ID": 3, "$type": "Text", "$rect": native_rectangle(2080, 10, 1, 1)}]})
        args[3]["render_tree"] = replace_render_text(args[3]["render_tree"],
            render_line(5, "CANVAS_NODE", 101, "2080 10 20 20", "OtherModifiers:[]"),
            render_line(5, "CANVAS_NODE", 101, "-inf -inf -inf -inf",
                        "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]"))
        args[3]["render_tree"] += render_line(6, "CANVAS_NODE", 102, "0 0 1 1", "OtherModifiers:[]")
        self.assert_failed(args, "empty_render_subtree_nonzero_geometry:102")

    def test_complete_empty_no_draw_subtree_retains_nodes_and_native_nonzero_parent_origin(self):
        args = empty_subtree_fixture()
        original = copy.deepcopy(args)
        result = self.verified(args)
        validation = result["coordinate_validation"]
        self.assertEqual(validation["frame_nodes"], 4)
        self.assertEqual(validation["verified_frame_nodes"], 4)
        parent = result["children"][0]["children"][0]
        child = parent["children"][0]
        self.assertEqual([parent["attributes"]["ID"], child["attributes"]["ID"]], ["1363", "1364"])
        for node, proof in ((parent, {"1363", "1364"}), (child, {"1364"})):
            geometry = node["geometry"]
            self.assertEqual(geometry["screen_bounds"], {"x": 546, "y": 373, "width": 0, "height": 0})
            self.assertEqual(geometry["native_window_rect"], "[0.00,169.00],[0.00,169.00]")
            self.assertEqual(geometry["status"], "verified")
            self.assertEqual(geometry["render_state"], "not_rendered")
            self.assertFalse(geometry["rendered"])
            self.assertEqual(geometry["render_state_source"], "RenderService.no_draw_content")
            self.assertTrue(geometry["source"]["native_rect_cross_checked"])
            self.assertEqual(set(geometry["source"]["no_draw_render_node_ids"]), proof)
        self.assertEqual(args, original)

    def test_empty_no_draw_subtree_does_not_require_an_optional_modifiers_dump_field(self):
        args = empty_subtree_fixture()
        args[3]["render_tree"] = replace_render_text(args[3]["render_tree"], "OtherModifiers:[]", "")
        self.verified(args)

    def test_positive_native_or_raw_area_cannot_claim_an_empty_nonfinite_subtree(self):
        for target in ("raw", "native"):
            args = empty_subtree_fixture()
            if target == "raw":
                node = args[0]["children"][0]["children"][0]
                for key in ("FrameRect", "PaintRect without transform"):
                    node["attributes"][key] = raw_rectangle(0, 0, 1, 1)
            else:
                args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[0,169],[1,170]"
            with self.subTest(target=target):
                self.assert_failed(args, "")

    def test_empty_parent_cannot_admit_a_consistent_positive_area_no_draw_descendant(self):
        args = empty_subtree_fixture()
        child = args[0]["children"][0]["children"][0]["children"][0]
        for key in ("FrameRect", "PaintRect without transform"):
            child["attributes"][key] = raw_rectangle(0, 0, 1, 1)
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$children"][0]["$rect"] = "[0,169],[1,170]"
        rows = args[3]["render_tree"].splitlines(keepends=True)
        rows[-1] = rows[-1].replace("Bounds[-inf -inf -inf -inf] Frame[-inf -inf -inf -inf]",
                                    "Bounds[0 0 1 1] Frame[0 0 1 1]")
        args[3]["render_tree"] = "".join(rows)
        self.assert_failed(args, "empty_render_subtree_nonzero_geometry")

    def test_empty_parent_does_not_override_a_drawing_or_unproven_descendant(self):
        changes = (("hasDrawContent: 0,", "hasDrawContent: 1,"),
                   ("hasChildrenDrawContent: 0,", "hasChildrenDrawContent: 1,"),
                   ("hasDrawContent: 0,", ""), ("hasChildrenDrawContent: 0,", ""))
        for old, new in changes:
            args = empty_subtree_fixture()
            rows = args[3]["render_tree"].splitlines(keepends=True)
            rows[-1] = rows[-1].replace(old, new)
            args[3]["render_tree"] = "".join(rows)
            with self.subTest(mutation=(old, new)):
                self.assert_failed(args, "")

    def test_duplicate_or_conflicting_draw_flags_are_not_complete_subtree_evidence(self):
        for depth in (-1, -2):
            for flag in ("hasDrawContent", "hasChildrenDrawContent"):
                for duplicate in (0, 1):
                    args = empty_subtree_fixture()
                    rows = args[3]["render_tree"].splitlines(keepends=True)
                    rows[depth] = rows[depth].replace(f"{flag}: 0,", f"{flag}: 0, {flag}: {duplicate},")
                    args[3]["render_tree"] = "".join(rows)
                    with self.subTest(depth=depth, flag=flag, duplicate=duplicate):
                        self.assert_failed(args, "")

    def test_unknown_render_descendant_cannot_be_hidden_under_no_draw_subtree_flags(self):
        args = empty_subtree_fixture()
        rows = args[3]["render_tree"].splitlines(keepends=True)
        rows[-1] = rows[-1].rstrip("\n") + ", sortedChildren: 1(999 )\n"
        args[3]["render_tree"] = "".join(rows)
        args[3]["render_tree"] += render_line(8, "CANVAS_NODE", 999, "-inf -inf -inf -inf",
            "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]")
        self.assert_failed(args, "empty_render_subtree_membership_mismatch")

    def test_empty_subtree_actual_render_parent_must_match_the_native_frame_parent(self):
        args = empty_subtree_fixture()
        rows = args[3]["render_tree"].splitlines(keepends=True)
        rows[-1] = rows[-1][2:]
        rows[-2] = rows[-2].replace("sortedChildren: 1(1364 )", "sortedChildren: 0()")
        args[3]["render_tree"] = "".join(rows)
        self.assert_failed(args, "")

    def test_empty_subtree_native_identity_and_parent_cannot_conflict_with_the_raw_tree(self):
        for corruption in ("type", "parent"):
            args = empty_subtree_fixture()
            modal = args[3]["arkui_geometry"]["$children"][0]
            parent = modal["$children"][0]
            if corruption == "type":
                parent["$type"] = "Column"
            else:
                modal["$children"].append(parent["$children"].pop())
            with self.subTest(corruption=corruption):
                self.assert_failed(args, "")

    def test_zero_size_child_native_origin_is_still_checked_against_its_full_local_parent_projection(self):
        args = empty_subtree_fixture()
        child = args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$children"][0]
        child["$rect"] = "[0.00,170.00],[0.00,170.00]"
        result = self.assert_failed(args, "native_geometry_local_projection_mismatch")
        self.assertEqual(result["coordinate_validation"]["frame_nodes"], 4)
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 3)
        raw_child = result["children"][0]["children"][0]["children"][0]
        self.assertEqual(raw_child["attributes"]["ID"], "1364")
        self.assertEqual(raw_child["geometry"]["status"], "failed")

    def test_missing_wrong_or_duplicate_declared_render_child_counts_are_not_complete_subtree_proof(self):
        for replacement in ("", "sortedChildren: 2(1364 999 )", "sortedChildren: 1(1364 ), sortedChildren: 1(1364 )"):
            args = empty_subtree_fixture()
            args[3]["render_tree"] = args[3]["render_tree"].replace("sortedChildren: 1(1364 )", replacement)
            with self.subTest(replacement=replacement):
                self.assert_failed(args, "empty_render_subtree_children_unverified")

    def test_empty_subtree_does_not_allow_unsupported_transforms_or_enabled_clips(self):
        for modifier in ("Rotation:[5]", "CustomClip:[0 0 0 0]", "Scale:[x:2 y:1]", "ClipToBounds:[true]"):
            args = empty_subtree_fixture()
            rows = args[3]["render_tree"].splitlines(keepends=True)
            rows[-1] = rows[-1].replace("OtherModifiers:[]", f"OtherModifiers:[{modifier}]")
            if modifier == "ClipToBounds:[true]":
                rows[-1] = rows[-1].replace("OtherModifiers:", "ClipToBounds[True], OtherModifiers:")
            args[3]["render_tree"] = "".join(rows)
            with self.subTest(modifier=modifier):
                self.assert_failed(args, "")

    def test_cached_skip_markers_never_replace_missing_dual_draw_proof(self):
        args = empty_subtree_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "hasDrawContent: 0, hasChildrenDrawContent: 0,", "IsSubTreeSkipped: true,")
        self.assert_failed(args, "")

    def test_explicit_detached_identity_stack_can_be_omitted_from_the_empty_subtree_render_parent_chain(self):
        args = omitted_empty_ancestor_fixture()
        original = copy.deepcopy(args)
        result = self.verified(args)
        validation = result["coordinate_validation"]
        self.assertEqual(validation["frame_nodes"], 5)
        self.assertEqual(validation["verified_frame_nodes"], 5)
        wrapper = result["children"][0]["children"][0]
        parent, child = wrapper["children"][0], wrapper["children"][0]["children"][0]
        self.assertEqual(wrapper["attributes"]["type"], "Stack")
        self.assertEqual(wrapper["geometry"]["render_state_source"], "RenderService.allNodeNotOnTree")
        self.assertEqual(wrapper["geometry"]["native_window_rect"], "[0.00,169.00],[2091.00,1310.00]")
        for node in (parent, child):
            self.assertEqual(node["geometry"]["screen_bounds"], {"x": 546, "y": 373, "width": 0, "height": 0})
            self.assertEqual(node["geometry"]["render_state"], "not_rendered")
        self.assertEqual(set(parent["geometry"]["source"]["no_draw_render_node_ids"]), {"1363", "1364"})
        self.assertEqual(args, original)

    def test_explicit_lazy_identity_ancestor_uses_the_same_existing_omission_proof(self):
        result = self.verified(omitted_empty_ancestor_fixture("lazy"))
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 5)
        wrapper = result["children"][0]["children"][0]
        self.assertFalse(wrapper["geometry"]["source"]["render_node_on_screen_tree"])
        self.assertEqual(wrapper["children"][0]["geometry"]["render_state"], "not_rendered")

    def test_missing_attachment_proof_cannot_exempt_an_omitted_raw_frame_parent(self):
        self.assert_failed(omitted_empty_ancestor_fixture("missing"), "")

    def test_materialized_stack_cannot_be_skipped_even_when_its_transform_is_identity(self):
        self.assert_failed(omitted_empty_ancestor_fixture("active"), "empty_render_subtree_parent_mismatch")

    def test_omitted_stack_raw_nonidentity_transform_cannot_be_ignored_even_with_consistent_native_geometry(self):
        for proof in ("detached", "lazy"):
            args = omitted_empty_ancestor_fixture(proof)
            wrapper = args[0]["children"][0]["children"][0]
            wrapper["attributes"]["translate(x,y)"] = "1,0"
            native = args[3]["arkui_geometry"]["$children"][0]["$children"][0]
            native["$rect"] = native_rectangle(1, 169, 2091, 1141)
            native["$children"][0]["$rect"] = native_rectangle(1, 169, 0, 0)
            native["$children"][0]["$children"][0]["$rect"] = native_rectangle(1, 169, 0, 0)
            with self.subTest(proof=proof):
                self.assert_failed(args, "scene_render_frame_ancestor_missing")

    def test_detached_stack_scale_translate_and_unknown_modifier_must_still_match_omission_contract(self):
        for modifier in ("Scale:[x:2 y:1]", "Translate:[x:1 y:0]", "Rotation:[5]", "CustomClip:[0 0 1 1]"):
            args = omitted_empty_ancestor_fixture()
            args[3]["render_detached_tree"] = args[3]["render_detached_tree"].replace(
                "OtherModifiers:[]", f"OtherModifiers:[{modifier}]")
            with self.subTest(modifier=modifier):
                self.assert_failed(args, "")
