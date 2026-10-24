"""@brief 回收包装节点按双树中的父节点和有序子身份区分，原始标识及完整结构保持不变。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from har_capture import coordinates
from har_capture.arkui import count_arkui_nodes, parse_forest
from har_capture.arkui_selection import index_nodes, read_window_arkui, select_native_tree
from har_capture.capture_facts import normalized_arkui
from har_capture.hdc import CaptureError
from har_capture.metadata import artifact_info
from har_capture.scene_evidence import source_path
from har_capture.tree_capture import save_full_arkui_tree
from tests.test_coordinates import fixture
from tests.test_undefined_image_identity import raw_frame, save_source


def structural_node(indent, identity, parent):
    """@brief 生成没有布局框的官方复合头和完整父身份。"""
    tag, node_id, accessibility, children = identity
    prefix, body = " " * indent, " " * (indent + 4)
    text = (f"{prefix}|-> {tag},{node_id},{accessibility} childSize:{children}\n"
            f"{body}ID: {node_id}\n{body}AccessibilityId: {accessibility}\n{body}Description: Children\n")
    if parent is not None:
        text += f"{body}Parent: {parent[0]},{parent[1]},{parent[2]}\n"
    return text


def full_recycle_source():
    """@brief 同一 Column 下两个回收节点分别包装不同 JsView 与布局后代。"""
    text = raw_frame(0, ("root", 0, 10, 1), None, "RectT (0, 0) - [200 x 150]", 100)
    text += raw_frame(2, ("Column", 1, 11, 2), ("root", 0, 10), "RectT (10, 20) - [100 x 80]", 101)
    text += structural_node(4, ("__Recycle__", -1, 13, 1), ("Column", 1, 11))
    text += structural_node(6, ("JsView", 3, 14, 1), ("__Recycle__", -1, 13))
    text += raw_frame(8, ("Text", 2, 12, 0), ("JsView", 3, 14), "RectT (5, -10) - [20 x 30]", 102)
    text += structural_node(4, ("__Recycle__", -1, 15, 1), ("Column", 1, 11))
    text += structural_node(6, ("JsView", 4, 16, 1), ("__Recycle__", -1, 15))
    text += raw_frame(8, ("Text", 5, 17, 0), ("JsView", 4, 16), "RectT (40, 30) - [12 x 14]", 105)
    native = {"$ID": 0, "$type": "root", "$rect": "[0,0],[200,150]", "$children": [
        {"$ID": 1, "$type": "Column", "$rect": "[10,20],[110,100]", "$children": [
            {"$ID": -1, "$type": "__Recycle__", "$children": [
                {"$ID": 3, "$type": "JsView", "$children": [
                    {"$ID": 2, "$type": "Text", "$rect": "[15,10],[35,40]"}]}]},
            {"$ID": -1, "$type": "__Recycle__", "$children": [
                {"$ID": 4, "$type": "JsView", "$children": [
                    {"$ID": 5, "$type": "Text", "$rect": "[50,50],[62,64]"}]}]}]}]}
    return text, native


def recycle_fixture():
    """@brief 沿用已验证屏幕映射和真实渲染父链，结构包装节点不伪造渲染节点。"""
    args = list(fixture())
    text, native = full_recycle_source()
    args[0] = select_native_tree(parse_forest(text), native)
    args[3]["arkui_geometry"] = native
    args[3]["render_tree"] += (
        "          | CANVAS_NODE[105], Properties: Bounds[40 30 12 14] Frame[40 30 12 14], OtherModifiers:[]\n")
    return args


def multiple_child_source():
    """@brief 将两个明确子节点放在同一包装节点下，完整顺序作为身份的一部分。"""
    text, native = full_recycle_source()
    text = text.replace(structural_node(4, ("__Recycle__", -1, 15, 1), ("Column", 1, 11)), "")
    text = text.replace("__Recycle__,-1,13 childSize:1", "__Recycle__,-1,13 childSize:2")
    text = text.replace("Column,1,11 childSize:2", "Column,1,11 childSize:1")
    text = text.replace("Parent: __Recycle__,-1,15", "Parent: __Recycle__,-1,13")
    wrappers = native["$children"][0]["$children"]
    wrappers[0]["$children"].extend(wrappers[1]["$children"])
    wrappers.pop()
    return parse_forest(text), native


def saved_recycle_fixture(stage):
    """@brief 从原始响应保存完整森林，再由生产读回和坐标接口独立核验。"""
    args = recycle_fixture()
    window, canvas, evidence = args[1:]
    window["bundle_name"] = "com.example.recycle"
    text, native = full_recycle_source()
    native["appInfo"] = {"WindowID": 9, "BundleName": window["bundle_name"]}
    header = "WinId: 9\nPid: 1001\n"
    raw = save_source(stage, header + text, "application-response.txt")
    geometry = {"response": save_source(stage, header + json.dumps(native), "native-response.txt"),
                "arkui_geometry": save_source(stage, json.dumps(native), "native.json"),
                "render_tree": save_source(stage, evidence["render_tree"], "render-tree.txt"),
                "render_detached_tree": save_source(stage, evidence["render_detached_tree"], "render-detached.txt")}
    record = save_full_arkui_tree(stage, stage / raw["file"], geometry)
    record.update(response=raw, origin={"kind": "stdout"}, session={"is_system": False})
    window["trees"] = {"arkui": record}
    return window, canvas, {9: evidence["scene"]}


class RecycleIdentityTests(unittest.TestCase):
    def rejected_source(self, roots, native):
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            select_native_tree(roots, native)

    def verified(self, args):
        result = coordinates.normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        return result

    def rejected_geometry(self, args, code):
        with self.assertLogs(level="ERROR"):
            result = coordinates.normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        self.assertTrue(any(code in issue["code"] for issue in result["coordinate_validation"]["issues"]),
                        result["coordinate_validation"]["issues"])
        return result

    def test_same_parent_wrappers_keep_all_ids_raw_fields_paths_and_distinct_child_identity(self):
        text, native = full_recycle_source()
        roots = parse_forest(text)
        before = copy.deepcopy((roots, native))
        selected = select_native_tree(roots, native)
        self.assertEqual(count_arkui_nodes(selected), 8)
        for index, child_id in enumerate((3, 4)):
            original = roots[0]["children"][0]["children"][index]
            wrapper = selected["children"][0]["children"][index]
            key = f"1/__Recycle__[JsView:{child_id}]"
            self.assertEqual(coordinates.raw_node_key(original, "1"), key)
            self.assertEqual(coordinates.native_node_key(native["$children"][0]["$children"][index], "1"), key)
            self.assertEqual(wrapper["attributes"]["ID"], "-1")
            self.assertEqual(wrapper["source_header"], original["attributes"]["type"])
            for name in ("source_attributes", "raw", "source_line", "declared_child_count"):
                self.assertEqual(wrapper[name], original[name])
            self.assertEqual(wrapper["children"][0]["attributes"]["ID"], str(child_id))
            self.assertEqual(wrapper["children"][0]["source_association"]["native_parent_id"], key)
        self.assertEqual((roots, native), before)

    def test_complete_saved_forest_reloads_and_normalizes_both_frame_subtrees(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            window, canvas, scenes = saved_recycle_fixture(stage)
            before = {path.name: path.read_bytes() for path in stage.iterdir()}
            selected = read_window_arkui(stage, window)
            result = normalized_arkui(stage, window, canvas, scenes)["tree"]
            self.assertEqual(count_arkui_nodes(result), 8)
            self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 4)
            self.assertEqual(result["coordinate_validation"]["non_layout_nodes"], 4)
            expected = ({"x": 115, "y": 60, "width": 20, "height": 30},
                        {"x": 150, "y": 100, "width": 12, "height": 14})
            for index, wrapper in enumerate(result["children"][0]["children"]):
                original = selected["children"][0]["children"][index]
                self.assertEqual(wrapper["attributes"], original["attributes"])
                self.assertEqual(wrapper["geometry"]["status"], "not_applicable")
                frame = wrapper["children"][0]["children"][0]
                self.assertEqual(frame["geometry"]["screen_bounds"], expected[index])
                self.assertEqual(frame["geometry"]["source"]["node_id"], str((2, 5)[index]))
                self.assertEqual(frame["geometry"]["source"]["render_node_id"], str((102, 105)[index]))
            self.assertEqual(before, {path.name: path.read_bytes() for path in stage.iterdir()})

    def test_synchronously_reordered_siblings_still_preserve_both_subtrees(self):
        text, native = full_recycle_source()
        roots = parse_forest(text)
        roots[0]["children"][0]["children"].reverse()
        native["$children"][0]["$children"].reverse()
        selected = select_native_tree(roots, native)
        self.assertEqual([node["children"][0]["attributes"]["ID"]
                          for node in selected["children"][0]["children"]], ["4", "3"])
        args = recycle_fixture()
        args[0], args[3]["arkui_geometry"] = selected, native
        self.assertEqual(self.verified(args)["coordinate_validation"]["verified_frame_nodes"], 4)

    def test_one_sided_wrapper_order_changes_are_rejected(self):
        for changed in ("raw", "native"):
            text, native = full_recycle_source()
            roots = parse_forest(text)
            if changed == "raw":
                roots[0]["children"][0]["children"].reverse()
            else:
                native["$children"][0]["$children"].reverse()
            with self.subTest(changed=changed):
                self.rejected_source(roots, native)

    def test_multiple_assigned_children_use_the_entire_ordered_identity_list(self):
        roots, native = multiple_child_source()
        selected = select_native_tree(roots, native)
        wrapper = selected["children"][0]["children"][0]
        self.assertEqual(coordinates.raw_node_key(wrapper, "1"), "1/__Recycle__[JsView:3,JsView:4]")
        self.assertEqual(coordinates.native_node_key(native["$children"][0]["$children"][0], "1"),
                         "1/__Recycle__[JsView:3,JsView:4]")
        self.assertEqual([node["attributes"]["ID"] for node in wrapper["children"]], ["3", "4"])
        self.assertEqual(count_arkui_nodes(selected), 7)
        args = recycle_fixture()
        args[0], args[3]["arkui_geometry"] = selected, native
        self.assertEqual(self.verified(args)["coordinate_validation"]["verified_frame_nodes"], 4)

    def test_multiple_child_order_mismatch_is_rejected(self):
        roots, native = multiple_child_source()
        roots[0]["children"][0]["children"][0]["children"].reverse()
        self.rejected_source(roots, native)

    def test_assigned_zero_child_id_is_a_valid_structural_anchor(self):
        text = structural_node(0, ("Column", 10, 10, 1), None)
        text += structural_node(2, ("__Recycle__", -1, 11, 1), ("Column", 10, 10))
        text += structural_node(4, ("JsView", 0, 12, 0), ("__Recycle__", -1, 11))
        native = {"$ID": 10, "$type": "Column", "$children": [
            {"$ID": -1, "$type": "__Recycle__", "$children": [{"$ID": 0, "$type": "JsView"}]}]}
        result = select_native_tree(parse_forest(text), native)
        self.assertEqual(coordinates.raw_node_key(result["children"][0], "10"), "10/__Recycle__[JsView:0]")

    def test_missing_child_or_conflicting_child_type_is_rejected(self):
        for mutation in ("raw_missing", "native_missing", "native_type"):
            text, native = full_recycle_source()
            roots = parse_forest(text)
            raw_wrapper = roots[0]["children"][0]["children"][0]
            native_wrapper = native["$children"][0]["$children"][0]
            if mutation == "raw_missing":
                raw_wrapper["children"].clear()
            elif mutation == "native_missing":
                native_wrapper["$children"].clear()
            else:
                native_wrapper["$children"][0]["$type"] = "IfElse"
            with self.subTest(mutation=mutation):
                self.rejected_source(roots, native)

    def test_repeated_positive_child_ids_still_fail_globally(self):
        text, native = full_recycle_source()
        roots = parse_forest(text)
        native["$children"][0]["$children"][1]["$children"][0]["$ID"] = 3
        roots[0]["children"][0]["children"][1]["children"][0]["attributes"]["ID"] = "3"
        self.rejected_source(roots, native)

    def test_identical_wrapper_subtree_is_not_deduplicated(self):
        text, native = full_recycle_source()
        roots = parse_forest(text)
        roots[0]["children"][0]["children"][1] = copy.deepcopy(roots[0]["children"][0]["children"][0])
        native["$children"][0]["$children"][1] = copy.deepcopy(native["$children"][0]["$children"][0])
        self.rejected_source(roots, native)

    def test_unassigned_child_identity_cannot_anchor_a_recycle_wrapper(self):
        for child_tag in ("JsView", "Image", "__Recycle__"):
            text, native = full_recycle_source()
            roots = parse_forest(text)
            child = roots[0]["children"][0]["children"][0]["children"][0]
            child["attributes"].update(ID="-1", type=child_tag)
            native["$children"][0]["$children"][0]["$children"][0].update({"$ID": -1, "$type": child_tag})
            with self.subTest(child_tag=child_tag):
                self.rejected_source(roots, native)

    def test_single_empty_wrapper_keeps_parent_scope_but_two_empty_wrappers_fail(self):
        for count in (1, 2):
            text, native = full_recycle_source()
            roots = parse_forest(text)
            wrappers = roots[0]["children"][0]["children"][:count]
            originals = native["$children"][0]["$children"][:count]
            for raw, original in zip(wrappers, originals, strict=True):
                raw["children"], original["$children"] = [], []
                raw["declared_child_count"] = 0
            roots[0]["children"][0]["children"], native["$children"][0]["$children"] = wrappers, originals
            if count == 2:
                self.rejected_source(roots, native)
            else:
                result = select_native_tree(roots, native)
                self.assertEqual(coordinates.raw_node_key(result["children"][0]["children"][0], "1"), "1/__Recycle__")

    def test_overlay_does_not_supply_native_anchor_to_empty_wrapper(self):
        text, native = full_recycle_source()
        roots = parse_forest(text)
        raw_wrapper = roots[0]["children"][0]["children"][0]
        overlay = raw_wrapper["children"][0]["children"][0]
        overlay["attributes"].update(Description="OverlayNode", LastParentId="-1")
        raw_wrapper["children"] = [overlay]
        roots[0]["children"][0]["children"] = [raw_wrapper]
        native_wrapper = native["$children"][0]["$children"][0]
        native_wrapper["$children"] = []
        native["$children"][0]["$children"] = [native_wrapper]
        selected = select_native_tree(roots, native)
        wrapper = selected["children"][0]["children"][0]
        self.assertEqual(coordinates.raw_node_key(wrapper, "1"), "1/__Recycle__")
        self.assertEqual(wrapper["children"][0]["attributes"]["ID"], "2")
        self.assertEqual(wrapper["children"][0]["source_association"]["relationship"], "overlay")

    def test_two_empty_wrappers_are_ambiguous_even_with_distinct_raw_overlays(self):
        text, native = full_recycle_source()
        roots = parse_forest(text)
        for raw, original in zip(roots[0]["children"][0]["children"],
                                 native["$children"][0]["$children"], strict=True):
            overlay = raw["children"][0]["children"][0]
            overlay["attributes"].update(Description="OverlayNode", LastParentId="-1")
            raw["children"], original["$children"] = [overlay], []
        self.rejected_source(roots, native)

    def test_wrong_native_parent_cannot_reassociate_raw_wrapper(self):
        text, native = full_recycle_source()
        roots = parse_forest(text)
        moved = native["$children"][0]["$children"].pop()
        native["$children"].append({"$ID": 6, "$type": "Column", "$children": [moved]})
        roots[0]["children"].append(parse_forest(structural_node(0, ("Column", 6, 18, 0), ("root", 0, 10)))[0])
        self.rejected_source(roots, native)

    def test_frame_descendants_keep_native_geometry_and_actual_render_parent_checks(self):
        args = recycle_fixture()
        node = args[3]["arkui_geometry"]["$children"][0]["$children"][1]["$children"][0]["$children"][0]
        node["$rect"] = "[51,50],[63,64]"
        rejected = self.rejected_geometry(args, "native_geometry_local_projection_mismatch")
        self.assertEqual(count_arkui_nodes(rejected), 8)
        args = recycle_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "          | CANVAS_NODE[105]", "        | CANVAS_NODE[105]")
        self.rejected_geometry(args, "parent_mismatch")

    def test_duplicate_render_identity_is_not_hidden_by_distinct_wrapper_anchors(self):
        args = recycle_fixture()
        frame = args[0]["children"][0]["children"][1]["children"][0]["children"][0]
        frame["raw"] = frame["raw"].replace("CanvasNode[105]", "CanvasNode[102]")
        self.rejected_geometry(args, "ambiguous_render_geometry_mapping")

    def test_wrapper_parent_reference_must_match_actual_parent_complete_header(self):
        for parent in (None, "Column,9,11", "Row,1,11", "Column,1,999"):
            text, native = full_recycle_source()
            original = "        Parent: Column,1,11\n"
            changed = "" if parent is None else f"        Parent: {parent}\n"
            roots = parse_forest(text.replace(original, changed, 1))
            with self.subTest(parent=parent):
                self.rejected_source(roots, native)

    def test_wrapped_child_parent_reference_must_match_its_specific_wrapper_header(self):
        for parent in (None, "__Recycle__,-1,15", "__Recycle__,-1,999", "__Recycle__,8,13", "Column,-1,13"):
            text, native = full_recycle_source()
            original = "          Parent: __Recycle__,-1,13\n"
            changed = "" if parent is None else f"          Parent: {parent}\n"
            roots = parse_forest(text.replace(original, changed, 1))
            with self.subTest(parent=parent):
                self.rejected_source(roots, native)

    def test_swapped_wrapped_children_cannot_keep_stale_raw_parent_references(self):
        text, native = full_recycle_source()
        roots = parse_forest(text)
        raw = roots[0]["children"][0]["children"]
        actual = native["$children"][0]["$children"]
        raw[0]["children"], raw[1]["children"] = raw[1]["children"], raw[0]["children"]
        actual[0]["$children"], actual[1]["$children"] = actual[1]["$children"], actual[0]["$children"]
        self.assertEqual(raw[0]["children"][0]["attributes"]["Parent"], "__Recycle__,-1,15")
        self.assertEqual(raw[0]["attributes"]["type"], "__Recycle__,-1,13")
        self.rejected_source(roots, native)

    def test_recycle_wrapper_cannot_gain_raw_or_native_layout_geometry(self):
        for side in ("raw", "native", "both"):
            text, native = full_recycle_source()
            roots = parse_forest(text)
            if side in ("raw", "both"):
                roots[0]["children"][0]["children"][0]["attributes"]["FrameRect"] = "RectT (0, 0) - [0 x 0]"
            if side in ("native", "both"):
                native["$children"][0]["$children"][0]["$rect"] = "[10,20],[10,20]"
            with self.subTest(side=side):
                self.rejected_source(roots, native)

    def test_rehashed_selected_parent_association_cannot_replace_rebuilt_source_identity(self):
        for field in ("raw_parent_id", "native_parent_id"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                stage = Path(temporary)
                window, _, _ = saved_recycle_fixture(stage)
                record = window["trees"]["arkui"]
                original_files = {name: (stage / record[name]["file"]).read_bytes() for name in ("raw", "forest")}
                selected_path = stage / record["file"]
                selected = json.loads(selected_path.read_bytes())
                child = selected["children"][0]["children"][0]["children"][0]
                self.assertEqual(child["source_association"][field], "1/__Recycle__[JsView:3]")
                child["source_association"][field] = "1/__Recycle__"
                selected_path.write_text(json.dumps(selected), encoding="utf-8")
                record.update(artifact_info(stage, selected_path))
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "JSON does not match"):
                    read_window_arkui(stage, window)
                self.assertEqual(original_files, {name: (stage / record[name]["file"]).read_bytes()
                                                  for name in ("raw", "forest")})

    def test_scene_records_verify_full_raw_parent_evidence_without_application_selection(self):
        text, native = full_recycle_source()
        root = parse_forest(text)[0]
        scene = {"path": [root], "native": native}
        before = copy.deepcopy(scene)
        records = coordinates.scene_records(scene)
        self.assertEqual(set(records["nodes"]), {"100", "101", "102", "105"})
        self.assertEqual(records["parents"]["3"], "1/__Recycle__[JsView:3]")
        self.assertEqual(records["parents"]["4"], "1/__Recycle__[JsView:4]")
        self.assertEqual(records["rectangles"]["102"], {"x": 15, "y": 10, "width": 20, "height": 30})
        self.assertEqual(records["rectangles"]["105"], {"x": 50, "y": 50, "width": 12, "height": 14})
        self.assertEqual(scene, before)
        for target, parent, reason in (("wrapper", "Column,1,999", "recycle_parent_or_layout_mismatch"),
                                        ("child", "__Recycle__,-1,15", "recycle_child_parent_mismatch")):
            changed = copy.deepcopy(scene)
            wrapper = changed["path"][0]["children"][0]["children"][0]
            node = wrapper if target == "wrapper" else wrapper["children"][0]
            node["attributes"]["Parent"] = parent
            with self.subTest(target=target):
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(ValueError, reason):
                    coordinates.scene_records(changed)

    def test_direct_normalization_rechecks_recycle_parent_fields_after_source_selection(self):
        for target, parent, reason in (("wrapper", "Column,1,999", "recycle_parent_or_layout_mismatch"),
                                        ("child", "__Recycle__,-1,15", "recycle_child_parent_mismatch")):
            args = recycle_fixture()
            wrapper = args[0]["children"][0]["children"][0]
            node = wrapper if target == "wrapper" else wrapper["children"][0]
            node["attributes"]["Parent"] = parent
            before = copy.deepcopy(args)
            with self.subTest(target=target):
                rejected = self.rejected_geometry(args, reason)
                self.assertEqual(count_arkui_nodes(rejected), 8)
                self.assertEqual(args, before)

    def test_scene_recycle_ancestor_keeps_its_complete_child_for_identity(self):
        text = structural_node(0, ("Screen", 900, 900, 1), None)
        text += structural_node(2, ("__Recycle__", -1, 901, 1), ("Screen", 900, 900))
        text += structural_node(4, ("JsView", 902, 902, 1), ("__Recycle__", -1, 901))
        text += structural_node(6, ("WindowScene", 903, 90, 1), ("JsView", 902, 902))
        text += structural_node(8, ("Text", 904, 904, 0), ("WindowScene", 903, 90))
        raw = text.encode("utf-8")
        lines, nodes = index_nodes(raw)
        index = next(i for i, node in enumerate(nodes) if node["tag"] == b"WindowScene")
        path = source_path({"raw": raw, "lines": lines, "nodes": nodes}, index)
        self.assertEqual([node["attributes"]["ID"] for node in path], ["900", "-1", "902", "903"])
        self.assertEqual(path[0]["children"], [])
        self.assertEqual(path[2]["children"], [])
        self.assertEqual(count_arkui_nodes(path[1]), 4)
        self.assertEqual(coordinates.raw_node_key(path[1], "900"), "900/__Recycle__[JsView:902]")
        self.assertEqual(path[1]["children"][0]["children"][0]["children"][0]["attributes"]["ID"], "904")
        self.assertEqual(path[3]["children"][0]["attributes"]["ID"], "904")
        self.assertEqual(path[1]["children"][0]["source_line"], path[2]["source_line"])
        native = {"$ID": 900, "$type": "Screen", "$children": [
            {"$ID": -1, "$type": "__Recycle__", "$children": [
                {"$ID": 902, "$type": "JsView", "$children": [
                    {"$ID": 903, "$type": "WindowScene", "$children": [{"$ID": 904, "$type": "Text"}]}]}]}]}
        records = coordinates.scene_records({"path": path, "native": native})
        self.assertEqual(records["parents"]["902"], "900/__Recycle__[JsView:902]")
        self.assertEqual(records["parents"]["903"], "902")
        self.assertEqual(len(records["native"]), 5)
        for original, replacement, reason in (
                ("Parent: Screen,900,900", "Parent: Screen,999,900", "recycle_parent_or_layout_mismatch"),
                ("Parent: __Recycle__,-1,901", "Parent: __Recycle__,-1,999", "recycle_child_parent_mismatch")):
            changed = text.replace(original, replacement, 1).encode("utf-8")
            lines, nodes = index_nodes(changed)
            index = next(i for i, node in enumerate(nodes) if node["tag"] == b"WindowScene")
            bad_path = source_path({"raw": changed, "lines": lines, "nodes": nodes}, index)
            with self.subTest(reference=original):
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(ValueError, reason):
                    coordinates.scene_records({"path": bad_path, "native": native})


if __name__ == "__main__":
    unittest.main()
