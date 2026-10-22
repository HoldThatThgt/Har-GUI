"""控件树页面分组和跨运行状态去重测试。"""

import copy
import json
import unittest

from har_capture.hdc import CaptureError
from har_capture.signatures import build_signatures, node_state


def fixture():
    node = {"attributes": {"type": "Column", "hostWindowId": "7", "bounds": "[20,30][220,230]"}, "children": [
        {"attributes": {"type": "Text", "text": "12:30", "bounds": "[20,30][70,50]"}, "children": []},
        {"attributes": {"type": "TextInput", "id": "editor", "text": "test", "bounds": "[30,60][190,90]",
                        "focused": "true"}, "children": []},
        {"attributes": {"type": "Scroll", "bounds": "[20,100][200,210]", "scrollOffset": 0}, "children": []}]}
    window = {"window_id": 7, "bundle_name": "app.editor", "window_type": 1, "window_mode": 102,
              "z_order": 100, "process_id": 42, "ui_node_id": 87,
              "bounding_box": {"x": 20, "y": 30, "width": 200, "height": 200},
              "transform": {"scale_x": 1, "scale_y": 1, "offset_x": 0, "offset_y": 0}}
    wms = {"capture_id": "frame", "canvas": {"width": 3120, "height": 2080}, "window_mode": "单窗口缩放",
           "app_windows": [window], "windows": [window]}
    trees = {"windows": [{"window_id": 7, "bundle_name": "app.editor", "tree": node}]}
    return wms, trees, copy.deepcopy(trees)


class SignatureTests(unittest.TestCase):
    def signatures(self, data):
        return build_signatures(*data, "app.editor")

    def test_runtime_identity_and_capture_time_do_not_change_signatures(self):
        first = fixture()
        second = copy.deepcopy(first)
        second[0]["capture_id"] = "new-frame"
        second[0]["app_windows"][0].update(window_id=99, process_id=80, ui_node_id=120, z_order=700)
        for tree in second[1:]:
            tree["windows"][0]["window_id"] = 99
            tree["windows"][0]["tree"]["attributes"].update(hostWindowId="99", accessibilityId="200")
        self.assertEqual(self.signatures(first), self.signatures(second))

    def test_clock_change_and_blinking_cursor_are_not_new_states(self):
        first = fixture()
        second = copy.deepcopy(first)
        for tree in second[1:]:
            root = tree["windows"][0]["tree"]
            root["children"][0]["attributes"].update(text="12:31", bounds="[20,30][80,50]")
            root["children"].append({"attributes": {"type": "Caret", "opacity": "0.5"}, "children": []})
        self.assertEqual(self.signatures(first), self.signatures(second))

    def test_editor_content_has_distinct_state_without_changing_page(self):
        first = fixture()
        second = copy.deepcopy(first)
        second[1]["windows"][0]["tree"]["children"][1]["attributes"]["text"] = "different private text"
        before, after = self.signatures(first), self.signatures(second)
        self.assertEqual(before["page_hash"], after["page_hash"])
        self.assertNotEqual(before["state_hash"], after["state_hash"])
        self.assertNotIn("different private text", str(after))

    def test_reading_content_fields_change_state_without_changing_page(self):
        for tree_index, key in ((1, "text"), (1, "originalText"), (1, "description"), (1, "hint"),
                                (2, "Content"), (2, "Hint"), (2, "textContent")):
            with self.subTest(tree=tree_index, field=key):
                first = fixture()
                leaf = first[tree_index]["windows"][0]["tree"]["children"][0]
                leaf["attributes"].pop("text")
                leaf["attributes"][key] = "first private mail body"
                second = copy.deepcopy(first)
                second[tree_index]["windows"][0]["tree"]["children"][0]["attributes"][key] = "second private mail body"
                before, after = self.signatures(first), self.signatures(second)
                self.assertEqual(before["page_hash"], after["page_hash"])
                self.assertNotEqual(before["state_hash"], after["state_hash"])

    def test_node_state_retains_only_content_digest(self):
        attrs = {"type": "Text", "text": "private mail body", "originalText": "private original body",
                 "description": "private description", "hint": "private hint", "Content": ["private ArkUI body"],
                 "Hint": "private ArkUI hint", "textContent": "private text content"}
        original = copy.deepcopy(attrs)
        state = node_state({"attributes": attrs}, {"x": 0, "y": 0, "width": 100, "height": 100})
        self.assertRegex(state["content_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("private", json.dumps(state))
        self.assertEqual(set(state), {"content_sha256"})
        self.assertEqual(attrs, original)

    def test_arkui_and_accessibility_clock_content_do_not_create_states(self):
        for kind, key in (("Text", "originalText"), ("Text", "Content"), ("Text", "textContent"),
                          ("TextClock,7,7", "Content"), ("TextTimer", "text")):
            with self.subTest(kind=kind, field=key):
                first = fixture()
                attrs = first[2]["windows"][0]["tree"]["children"][0]["attributes"]
                attrs.pop("text")
                attrs.update(type=kind)
                attrs[key] = "12:30"
                second = copy.deepcopy(first)
                second[2]["windows"][0]["tree"]["children"][0]["attributes"][key] = "12:31"
                self.assertEqual(self.signatures(first), self.signatures(second))

    def test_scroll_has_distinct_state_without_changing_page(self):
        first = fixture()
        second = copy.deepcopy(first)
        second[1]["windows"][0]["tree"]["children"][2]["attributes"]["scrollOffset"] = 120
        self.assertEqual(self.signatures(first)["page_hash"], self.signatures(second)["page_hash"])
        self.assertNotEqual(self.signatures(first)["state_hash"], self.signatures(second)["state_hash"])

    def test_scrolled_child_geometry_counts_when_no_offset_is_exposed(self):
        first = fixture()
        second = copy.deepcopy(first)
        second[1]["windows"][0]["tree"]["children"][2]["attributes"]["bounds"] = "[20,90][200,200]"
        self.assertNotEqual(self.signatures(first)["state_hash"], self.signatures(second)["state_hash"])

    def test_moved_window_keeps_page_and_changes_state(self):
        first = fixture()
        second = copy.deepcopy(first)
        second[0]["app_windows"][0]["bounding_box"]["x"] = 50
        self.assertEqual(self.signatures(first)["page_hash"], self.signatures(second)["page_hash"])
        self.assertNotEqual(self.signatures(first)["state_hash"], self.signatures(second)["state_hash"])

    def test_resizing_and_wms_transform_change_state(self):
        first = fixture()
        for key, value in (("bounding_box", {"x": 20, "y": 30, "width": 220, "height": 200}),
                           ("transform", {"scale_x": 0.8, "scale_y": 0.8, "offset_x": 0, "offset_y": 0})):
            second = copy.deepcopy(first)
            second[0]["app_windows"][0][key] = value
            self.assertNotEqual(self.signatures(first)["state_hash"], self.signatures(second)["state_hash"])

    def test_added_occluder_does_not_change_target_page(self):
        first = fixture()
        second = copy.deepcopy(first)
        window = copy.deepcopy(second[0]["app_windows"][0])
        window.update(window_id=8, bundle_name="app.calculator", z_order=101)
        second[0]["app_windows"].append(window)
        for tree in second[1:]:
            other = copy.deepcopy(tree["windows"][0])
            other.update(window_id=8, bundle_name="app.calculator")
            tree["windows"].append(other)
        before, after = self.signatures(first), self.signatures(second)
        self.assertEqual(before["page_hash"], after["page_hash"])
        self.assertNotEqual(before["state_hash"], after["state_hash"])
        second[0]["app_windows"][1]["z_order"] = 99
        self.assertNotEqual(after["state_hash"], self.signatures(second)["state_hash"])

    def test_readonly_identity_and_time_text_do_not_change_page(self):
        first = fixture()
        second = copy.deepcopy(first)
        second[1]["windows"][0]["tree"]["children"][0]["attributes"].update(text="new username", hashcode="88")
        self.assertEqual(self.signatures(first)["page_hash"], self.signatures(second)["page_hash"])

    def test_missing_tree_is_rejected(self):
        data = fixture()
        data[2]["windows"] = []
        with self.assertRaises(CaptureError):
            self.signatures(data)

    def test_wrong_bundle_and_duplicate_window_are_rejected(self):
        data = fixture()
        data[2]["windows"][0]["bundle_name"] = "wrong"
        with self.assertRaises(CaptureError):
            self.signatures(data)
        data = fixture()
        data[2]["windows"].append(data[2]["windows"][0])
        with self.assertRaises(CaptureError):
            self.signatures(data)

    def test_missing_target_is_rejected(self):
        with self.assertRaises(CaptureError):
            build_signatures(*fixture(), "absent")


if __name__ == "__main__":
    unittest.main()
