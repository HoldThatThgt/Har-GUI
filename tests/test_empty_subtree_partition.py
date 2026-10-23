"""完整空子树的观测帧应分为实际挂载和明确未挂载两组，不伪造逻辑子节点。"""

import copy
import unittest

from har_capture.arkui import parse_arkui
from har_capture.coordinates import normalize_arkui
from tests.test_viewport_geometry import empty_subtree_fixture, native_rectangle, raw_rectangle
from tests.test_window_mapping import component, render_line


def partition_fixture():
    """匿名保留 Row、隐藏 Column、声明数量和 FrameProxy，实际 RS 父为叶节点。"""
    args = empty_subtree_fixture()
    zero = raw_rectangle(0, 0, 0, 0)
    raw = ("|-> Row childSize:1\n"
           "  | ID: 438\n  | Description: Children\n  | LastParentId: 2\n"
           f"  | FrameRect: {zero}\n  | PaintRect without transform: {zero}\n"
           "  | CanvasNode[502] child[] lazyLoad[0] nodeState[ACTIVE]\n"
           "  | FrameProxy: FrameChildNode:[439-0-1,] partFrameNodeChildren:[439,] TotalCount:1\n"
           "  |-> Column childSize:1\n"
           "    | ID: 439\n    | Description: Children\n    | LastParentId: 438\n"
           f"    | FrameRect: {zero}\n    | PaintRect without transform: {zero}\n"
           "    | CanvasNode[503] child[] lazyLoad[0] nodeState[ACTIVE]\n"
           "    | FrameProxy: FrameChildNode:[440-0-1,] partFrameNodeChildren:[440,] TotalCount:1\n")
    args[0]["children"][0]["children"] = [parse_arkui(raw)]
    native = {"$ID": 438, "$type": "Row", "$rect": native_rectangle(0, 169, 0, 0), "$children": [
        {"$ID": 439, "$type": "Column", "$rect": native_rectangle(0, 169, 0, 0),
         "visible": "false", "$children": []}]}
    args[3]["arkui_geometry"]["$children"][0]["$children"] = [native]
    rows = args[3]["render_tree"].splitlines(keepends=True)[:6]
    rows.append(render_line(6, "CANVAS_NODE", 502, "-inf -inf -inf -inf",
                            "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]"))
    args[3]["render_tree"] = "".join(rows)
    args[3]["render_detached_tree"] = "-- Render Node Not On Tree\n" + render_line(
        0, "CANVAS_NODE", 503, "-inf -inf -inf -inf",
        "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]")
    return args


def add_observed_sibling(args, state="mounted"):
    """在匿名观测中增加有原始和原生来源的零尺寸兄弟，不使用 FrameProxy 补造节点。"""
    parent = args[0]["children"][0]["children"][0]
    sibling = component(441, "Column", raw_rectangle(0, 0, 0, 0))
    sibling["raw"] = "CanvasNode[504] child[] lazyLoad[0] nodeState[ACTIVE]"
    parent["children"].append(sibling)
    parent["declared_child_count"] = 2
    proxy = "FrameChildNode:[439-0-1,441-1-1,] partFrameNodeChildren:[439,441,] TotalCount:2"
    parent["raw"] = parent["raw"].replace(parent["attributes"]["FrameProxy"], proxy)
    for attributes in (parent["attributes"], parent["source_attributes"]):
        attributes["FrameProxy"] = proxy
    native = args[3]["arkui_geometry"]["$children"][0]["$children"][0]
    native["$children"].append({"$ID": 441, "$type": "Column", "$rect": native_rectangle(0, 169, 0, 0)})
    row = render_line(7 if state == "mounted" else 1, "CANVAS_NODE", 504, "-inf -inf -inf -inf",
                      "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]")
    if state == "mounted":
        rows = args[3]["render_tree"].splitlines(keepends=True)
        rows[-1] = rows[-1].rstrip("\n") + ", sortedChildren: 1(504 )\n"
        args[3]["render_tree"] = "".join(rows) + row
    else:
        args[3]["render_detached_tree"] = args[3]["render_detached_tree"].rstrip("\n") + ", sortedChildren: 1(504 )\n"
        args[3]["render_detached_tree"] += row


def rewrite_render(args, state, render_id, mutate):
    """只修改指定匿名渲染节点行，避免误改其他来源。"""
    key = "render_tree" if state == "mounted" else "render_detached_tree"
    rows = args[3][key].splitlines(keepends=True)
    matches = [index for index, row in enumerate(rows) if f"CANVAS_NODE[{render_id}]," in row]
    if len(matches) != 1:
        raise AssertionError("Synthetic render mutation requires one exact source node")
    original = rows[matches[0]]
    rows[matches[0]] = mutate(original)
    if rows[matches[0]] == original:
        raise AssertionError("Synthetic render mutation did not change its target")
    args[3][key] = "".join(rows)


class EmptySubtreePartitionTests(unittest.TestCase):
    def verified(self, args):
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        return result

    def assert_failed(self, args, code=""):
        result = normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        issues = result["coordinate_validation"]["issues"]
        self.assertTrue(any(code in issue["code"] for issue in issues), issues)
        return result

    def test_mounted_leaf_and_detached_child_partition_preserves_every_observed_frame_and_proxy(self):
        args = partition_fixture()
        original = copy.deepcopy(args)
        result = self.verified(args)
        parent = result["children"][0]["children"][0]
        child = parent["children"][0]
        self.assertEqual(result["coordinate_validation"]["frame_nodes"], 4)
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 4)
        self.assertEqual(parent["attributes"]["type"], "Row")
        self.assertEqual(child["attributes"]["type"], "Column")
        self.assertEqual(parent["declared_child_count"], 1)
        self.assertEqual(child["declared_child_count"], 1)
        self.assertEqual(child["children"], [])
        self.assertEqual(child["attributes"]["FrameProxy"],
                         "FrameChildNode:[440-0-1,] partFrameNodeChildren:[440,] TotalCount:1")
        self.assertIn("FrameChildNode:[440-0-1,]", child["raw"])
        self.assertEqual(parent["geometry"]["source"]["no_draw_render_node_ids"], ["502"])
        self.assertEqual(parent["geometry"]["source"]["no_draw_detached_render_node_ids"], ["503"])
        for node in (parent, child):
            self.assertEqual(node["geometry"]["screen_bounds"], {"x": 546, "y": 373, "width": 0, "height": 0})
            self.assertEqual(node["geometry"]["native_window_rect"], "[0.00,169.00],[0.00,169.00]")
            self.assertFalse(node["geometry"]["rendered"])
        self.assertEqual(child["geometry"]["render_state_source"], "RenderService.allNodeNotOnTree")
        self.assertEqual(child["geometry"]["clip_chain"], [])
        self.assertIsNone(child["geometry"]["rectangular_clip_bounds"])
        self.assertEqual(args, original)

    def test_mixed_observed_mounted_and_detached_children_have_complete_disjoint_proof_sets(self):
        args = partition_fixture()
        add_observed_sibling(args)
        result = self.verified(args)
        parent = result["children"][0]["children"][0]
        source = parent["geometry"]["source"]
        mounted, detached = set(source["no_draw_render_node_ids"]), set(source["no_draw_detached_render_node_ids"])
        self.assertEqual(mounted, {"502", "504"})
        self.assertEqual(detached, {"503"})
        self.assertFalse(mounted.intersection(detached))
        self.assertEqual(mounted.union(detached), {"502", "503", "504"})
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 5)
        self.assertEqual([node["attributes"]["ID"] for node in parent["children"]], ["439", "441"])

    def test_unrelated_detached_forest_is_not_misidentified_as_a_claimed_descendant(self):
        args = partition_fixture()
        args[3]["render_detached_tree"] += render_line(0, "CANVAS_NODE", 999, "0 0 20 20", "OtherModifiers:[]")
        parent = self.verified(args)["children"][0]["children"][0]
        self.assertEqual(parent["geometry"]["source"]["no_draw_detached_render_node_ids"], ["503"])

    def test_complete_observed_detached_descendant_forest_has_matching_native_parent_chain(self):
        args = partition_fixture()
        column = args[0]["children"][0]["children"][0]["children"][0]
        child = component(442, "Text", raw_rectangle(0, 0, 0, 0))
        child["raw"] = "CanvasNode[504] child[] lazyLoad[0] nodeState[ACTIVE]"
        column["children"] = [child]
        column["raw"] = column["raw"].replace("440", "442")
        for attributes in (column["attributes"], column["source_attributes"]):
            attributes["FrameProxy"] = attributes["FrameProxy"].replace("440", "442")
        native = args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$children"][0]
        native.pop("visible")
        native["$children"] = [{"$ID": 442, "$type": "Text", "$rect": native_rectangle(0, 169, 0, 0)}]
        rewrite_render(args, "detached", 503, lambda row: row.rstrip("\n") + ", sortedChildren: 1(504 )\n")
        args[3]["render_detached_tree"] += render_line(1, "CANVAS_NODE", 504, "-inf -inf -inf -inf",
            "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]")
        result = self.verified(args)
        parent = result["children"][0]["children"][0]
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 5)
        self.assertEqual(parent["geometry"]["source"]["no_draw_render_node_ids"], ["502"])
        self.assertEqual(set(parent["geometry"]["source"]["no_draw_detached_render_node_ids"]), {"503", "504"})
        self.assertEqual(parent["children"][0]["children"][0]["attributes"]["ID"], "442")

    def test_explicit_finite_zero_detached_bounds_and_frame_can_prove_the_same_empty_observed_node(self):
        args = partition_fixture()
        rewrite_render(args, "detached", 503, lambda row: row.replace(
            "Bounds[-inf -inf -inf -inf] Frame[-inf -inf -inf -inf]", "Bounds[0 0 0 0] Frame[0 0 0 0]"))
        result = self.verified(args)
        parent = result["children"][0]["children"][0]
        self.assertEqual(parent["geometry"]["source"]["no_draw_detached_render_node_ids"], ["503"])
        self.assertEqual(parent["children"][0]["declared_child_count"], 1)
        self.assertEqual(parent["children"][0]["children"], [])

    def test_detached_identity_vectors_and_explicitly_disabled_clips_remain_supported(self):
        args = partition_fixture()
        rewrite_render(args, "detached", 503, lambda row: row.replace("OtherModifiers:[]",
            "ClipToBounds[False], ClipToFrame[False], OtherModifiers:[Scale:[x:1 y:1], "
            "Translate:[x:0 y:0], ClipToBounds:[false], ClipToFrame:[false]]"))
        self.verified(args)

    def test_current_detached_bounds_and_frame_must_both_prove_zero_area_without_conflicting_origin(self):
        rectangles = (("0 0 0 0", "0 0 1 0"), ("0 0 0 0", "0 0 0 1"),
                      ("0 0 0 0", "1 0 0 0"), ("0 0 0 0", "0 1 0 0"),
                      ("0 0 1 0", "0 0 0 0"), ("1 0 0 0", "0 0 0 0"))
        for bounds, frame in rectangles:
            args = partition_fixture()
            rewrite_render(args, "detached", 503, lambda row: row.replace(
                "Bounds[-inf -inf -inf -inf] Frame[-inf -inf -inf -inf]",
                f"Bounds[{bounds}] Frame[{frame}]"))
            with self.subTest(bounds=bounds, frame=frame):
                self.assert_failed(args)

    def test_mixed_or_partially_nonfinite_current_detached_rectangles_cannot_supply_empty_geometry(self):
        rectangles = (("-inf -inf -inf -inf", "0 0 0 0"), ("0 0 0 0", "-inf -inf -inf -inf"),
                      ("-inf 0 0 0", "0 0 0 0"), ("0 0 0 0", "0 -inf 0 0"))
        for bounds, frame in rectangles:
            args = partition_fixture()
            rewrite_render(args, "detached", 503, lambda row: row.replace(
                "Bounds[-inf -inf -inf -inf] Frame[-inf -inf -inf -inf]",
                f"Bounds[{bounds}] Frame[{frame}]"))
            with self.subTest(bounds=bounds, frame=frame):
                self.assert_failed(args)

    def test_frame_proxy_and_hidden_flag_cannot_replace_missing_detached_attachment_evidence(self):
        args = partition_fixture()
        args[3]["render_detached_tree"] = "-- Render Node Not On Tree\n"
        self.assert_failed(args, "empty_render_subtree_attachment_unverified")

    def test_explicit_lazy_identity_frame_is_separate_from_detached_partition(self):
        args = partition_fixture()
        child = args[0]["children"][0]["children"][0]["children"][0]
        child["raw"] = child["raw"].replace("lazyLoad[0] nodeState[ACTIVE]", "lazyLoad[1] nodeState[LAZY_LOAD]")
        args[3]["render_detached_tree"] = "-- Render Node Not On Tree\n"
        result = self.verified(args)
        parent = result["children"][0]["children"][0]
        self.assertEqual(parent["geometry"]["source"]["no_draw_detached_render_node_ids"], [])
        self.assertEqual(parent["geometry"]["source"]["no_draw_unmaterialized_render_node_ids"], ["503"])

    def test_mount_and_detached_membership_conflict_is_rejected_before_any_partition_claim(self):
        args = partition_fixture()
        args[3]["render_tree"] += render_line(7, "CANVAS_NODE", 503, "-inf -inf -inf -inf",
            "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]")
        self.assert_failed(args, "render_attachment_state_changed")

    def test_unknown_actual_mounted_and_detached_descendants_are_not_hidden_by_no_draw_flags(self):
        for state, root_id in (("mounted", 502), ("detached", 503)):
            args = partition_fixture()
            rewrite_render(args, state, root_id,
                           lambda row: row.rstrip("\n") + ", sortedChildren: 1(999 )\n")
            key = "render_tree" if state == "mounted" else "render_detached_tree"
            args[3][key] += render_line(7 if state == "mounted" else 1, "CANVAS_NODE", 999,
                "-inf -inf -inf -inf", "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]")
            with self.subTest(state=state):
                self.assert_failed(args, "membership_mismatch")

    def test_detached_root_with_a_foreign_parent_is_not_a_valid_partition_boundary(self):
        args = partition_fixture()
        args[3]["render_detached_tree"] = "-- Render Node Not On Tree\n" + render_line(
            0, "CANVAS_NODE", 999, "0 0 20 20", "OtherModifiers:[]") + render_line(
            1, "CANVAS_NODE", 503, "-inf -inf -inf -inf",
            "hasDrawContent: 0, hasChildrenDrawContent: 0, OtherModifiers:[]")
        self.assert_failed(args, "empty_render_subtree_detached_parent_mismatch")

    def test_detached_parent_relationship_must_match_native_frame_siblings(self):
        args = partition_fixture()
        add_observed_sibling(args, "detached")
        self.assert_failed(args, "empty_render_subtree_detached_parent_mismatch")

    def test_detached_native_identity_and_parent_must_still_match_the_raw_tree(self):
        for corruption in ("type", "parent"):
            args = partition_fixture()
            modal = args[3]["arkui_geometry"]["$children"][0]
            child = modal["$children"][0]["$children"][0]
            if corruption == "type":
                child["$type"] = "Row"
            else:
                modal["$children"].append(modal["$children"][0]["$children"].pop())
            with self.subTest(corruption=corruption):
                self.assert_failed(args)

    def test_detached_zero_size_native_origin_is_not_exempted_from_projection_validation(self):
        args = partition_fixture()
        child = args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$children"][0]
        child["$rect"] = native_rectangle(0, 170, 0, 0)
        result = self.assert_failed(args, "native_geometry_local_projection_mismatch")
        self.assertEqual(result["coordinate_validation"]["frame_nodes"], 4)
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 3)

    def test_consistent_positive_area_detached_frame_is_not_an_empty_subtree_proof(self):
        args = partition_fixture()
        child = args[0]["children"][0]["children"][0]["children"][0]
        for key in ("FrameRect", "PaintRect without transform"):
            child["attributes"][key] = raw_rectangle(0, 0, 1, 1)
        native = args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$children"][0]
        native["$rect"] = native_rectangle(0, 169, 1, 1)
        self.assert_failed(args, "empty_render_subtree_nonzero_geometry")

    def test_each_attachment_group_needs_unambiguous_dual_zero_draw_flags(self):
        for state, render_id in (("mounted", 502), ("detached", 503)):
            for flag in ("hasDrawContent", "hasChildrenDrawContent"):
                for replacement in ("", f"{flag}: 1,", f"{flag}: 0, {flag}: 0,", f"{flag}: 0, {flag}: 1,"):
                    args = partition_fixture()
                    rewrite_render(args, state, render_id,
                                   lambda row: row.replace(f"{flag}: 0,", replacement))
                    with self.subTest(state=state, flag=flag, replacement=replacement):
                        self.assert_failed(args)

    def test_detached_modifiers_and_enabled_clips_are_not_exempted_by_off_tree_state(self):
        for state, render_id in (("mounted", 502), ("detached", 503)):
            for modifier in ("Rotation:[5]", "Scale:[x:2 y:1]", "Translate:[x:1 y:0]",
                             "CustomClip:[0 0 0 0]", "ClipToBounds:[true]", "ClipToFrame:[true]"):
                args = partition_fixture()
                def mutate(row):
                    row = row.replace("OtherModifiers:[]", f"OtherModifiers:[{modifier}]")
                    if modifier.startswith("ClipTo"):
                        row = row.replace("OtherModifiers:", modifier.split(":", 1)[0] + "[True], OtherModifiers:")
                    return row
                rewrite_render(args, state, render_id, mutate)
                with self.subTest(state=state, modifier=modifier):
                    self.assert_failed(args)

    def test_incomplete_detached_children_printout_is_not_complete_membership_evidence(self):
        args = partition_fixture()
        rewrite_render(args, "detached", 503,
                       lambda row: row.rstrip("\n") + ", sortedChildren: 1(999 )\n")
        self.assert_failed(args, "empty_render_subtree_children_unverified")
