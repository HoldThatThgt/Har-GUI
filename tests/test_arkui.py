"""@brief 验证 ArkUI 组件树层级和原始属性保留。"""

import unittest

from har_capture.arkui import count_arkui_nodes, parse_arkui, parse_forest
from har_capture.hdc import CaptureError


class ArkuiParsingTests(unittest.TestCase):
    """@brief 使用匿名文本验证原始组件树解析。"""

    def test_first_line_root_and_nested_nodes(self):
        text = (
            "|-> root childSize:9\n"
            "    | ID: 0\n"
            "  |-> Column childSize:1\n"
            "      | ID: 7\n"
            "    |-> Text childSize:0\n"
            "          Content: Sample\n"
            "  |-> Button childSize:0\n"
            "        ID: 9\n"
        )
        root = parse_arkui(text)
        self.assertEqual(root["attributes"], {"type": "root", "ID": "0"})
        self.assertEqual(root["source_line"], 1)
        self.assertEqual(root["declared_child_count"], 9)
        self.assertEqual(count_arkui_nodes(root), 4)
        self.assertEqual(root["children"][0]["children"][0]["attributes"]["Content"], "Sample")
        self.assertEqual(root["children"][1]["attributes"]["ID"], "9")
        self.assertEqual(root["children"][1]["source_line"], 7)

    def test_raw_preserves_multiline_content_and_line_endings(self):
        properties = "    | Payload: {\r\n        nested text\r\n    | }\r\n    | blank:\r\n\r\n"
        root = parse_arkui("|-> root childSize:0\r\n" + properties)
        self.assertEqual(root["raw"], properties)
        self.assertEqual(root["attributes"]["Payload"], "{")
        self.assertEqual(root["attributes"]["blank"], "")
        self.assertIn("nested text", root["raw"])

    def test_repeated_properties_and_reserved_type_remain_available(self):
        text = (
            "|-> Text childSize:0\n"
            "      ID: 31\n"
            "      ID: 32\n"
            "      ID: 33\n"
            "      type: original\n"
            "      type: another\n"
        )
        root = parse_arkui(text)
        self.assertEqual(root["attributes"]["type"], "Text")
        self.assertEqual(root["attributes"]["ID"], ["31", "32", "33"])
        self.assertEqual(root["source_attributes"]["type"], ["original", "another"])

    def test_geometry_is_not_converted_to_screen_bounds(self):
        root = parse_arkui(
            "|-> Row childSize:0\n"
            "      FrameRect: RectT (-8.00, 2.00) - [10.00 x 12.00]\n"
            "      AccessibilityId: 812\n"
            "      PaintRect without transform: RectT (1, 2) - [3 x 4]\n"
        )
        self.assertEqual(root["attributes"]["FrameRect"], "RectT (-8.00, 2.00) - [10.00 x 12.00]")
        self.assertEqual(root["attributes"]["AccessibilityId"], "812")
        self.assertNotIn("bounds", root["attributes"])

    def test_marker_inside_property_is_not_a_node(self):
        root = parse_arkui("|-> Text childSize:0\n      Content: |-> fake childSize:0\n")
        self.assertEqual(count_arkui_nodes(root), 1)
        self.assertEqual(root["attributes"]["Content"], "|-> fake childSize:0")

    def test_preamble_and_nonzero_root_indent(self):
        root = parse_arkui("WindowName: anonymous\nWinId: 20\n  |-> root childSize:1\n    |-> Text childSize:0")
        self.assertEqual(root["source_line"], 3)
        self.assertEqual(count_arkui_nodes(root), 2)
        self.assertEqual(root["children"][0]["raw"], "")

    def test_forest_preserves_both_official_root_styles_and_global_source_lines(self):
        text = ("WinId: 5\r\nPid: 200\r\nbundleName:com.example.system\r\n"
                "  |-> root childSize:1\r\n    | ID: 0\r\n"
                "    |-> WindowScene childSize:0\r\n        ID: 90\r\n        compid: session9\r\n"
                "  |-> root childSize:0\r\n      ID: 10602\r\n      compid: \r\n"
                "      Payload: {\r\n        nested value\r\n      }\r\n")
        roots = parse_forest(text)
        self.assertEqual(len(roots), 2)
        self.assertEqual([root["attributes"]["ID"] for root in roots], ["0", "10602"])
        self.assertEqual([root["source_line"] for root in roots], [4, 9])
        self.assertEqual([root["declared_child_count"] for root in roots], [1, 0])
        self.assertEqual(sum(count_arkui_nodes(root) for root in roots), 3)
        self.assertEqual(roots[0]["children"][0]["source_line"], 6)
        self.assertEqual(roots[0]["children"][0]["raw"], "        ID: 90\r\n        compid: session9\r\n")
        self.assertEqual(roots[1]["raw"], "      ID: 10602\r\n      compid: \r\n"
                         "      Payload: {\r\n        nested value\r\n      }\r\n")
        self.assertEqual(roots[1]["children"], [])

    def test_single_window_parser_still_rejects_a_valid_forest(self):
        text = "  |-> root childSize:0\n      ID: 0\n  |-> root childSize:0\n      ID: 10\n"
        self.assertEqual(len(parse_forest(text)), 2)
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "exactly one component tree"):
            parse_arkui(text)

    def test_forest_rejects_malformed_later_roots_and_descendants_without_returning_a_prefix(self):
        prefix = "  |-> root childSize:0\n      ID: 0\n"
        tails = {
            "lower root indent": " |-> root childSize:0\n",
            "broken node count": "  |-> root childSize:no\n",
            "tab root indent": "\t|-> root childSize:0\n",
            "skipped later child": "  |-> root childSize:1\n      |-> Text childSize:0\n",
            "odd later child": "  |-> root childSize:1\n     |-> Text childSize:0\n",
            "unmatched later dedent": "  |-> root childSize:1\n    |-> Row childSize:0\n   |-> Text childSize:0\n",
        }
        for label, tail in tails.items():
            with self.subTest(label=label), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                parse_forest(prefix + tail)

    def test_invalid_tree_shapes_are_rejected(self):
        cases = {
            "no tree": "WinId: 7\nUINodeCount: 50\n",
            "multiple roots": "|-> root childSize:0\n|-> root childSize:0\n",
            "skipped depth": "|-> root childSize:1\n    |-> Text childSize:0\n",
            "odd depth": "|-> root childSize:1\n   |-> Text childSize:0\n",
            "unmatched dedent": "|-> root childSize:1\n  |-> Row childSize:1\n |-> Text childSize:0\n",
            "tab indentation": "|-> root childSize:1\n\t|-> Text childSize:0\n",
            "missing count": "|-> root childSize:no\n",
            "missing tag": "|->  childSize:0\n",
            "whitespace tag": "|->   childSize:0\n",
        }
        for label, text in cases.items():
            with self.subTest(label=label), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                parse_arkui(text)


if __name__ == "__main__":
    unittest.main()
