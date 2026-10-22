"""@brief 验证系统窗口组件树的关联和原始字节截取。"""

import unittest

from har_capture.arkui import count_arkui_nodes, parse_arkui
from har_capture.arkui_selection import select_system_tree
from har_capture.hdc import CaptureError


def scene(identity="175", component="session9", node_id="83"):
    return (
        f"  |-> WindowScene,{node_id},{identity} childSize:1\n"
        f"      | ID: {node_id}\n"
        f"      | AccessibilityId: {identity}\n"
        f"      | compid: {component}\n"
        "    |-> Text,84,176 childSize:0\n"
        "        | Content: Example\n"
    )


class ArkuiSelectionTests(unittest.TestCase):
    """@brief 使用匿名组件数据验证选择规则。"""

    def select(self, raw):
        return select_system_tree(raw, {"window_id": 9}, {"ui_node_id": 175})

    def test_multiple_roots_and_siblings_select_exact_subtree(self):
        prefix = "Header\n|-> root childSize:3\n" + scene("50", "session7", "50")
        selected = scene()
        suffix = scene("90", "session10", "90") + "|-> root childSize:0\nTail\n"
        raw = (prefix + selected + suffix).encode()
        subtree, source = self.select(raw)
        self.assertEqual(subtree, selected.encode())
        self.assertEqual(source, {
            "byte_offset": len(prefix.encode()),
            "byte_length": len(selected.encode()),
            "start_line": len(prefix.splitlines()) + 1,
            "end_line": len((prefix + selected).splitlines()),
            "accessibility_id": 175,
            "component_id": "session9",
            "node_id": 83,
        })
        self.assertEqual(count_arkui_nodes(parse_arkui(subtree.decode())), 2)

    def test_chinese_crlf_bytes_are_preserved(self):
        prefix = "窗口信息：匿名\r\n|-> root childSize:1\r\n".encode()
        selected = scene().replace("Example", "测试文字").replace("\n", "\r\n").encode()
        suffix = b"|-> root childSize:0\r\n"
        raw = prefix + selected + suffix
        subtree, source = self.select(raw)
        self.assertEqual(subtree, selected)
        self.assertEqual(source["byte_offset"], len(prefix))
        self.assertEqual(source["byte_length"], len(selected))
        self.assertEqual(raw[source["byte_offset"]:source["byte_offset"] + source["byte_length"]], subtree)

    def test_matching_id_does_not_replace_accessibility_id(self):
        raw = scene("999", "session9", "175").encode()
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            self.select(raw)

    def test_wrong_component_or_tag_is_rejected(self):
        cases = [scene(component="session90"), scene().replace("WindowScene,", "Stack,")]
        for text in cases:
            with self.subTest(text=text), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                self.select(text.encode())

    def test_ambiguous_matches_and_duplicate_identity_are_rejected(self):
        cases = [scene() + scene(), scene().replace("      | ID:", "      | compid: session9\n      | ID:")]
        for text in cases:
            with self.subTest(text=text), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                self.select(text.encode())

    def test_child_identity_does_not_match_parent(self):
        text = (
            "|-> WindowScene childSize:1\n"
            "    | ID: 175\n"
            "  |-> Text childSize:0\n"
            "      | AccessibilityId: 175\n"
            "      | compid: session9\n"
        )
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            self.select(text.encode())

    def test_last_subtree_preserves_remaining_text(self):
        raw = (scene() + "Trailing diagnostic text\n").encode()
        subtree, source = self.select(raw)
        self.assertEqual(subtree, raw)
        self.assertEqual(source["end_line"], len(raw.splitlines()))

    def test_missing_tree_or_node_id_is_rejected(self):
        cases = ["Header only\n", scene().replace("      | ID: 83\n", ""), scene(node_id="invalid")]
        for text in cases:
            with self.subTest(text=text), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                self.select(text.encode())


if __name__ == "__main__":
    unittest.main()
