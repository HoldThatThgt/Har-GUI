"""验证完整森林、缓存根和实际窗口之间的严格关联。"""

import copy
import json
import unittest

from har_capture.arkui import parse_forest, count_arkui_nodes
from har_capture.arkui_selection import select_native_tree
from har_capture.geometry_capture import parse_geometry_response, verify_native_application
from har_capture.hdc import CaptureError


def full_source():
    text = (
        "|-> root,0,0 childSize:1\n    ID: 0\n    AccessibilityId: 0\n"
        "  |-> LazyForEach,1,11 childSize:1\n      ID: 1\n      AccessibilityId: 11\n"
        "      CachedItems: [[IfElse,3,13]]\n"
        "    |-> Text,2,12 childSize:0\n        ID: 2\n        AccessibilityId: 12\n"
        "        Parent: LazyForEach,1,11\n"
        "|-> IfElse,3,13 childSize:1\n    ID: 3\n    AccessibilityId: 13\n"
        "    Parent: LazyForEach,1,11\n"
        "  |-> Text,4,14 childSize:0\n      ID: 4\n      AccessibilityId: 14\n"
        "|-> Column,5,15 childSize:0\n    ID: 5\n    AccessibilityId: 15\n"
    )
    native = {"$ID": 0, "$type": "root", "$children": [
        {"$ID": 1, "$type": "LazyForEach", "$CachedItems": "[[IfElse,3,13]]", "$children": [
            {"$ID": 2, "$type": "Text"},
            {"$ID": 3, "$type": "IfElse", "$children": [{"$ID": 4, "$type": "Text"}]}]}]}
    return parse_forest(text), native


class CompleteSourceTests(unittest.TestCase):
    def test_declared_cached_root_is_attached_without_discarding_source(self):
        roots, native = full_source()
        before = copy.deepcopy(roots)
        result = select_native_tree(roots, native)
        self.assertEqual(roots, before)
        self.assertEqual(len(roots), 3)
        self.assertEqual(count_arkui_nodes(result), 5)
        cached = result["children"][0]["children"][1]
        self.assertEqual(cached["attributes"]["type"], "IfElse")
        self.assertEqual(cached["source_header"], "IfElse,3,13")
        self.assertEqual(cached["source_line"], roots[1]["source_line"])
        self.assertEqual(cached["source_association"]["relationship"], "cached_item")
        self.assertIsNone(cached["source_association"]["raw_parent_id"])
        self.assertEqual(cached["source_association"]["native_parent_id"], "1")
        self.assertEqual(cached["raw"], roots[1]["raw"])

    def test_cache_owner_and_identity_mutations_are_rejected(self):
        mutations = {
            "missing raw declaration": lambda r, n: r[0]["children"][0]["attributes"].pop("CachedItems"),
            "wrong raw parent": lambda r, n: r[1]["attributes"].update(Parent="LazyForEach,9,11"),
            "wrong accessibility": lambda r, n: r[1]["attributes"].update(AccessibilityId="99"),
            "wrong native owner": lambda r, n: n["$children"][0].update(**{"$CachedItems": "[]"}),
            "trailing cache content": lambda r, n: n["$children"][0].update(
                **{"$CachedItems": "[[IfElse,3,13]] unexpected"}),
            "wrong cached tag": lambda r, n: n["$children"][0].update(
                **{"$CachedItems": "[[Row,3,13]]"}),
            "unknown native child": lambda r, n: n["$children"].append({"$ID": 99, "$type": "Text"}),
            "missing native child": lambda r, n: n["$children"][0]["$children"].pop(0),
        }
        for label, mutate in mutations.items():
            roots, native = full_source()
            mutate(roots, native)
            with self.subTest(label=label), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                select_native_tree(roots, native)

    def test_cached_frame_requires_its_own_last_parent(self):
        roots, native = full_source()
        roots[1]["attributes"]["FrameRect"] = "RectT (0, 0) - [1 x 1]"
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            select_native_tree(roots, native)
        roots[1]["attributes"]["LastParentId"] = "1"
        self.assertEqual(count_arkui_nodes(select_native_tree(roots, native)), 5)

    def test_broken_frame_reference_cannot_hide_conflicting_unparsed_render_text(self):
        roots, native = full_source()
        frame = roots[0]["children"][0]["children"][0]
        frame["attributes"].update(Description="Children", FrameRect="RectT (0, 0) - [1 x 1]")
        frame["raw"] += "    CanvasNode[100] child[] lazyLoad[0] nodeState[ACTIVE]\n"
        alias = copy.deepcopy(frame)
        alias["attributes"]["Description"] = "BrokenChildren"
        roots.append(alias)
        self.assertEqual(count_arkui_nodes(select_native_tree(roots, native)), 5)
        alias["raw"] += "    CanvasNode[999] child[] lazyLoad[0] nodeState[ACTIVE]\n"
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            select_native_tree(roots, native)

    def test_broken_reference_requires_an_identical_real_entity(self):
        roots, native = full_source()
        original = roots[0]["children"][0]["children"][0]
        original["attributes"].update(Description="Children", DumpDepth="2")
        alias = copy.deepcopy(original)
        alias["source_line"] = 100
        alias["attributes"].update(Description="BrokenChildren", DumpDepth="0")
        roots.append(alias)
        self.assertEqual(count_arkui_nodes(select_native_tree(roots, native)), 5)
        for field, value in (("Description", "Children"), ("AccessibilityId", "999"), ("Parent", "Row,8,8")):
            changed = copy.deepcopy(roots)
            changed[-1]["attributes"][field] = value
            with self.subTest(field=field), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                select_native_tree(changed, native)
        roots[-1]["children"] = [copy.deepcopy(original)]
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            select_native_tree(roots, native)

    def test_native_key_order_is_irrelevant_but_body_identity_is_not(self):
        tree = {"appInfo": {"BundleName": "com.example.app", "WindowID": 9}, "$type": "root", "$ID": 0}
        response = "WinId: 9\nPid: 100\n" + json.dumps(tree)
        parsed = parse_geometry_response(response)
        self.assertEqual(parsed, tree)
        window = {"window_id": 9, "process_id": 100, "bundle_name": "com.example.app"}
        verify_native_application(parsed, window)
        for mutation in ({"WindowID": 8}, {"BundleName": "com.example.other"}):
            wrong = copy.deepcopy(parsed)
            wrong["appInfo"].update(mutation)
            with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                verify_native_application(wrong, window)

    def test_explicit_overlay_branch_is_preserved_for_independent_render_validation(self):
        roots, native = full_source()
        overlay = parse_forest("|-> Column,8,18 childSize:1\n    ID: 8\n    Description: OverlayNode\n"
                               "  |-> Text,9,19 childSize:0\n      ID: 9\n")[0]
        roots[0]["children"].insert(0, overlay)
        selected = select_native_tree(roots, native)
        self.assertEqual(selected["children"][0]["attributes"]["ID"], "8")
        self.assertEqual(selected["children"][0]["children"][0]["attributes"]["ID"], "9")
        self.assertEqual(selected["children"][0]["source_association"]["relationship"], "overlay")
        self.assertNotIn("geometry", selected["children"][0])
        self.assertEqual(count_arkui_nodes(selected), 7)
        overlay["attributes"]["Description"] = "Children"
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            select_native_tree(roots, native)

    def test_system_uicontent_identity_does_not_impersonate_session(self):
        tree = {"appInfo": {"WindowID": 1, "BundleName": "com.ohos.sceneboard"}, "$ID": 0, "$type": "root"}
        verify_native_application(tree, {"window_id": 7, "is_system": True})
        self.assertEqual(tree["appInfo"]["WindowID"], 1)


if __name__ == "__main__":
    unittest.main()
