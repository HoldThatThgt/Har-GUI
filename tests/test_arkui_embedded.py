"""UIExtension 的独立响应保留完整来源，不能污染宿主属性或本地子节点。"""

import copy
import unittest

from har_capture.arkui import count_arkui_nodes, node_property_text, parse_arkui, parse_forest


def remote_tree():
    """构造包含与宿主重复 ID 的独立匿名远端树，叶属性遵循官方无竖线格式。"""
    return ("|-> root, 0, 0 childSize:1\n"
            "    | ID: 0\n"
            "    | CanvasNode[2000] child[2001] lazyLoad[0] nodeState[ACTIVE]\n"
            "  |-> Text, 104, 12 childSize:0\n"
            "        ID: 104\n"
            "        Alpha: 1.000000\n"
            "        CanvasNode[2001] child[] lazyLoad[0] nodeState[ACTIVE]\n")


def host_tree(remote=None, leaf=False):
    """按官方逐行 AddDesc 包装远端响应，随后恢复宿主 RenderService 属性。"""
    prefix = "      " if leaf else "    | "
    remote = remote_tree() if remote is None else remote
    lines = [f"|-> UIExtensionComponent, 104, 102 childSize:{0 if leaf else 1}\n",
             prefix + "ID: 104\n", prefix + "Alpha: 0.000000\n",
             prefix + "UI Extension info: \n", prefix + "bundleName: app.remote.fixture\n",
             prefix + "UINodeCount: 2\n"]
    lines.extend(prefix + line for line in remote.splitlines(keepends=True))
    lines.extend([prefix + "------------start print rsNode\n",
                  prefix + "CanvasNode[1000] child[] lazyLoad[0] nodeState[ACTIVE]\n",
                  prefix + " ShadowRadius: -1.000000\n"])
    if not leaf:
        lines.extend(["  |-> Surface, 105, 103 childSize:0\n", "        ID: 105\n"])
    return "".join(lines)


class ArkuiEmbeddedTests(unittest.TestCase):
    def assert_failed(self, text, message):
        with self.assertLogs(level="ERROR"):
            with self.assertRaisesRegex(RuntimeError, message):
                parse_forest(text)

    def test_wrapped_remote_forest_keeps_full_source_and_separates_host_identity(self):
        text = host_tree()
        host = parse_arkui(text)
        self.assertEqual(host["attributes"]["ID"], "104")
        self.assertEqual(host["source_attributes"]["ID"], "104")
        self.assertEqual(host["attributes"]["Alpha"], "0.000000")
        self.assertNotIn("bundleName", host["attributes"])
        self.assertNotIn("UINodeCount", host["source_attributes"])
        self.assertEqual(host["attributes"]["type"], "UIExtensionComponent, 104, 102")
        self.assertEqual([child["attributes"]["ID"] for child in host["children"]], ["105"])
        self.assertEqual(count_arkui_nodes(host), 2)
        block = host["embedded_trees"][0]
        remote = block["roots"][0]
        self.assertIn("UI Extension info: \n", block["raw"])
        self.assertIn("bundleName: app.remote.fixture\n", block["raw"])
        self.assertEqual(remote["attributes"]["type"], "root, 0, 0")
        self.assertEqual(remote["attributes"]["ID"], "0")
        self.assertEqual(remote["children"][0]["attributes"]["ID"], "104")
        self.assertEqual(remote["children"][0]["attributes"]["Alpha"], "1.000000")
        self.assertEqual(count_arkui_nodes(remote), 2)
        self.assertEqual(host["raw"], text[text.index("\n") + 1:text.index("  |-> Surface")])

    def test_property_reader_excludes_remote_client_nodes_and_alpha_without_changing_any_original(self):
        host = parse_arkui(host_tree())
        original = copy.deepcopy(host)
        own = node_property_text(host)
        self.assertIn("CanvasNode[1000]", own)
        self.assertIn("Alpha: 0.000000", own)
        self.assertNotIn("CanvasNode[2000]", own)
        self.assertNotIn("CanvasNode[2001]", own)
        self.assertNotIn("Alpha: 1.000000", own)
        self.assertNotIn("bundleName", own)
        self.assertEqual(host, original)

    def test_leaf_host_has_space_only_wrapper_and_remote_headers_do_not_become_local_children(self):
        text = host_tree(leaf=True)
        host = parse_arkui(text)
        self.assertEqual(host["children"], [])
        self.assertEqual(host["declared_child_count"], 0)
        self.assertEqual(host["attributes"]["ID"], "104")
        self.assertEqual(host["embedded_trees"][0]["roots"][0]["attributes"]["ID"], "0")
        self.assertNotIn("CanvasNode[2001]", node_property_text(host))
        self.assertEqual(host["raw"], text.split("\n", 1)[1])

    def test_crlf_originals_and_global_lines_survive_wrapping_and_nested_nodes(self):
        text = "response prelude\n" + host_tree()
        text = text.replace("\n", "\r\n")
        host = parse_arkui(text)
        block = host["embedded_trees"][0]
        root = block["roots"][0]
        lines = text.splitlines(keepends=True)
        marker = next(i for i, line in enumerate(lines, 1) if "UI Extension info:" in line)
        header = next(i for i, line in enumerate(lines, 1) if "| |-> root, 0, 0" in line)
        self.assertEqual(host["source_line"], 2)
        self.assertEqual(block["source_line"], marker)
        self.assertEqual(root["source_line"], header)
        self.assertEqual(root["children"][0]["source_line"], header + 3)
        self.assertEqual(block["raw"], "".join(lines[marker - 1:marker - 1 + len(block["raw"].splitlines())]))
        self.assertIn("\r\n", root["raw"])
        self.assertTrue(all(line.endswith("\r\n") for line in root["raw"].splitlines(keepends=True)))
        self.assertIn("    |     | ID: 0\r\n", root["raw"])

    def test_multiple_roots_in_one_remote_response_preserve_their_order_and_scope(self):
        remote = remote_tree() + "|-> Detached, 50, 60 childSize:0\n      ID: 50\n"
        host = parse_arkui(host_tree(remote))
        roots = host["embedded_trees"][0]["roots"]
        self.assertEqual([root["attributes"]["ID"] for root in roots], ["0", "50"])
        self.assertEqual(host["attributes"]["ID"], "104")
        self.assertEqual(len(host["children"]), 1)

    def test_multiple_remote_responses_keep_their_own_preludes_and_source_ranges(self):
        first = host_tree()
        marker = "    | ------------start print rsNode\n"
        second = ("    | UI Extension info: \n    | bundleName: app.second.fixture\n"
                  "    | |-> Second, 60, 61 childSize:0\n    |       ID: 60\n")
        self.assertIn(marker, first)
        host = parse_arkui(first.replace(marker, second + marker))
        blocks = host["embedded_trees"]
        self.assertEqual(len(blocks), 2)
        self.assertEqual([block["roots"][0]["attributes"]["ID"] for block in blocks], ["0", "60"])
        self.assertIn("app.remote.fixture", blocks[0]["raw"])
        self.assertNotIn("app.second.fixture", blocks[0]["raw"])
        self.assertIn("app.second.fixture", blocks[1]["raw"])
        self.assertNotIn("bundleName", node_property_text(host))

    def test_nested_remote_ui_extension_has_independent_originals_and_local_property_views(self):
        host = parse_arkui(host_tree(host_tree(leaf=True)))
        remote_host = host["embedded_trees"][0]["roots"][0]
        inner = remote_host["embedded_trees"][0]["roots"][0]
        self.assertEqual(host["attributes"]["ID"], "104")
        self.assertEqual(remote_host["attributes"]["ID"], "104")
        self.assertEqual(inner["attributes"]["ID"], "0")
        self.assertEqual(remote_host["children"], [])
        self.assertIn("CanvasNode[1000]", node_property_text(remote_host))
        self.assertNotIn("CanvasNode[2001]", node_property_text(remote_host))
        self.assertIn("    |       |-> root, 0, 0", remote_host["raw"])
        self.assertEqual(len(host["children"]), 1)

    def test_remote_response_can_end_at_host_end_and_keeps_unbarred_leaf_properties(self):
        text = host_tree(leaf=True)
        text = text[:text.index("      ------------start print rsNode")]
        host = parse_arkui(text)
        leaf = host["embedded_trees"][0]["roots"][0]["children"][0]
        self.assertEqual(leaf["attributes"]["ID"], "104")
        self.assertEqual(leaf["children"], [])
        self.assertEqual(node_property_text(host), "      ID: 104\n      Alpha: 0.000000\n")

    def test_diagnostic_only_remote_response_is_explicitly_preserved_as_no_component_tree(self):
        host = parse_arkui(host_tree("diagnostic: remote window unavailable\n"))
        block = host["embedded_trees"][0]
        self.assertEqual(block["roots"], [])
        self.assertIn("remote window unavailable", block["raw"])
        self.assertNotIn("diagnostic", host["attributes"])
        self.assertIn("CanvasNode[1000]", node_property_text(host))

    def test_node_without_embedded_response_has_an_unchanged_property_view(self):
        root = parse_arkui("|-> Text childSize:0\n      ID: 1\n      Alpha: 0.500000\n")
        self.assertEqual(node_property_text(root), root["raw"])
        root["embedded_trees"] = []
        self.assertEqual(node_property_text(root), root["raw"])

    def test_unmarked_remote_header_is_rejected_instead_of_swallowing_remote_attributes(self):
        text = host_tree().replace("    | UI Extension info: \n", "")
        self.assert_failed(text, "no UI Extension info marker")

    def test_malformed_remote_header_or_child_indent_remains_a_hard_parse_error(self):
        for original, replacement, error in (
            ("|-> root, 0, 0 childSize:1", "|-> root, 0, 0 childSize:invalid", "Malformed"),
            ("  |-> Text, 104, 12 childSize:0", "   |-> Text, 104, 12 childSize:0", "indentation"),
            ("|-> root, 0, 0 childSize:1", "\t|-> root, 0, 0 childSize:1", "Malformed"),
        ):
            remote = remote_tree()
            self.assertIn(original, remote)
            with self.subTest(replacement=replacement):
                self.assert_failed(host_tree(remote.replace(original, replacement)), error)

    def test_embedded_property_reader_rejects_source_range_and_raw_conflicts(self):
        for mutation in ("line", "raw", "overlap"):
            host = parse_arkui(host_tree())
            if mutation == "line":
                host["embedded_trees"][0]["source_line"] += 1
            elif mutation == "raw":
                host["embedded_trees"][0]["raw"] += "changed\n"
            else:
                host["embedded_trees"].append(copy.deepcopy(host["embedded_trees"][0]))
            with self.subTest(mutation=mutation), self.assertLogs(level="ERROR"):
                with self.assertRaisesRegex(RuntimeError, "source range"):
                    node_property_text(host)


if __name__ == "__main__":
    unittest.main()
