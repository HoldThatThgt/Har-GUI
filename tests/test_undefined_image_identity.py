"""@brief 默认 Image 标识按真实 ImageAnimator 父节点关联，完整来源及几何仍逐项核验。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from har_capture import coordinates
from har_capture.arkui import count_arkui_nodes, parse_forest
from har_capture.arkui_selection import read_window_arkui, select_native_tree
from har_capture.capture_facts import normalized_arkui
from har_capture.hdc import CaptureError
from har_capture.metadata import artifact_info
from har_capture.tree_capture import save_full_arkui_tree
from tests.test_coordinates import fixture, overlay_syntax_fixture
from tests.test_transparent_subtree import transparent_fixture, rewrite_render


def raw_frame(indent, identity, parent, rectangle, render_id):
    """@brief 生成带官方复合头、父身份和独立客户端渲染节点的匿名原文。"""
    tag, node_id, accessibility, children = identity
    prefix, body = " " * indent, " " * (indent + 4)
    text = (f"{prefix}|-> {tag},{node_id},{accessibility} childSize:{children}\n"
            f"{body}ID: {node_id}\n{body}AccessibilityId: {accessibility}\n{body}Description: Children\n")
    if parent is not None:
        text += f"{body}Parent: {parent[0]},{parent[1]},{parent[2]}\n{body}LastParentId: {parent[1]}\n"
    return (text + f"{body}FrameRect: {rectangle}\n{body}PaintRect without transform: {rectangle}\n"
            f"{body}CanvasNode[{render_id}] child[] lazyLoad[0] nodeState[ACTIVE]\n")


def full_image_source():
    """@brief 两个不同动画组件各自持有一个默认标识 Image，不按矩形或标识去重。"""
    text = raw_frame(0, ("root", 0, 10, 2), None, "RectT (0, 0) - [200 x 150]", 100)
    text += raw_frame(2, ("ImageAnimator", 1, 11, 1), ("root", 0, 10), "RectT (10, 20) - [100 x 80]", 101)
    text += raw_frame(4, ("Image", -1, 12, 0), ("ImageAnimator", 1, 11), "RectT (5, -10) - [20 x 30]", 102)
    text += raw_frame(2, ("ImageAnimator", 3, 13, 1), ("root", 0, 10), "RectT (80, 50) - [60 x 40]", 103)
    text += raw_frame(4, ("Image", -1, 14, 0), ("ImageAnimator", 3, 13), "RectT (7, 8) - [12 x 14]", 104)
    native = {"$ID": 0, "$type": "root", "$rect": "[0,0],[200,150]", "$children": [
        {"$ID": 1, "$type": "ImageAnimator", "$rect": "[10,20],[110,100]", "$children": [
            {"$ID": -1, "$type": "Image", "$rect": "[15,10],[35,40]"}]},
        {"$ID": 3, "$type": "ImageAnimator", "$rect": "[80,50],[140,90]", "$children": [
            {"$ID": -1, "$type": "Image", "$rect": "[87,58],[99,72]"}]}]}
    return text, native


def image_geometry_fixture():
    """@brief 复用已有整屏映射，只替换完整应用树及实际渲染父链。"""
    args = list(fixture())
    text, native = full_image_source()
    args[0] = select_native_tree(parse_forest(text), native)
    args[3]["arkui_geometry"] = native
    rows = args[3]["render_tree"].splitlines(keepends=True)
    rows.extend([
        "        | CANVAS_NODE[103], Properties: Bounds[80 50 60 40] Frame[80 50 60 40], OtherModifiers:[]\n",
        "          | CANVAS_NODE[104], Properties: Bounds[7 8 12 14] Frame[7 8 12 14], OtherModifiers:[]\n",
    ])
    args[3]["render_tree"] = "".join(rows)
    return args


def save_source(stage, content, name):
    """@brief 写入匿名实际来源文件并使用生产摘要接口登记。"""
    path = stage / name
    path.write_text(content, encoding="utf-8")
    return artifact_info(stage, path)


def complete_saved_fixture(stage):
    """@brief 用生产保存接口生成可由 normalized_arkui 独立重读的完整匿名来源。"""
    args = image_geometry_fixture()
    window, canvas, evidence = args[1:]
    window["bundle_name"] = "com.example.animator"
    text, native = full_image_source()
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


class UndefinedImageIdentityTests(unittest.TestCase):
    def verified(self, args):
        result = coordinates.normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "verified",
                         result["coordinate_validation"]["issues"])
        return result

    def rejected(self, args, code=""):
        with self.assertLogs(level="ERROR"):
            result = coordinates.normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        self.assertTrue(any(code in issue["code"] for issue in result["coordinate_validation"]["issues"]),
                        result["coordinate_validation"]["issues"])
        return result

    def rejected_source(self, roots, native):
        args = image_geometry_fixture()
        args[3]["arkui_geometry"] = native
        with self.assertLogs(level="ERROR"):
            try:
                args[0] = select_native_tree(roots, native)
            except CaptureError:
                return
            result = coordinates.normalize_arkui(*args)
        self.assertEqual(result["coordinate_validation"]["status"], "failed")
        self.assertTrue(result["coordinate_validation"]["issues"])

    def test_complete_forest_keeps_both_original_images_and_source_associations(self):
        text, native = full_image_source()
        roots = parse_forest(text)
        before = copy.deepcopy((roots, native))
        result = select_native_tree(roots, native)
        self.assertEqual(count_arkui_nodes(result), 5)
        for index, parent in enumerate(("1", "3")):
            original = roots[0]["children"][index]["children"][0]
            image = result["children"][index]["children"][0]
            self.assertEqual(image["attributes"]["ID"], "-1")
            self.assertEqual(image["attributes"]["type"], "Image")
            self.assertEqual(image["source_header"], original["attributes"]["type"])
            self.assertEqual(image["source_attributes"], original["source_attributes"])
            self.assertEqual(image["raw"], original["raw"])
            self.assertEqual(image["source_line"], original["source_line"])
            self.assertEqual(image["source_association"]["raw_parent_id"], parent)
            self.assertEqual(image["source_association"]["native_parent_id"], parent)
            self.assertEqual(coordinates.frame_key(image), parent + "/Image")
        self.assertEqual((roots, native), before)

    def test_saved_complete_source_reloads_and_normalizes_each_native_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            window, canvas, scenes = complete_saved_fixture(stage)
            before = {path.name: path.read_bytes() for path in stage.iterdir()}
            source = read_window_arkui(stage, window)
            result = normalized_arkui(stage, window, canvas, scenes)["tree"]
            self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 5)
            expected = ((115, 60, 20, 30), (187, 108, 12, 14))
            for index, parent in enumerate(("1", "3")):
                image = result["children"][index]["children"][0]
                geometry = image["geometry"]
                self.assertEqual(geometry["screen_bounds"], dict(zip(("x", "y", "width", "height"), expected[index])))
                self.assertEqual(geometry["source"]["node_id"], "-1")
                self.assertEqual(geometry["source"]["node_key"], parent + "/Image")
                self.assertEqual(geometry["source"]["render_node_id"], str(102 + index * 2))
                self.assertEqual(image["attributes"], source["children"][index]["children"][0]["attributes"])
            self.assertNotIn("node_key", result["geometry"]["source"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in stage.iterdir()})

    def test_same_parent_duplicate_image_is_still_ambiguous(self):
        text, native = full_image_source()
        roots = parse_forest(text)
        roots[0]["children"][0]["children"].append(copy.deepcopy(roots[0]["children"][0]["children"][0]))
        native["$children"][0]["$children"].append(copy.deepcopy(native["$children"][0]["$children"][0]))
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            select_native_tree(roots, native)

    def test_undefined_image_requires_native_image_animator_parent(self):
        for tag in ("Row", "Image", "Column"):
            text, native = full_image_source()
            text = text.replace("ImageAnimator,1,11", tag + ",1,11")
            native["$children"][0]["$type"] = tag
            with self.subTest(tag=tag), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                select_native_tree(parse_forest(text), native)

    def test_positive_duplicate_id_is_not_scoped_by_parent(self):
        text, native = full_image_source()
        text = text.replace("Image,-1,", "Image,7,").replace("ID: -1\n", "ID: 7\n")
        for parent in native["$children"]:
            parent["$children"][0]["$ID"] = 7
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "duplicate_native_geometry_id"):
            select_native_tree(parse_forest(text), native)

    def test_moved_native_image_cannot_borrow_the_other_animator_raw_child(self):
        text, native = full_image_source()
        native["$children"][0]["$children"], native["$children"][1]["$children"] = (
            native["$children"][1]["$children"], native["$children"][0]["$children"])
        args = image_geometry_fixture()
        args[0] = select_native_tree(parse_forest(text), native)
        args[3]["arkui_geometry"] = native
        self.rejected(args, "native_geometry_local_projection_mismatch")

    def test_missing_wrong_or_invalid_last_parent_cannot_normalize_an_image(self):
        for value in (None, "3", "999", "", "not-a-node", ["1", "3"]):
            text, native = full_image_source()
            roots = parse_forest(text)
            image = roots[0]["children"][0]["children"][0]
            if value is None:
                image["attributes"].pop("LastParentId")
            else:
                image["attributes"]["LastParentId"] = value
            with self.subTest(value=value):
                self.rejected_source(roots, native)

    def test_parent_reference_must_match_even_with_correct_last_parent_and_structure(self):
        for value in (None, "ImageAnimator,3,13", "Row,1,11", "ImageAnimator,1", ""):
            text, native = full_image_source()
            original = "        Parent: ImageAnimator,1,11\n"
            replacement = "" if value is None else f"        Parent: {value}\n"
            roots = parse_forest(text.replace(original, replacement))
            image = roots[0]["children"][0]["children"][0]
            self.assertEqual(image["attributes"]["LastParentId"], "1")
            with self.subTest(value=value):
                self.rejected_source(roots, native)

    def test_wrong_parent_accessibility_identity_is_rejected_during_source_selection(self):
        text, native = full_image_source()
        roots = parse_forest(text.replace("Parent: ImageAnimator,1,11", "Parent: ImageAnimator,1,999"))
        image = roots[0]["children"][0]["children"][0]
        self.assertEqual(image["attributes"]["LastParentId"], "1")
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            select_native_tree(roots, native)

    def test_direct_normalization_rechecks_parent_type_and_id_after_selection(self):
        for value in (None, "ImageAnimator,3,13", "Row,1,11", "ImageAnimator,1", ""):
            args = image_geometry_fixture()
            attrs = args[0]["children"][0]["children"][0]["attributes"]
            if value is None:
                attrs.pop("Parent")
            else:
                attrs["Parent"] = value
            self.assertEqual(attrs["LastParentId"], "1")
            with self.subTest(value=value):
                self.rejected(args, "undefined_image_parent_identity_mismatch")

    def test_last_parent_conflict_in_already_selected_tree_is_rejected_again(self):
        args = image_geometry_fixture()
        args[0]["children"][0]["children"][0]["attributes"]["LastParentId"] = "3"
        self.rejected(args)

    def test_wrong_actual_render_parent_is_rejected(self):
        args = image_geometry_fixture()
        args[3]["render_tree"] = args[3]["render_tree"].replace(
            "          | CANVAS_NODE[104]", "        | CANVAS_NODE[104]")
        self.rejected(args, "parent_mismatch")

    def test_duplicate_client_render_identity_is_not_hidden_by_distinct_parent_keys(self):
        args = image_geometry_fixture()
        second = args[0]["children"][1]["children"][0]
        second["raw"] = second["raw"].replace("CanvasNode[104]", "CanvasNode[102]")
        self.rejected(args, "ambiguous_render_geometry_mapping")

    def test_missing_raw_or_native_image_is_not_silently_discarded(self):
        for missing in ("raw", "native"):
            text, native = full_image_source()
            roots = parse_forest(text)
            if missing == "raw":
                roots[0]["children"][0]["children"].clear()
            else:
                native["$children"][0]["$children"].clear()
            with self.subTest(missing=missing), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                select_native_tree(roots, native)

    def test_clip_is_applied_to_the_correct_scoped_image(self):
        result = self.verified(image_geometry_fixture())
        first = result["children"][0]["children"][0]["geometry"]
        second = result["children"][1]["children"][0]["geometry"]
        self.assertEqual(first["rectangular_clip_bounds"], {"x": 115, "y": 70, "width": 20, "height": 20})
        self.assertEqual(second["rectangular_clip_bounds"], second["screen_bounds"])
        self.assertEqual(first["native_window_rect"], "[15,10],[35,40]")
        self.assertEqual(second["native_window_rect"], "[87,58],[99,72]")

    def test_identical_geometry_does_not_merge_images_from_different_animators(self):
        args = image_geometry_fixture()
        first, second = args[0]["children"]
        for original, target in ((first, second), (first["children"][0], second["children"][0])):
            for name in ("FrameRect", "PaintRect without transform"):
                target["attributes"][name] = original["attributes"][name]
        native_first, native_second = args[3]["arkui_geometry"]["$children"]
        native_second["$rect"] = native_first["$rect"]
        native_second["$children"][0]["$rect"] = native_first["$children"][0]["$rect"]
        rewrite_render(args, 103, lambda row: row.replace("80 50 60 40", "10 20 100 80"))
        rewrite_render(args, 104, lambda row: row.replace("7 8 12 14", "5 -10 20 30"))
        result = self.verified(args)
        images = [parent["children"][0] for parent in result["children"]]
        self.assertEqual(images[0]["geometry"]["screen_bounds"], images[1]["geometry"]["screen_bounds"])
        self.assertEqual([image["geometry"]["source"]["node_key"] for image in images], ["1/Image", "3/Image"])
        self.assertEqual([image["geometry"]["source"]["render_node_id"] for image in images], ["102", "104"])

    def test_zero_area_no_draw_image_keeps_its_parent_scoped_native_position(self):
        args = image_geometry_fixture()
        image = args[0]["children"][0]["children"][0]
        image["attributes"].update({"FrameRect": "RectT (5, -10) - [0 x 0]",
                                    "PaintRect without transform": "RectT (5, -10) - [0 x 0]"})
        args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[15,10],[15,10]"
        rewrite_render(args, 102, lambda row: row.replace(
            "Properties:", "hasDrawContent: 0, hasChildrenDrawContent: 0, Properties:").replace(
                "Bounds[5 -10 20 30] Frame[5 -10 20 30]", "Bounds[-inf -inf -inf -inf] Frame[-inf -inf -inf -inf]"))
        result = self.verified(args)
        geometry = result["children"][0]["children"][0]["geometry"]
        self.assertFalse(geometry["rendered"])
        self.assertEqual(geometry["screen_bounds"], {"x": 115, "y": 60, "width": 0, "height": 0})
        self.assertEqual(geometry["source"]["node_key"], "1/Image")
        self.assertEqual(geometry["source"]["no_draw_render_node_ids"], ["102"])
        rewrite_render(args, 102, lambda row: row.replace("hasDrawContent: 0,", "hasDrawContent: 1,"))
        self.rejected(args)

    def test_transparent_animator_preserves_unprepared_image_and_its_native_mapping(self):
        args = transparent_fixture()
        parent, image = args[0]["children"][0], args[0]["children"][0]["children"][0]
        parent["attributes"]["type"] = "ImageAnimator"
        image["attributes"].update(ID="-1", type="Image", LastParentId="182", Parent="ImageAnimator,182,182")
        parent["source_attributes"], image["source_attributes"] = dict(parent["attributes"]), dict(image["attributes"])
        native = args[3]["arkui_geometry"]["$children"][0]
        native["$type"] = "ImageAnimator"
        native["$children"][0].update({"$ID": -1, "$type": "Image"})
        result = self.verified(args)
        geometry = result["children"][0]["children"][0]["geometry"]
        self.assertEqual(geometry["screen_bounds"], {"x": 1022, "y": 788, "width": 46, "height": 46})
        self.assertFalse(geometry["rendered"])
        self.assertEqual(geometry["source"]["node_id"], "-1")
        self.assertEqual(geometry["source"]["node_key"], "182/Image")
        self.assertEqual(geometry["source"]["render_geometry_state"], "unprepared")
        self.assertEqual(geometry["source"]["zero_alpha_ancestor"], {"node_id": "182", "render_node_id": "182"})

    def test_overlay_below_recycle_checks_original_parent_id_instead_of_internal_key(self):
        args = overlay_syntax_fixture()
        column = args[0]["children"][0]
        overlay = column["children"][0]
        overlay["attributes"]["LastParentId"] = "-1"
        overlay["source_attributes"] = dict(overlay["attributes"])
        wrapper = {"attributes": {"ID": "-1", "type": "__Recycle__", "Description": "Children"},
                   "raw": "", "children": [overlay], "source_line": 5}
        column["children"] = [wrapper]
        args[3]["arkui_geometry"]["$children"][0]["$children"] = [
            {"$ID": -1, "$type": "__Recycle__", "$children": []}]
        before = copy.deepcopy(args)
        result = self.verified(args)
        actual = result["children"][0]["children"][0]["children"][0]
        self.assertEqual(actual["attributes"]["LastParentId"], "-1")
        self.assertEqual(actual["geometry"]["screen_bounds"], {"x": 115, "y": 60, "width": 20, "height": 30})
        self.assertEqual(actual["geometry"]["rectangular_clip_bounds"],
                         {"x": 115, "y": 70, "width": 20, "height": 20})
        self.assertTrue(actual["geometry"]["source"]["overlay_render_cross_checked"])
        self.assertFalse(actual["geometry"]["source"]["native_rect_cross_checked"])
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 3)
        self.assertEqual(result["coordinate_validation"]["non_layout_nodes"], 2)
        self.assertEqual(args, before)
        for invalid_parent in ("1/__Recycle__", "1", "99"):
            changed = copy.deepcopy(args)
            child = changed[0]["children"][0]["children"][0]["children"][0]
            child["attributes"]["LastParentId"] = invalid_parent
            with self.subTest(invalid_parent=invalid_parent):
                self.rejected(changed, "overlay_parent_evidence_mismatch")

    def test_overlay_image_cannot_impersonate_an_animator_without_matching_native_parent(self):
        for parent_tag in ("Column", "ImageAnimator"):
            args = fixture()
            parent = args[0]["children"][0]
            parent["attributes"]["type"] = parent_tag
            image = parent["children"][0]
            image["attributes"].update(ID="-1", type="Image", LastParentId="1",
                                       Parent="ImageAnimator,1,1", Description="OverlayNode")
            image["source_attributes"] = dict(image["attributes"])
            args[3]["arkui_geometry"]["$children"][0]["$children"] = []
            before = copy.deepcopy(args)
            with self.subTest(parent_tag=parent_tag):
                result = self.rejected(args, "raw_frame_identity_or_parent_mismatch:1/Image")
                self.assertEqual(count_arkui_nodes(result), 3)
                preserved = result["children"][0]["children"][0]
                self.assertEqual(preserved["attributes"], image["attributes"])
                self.assertEqual(preserved["source_attributes"], image["source_attributes"])
                self.assertEqual(preserved["raw"], image["raw"])
                self.assertEqual(args, before)


if __name__ == "__main__":
    unittest.main()
