"""@brief Canvas 绘制历史独立保留，不能放宽节点身份、父关系或渲染证据校验。"""

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from har_capture import coordinates
from har_capture.arkui import count_arkui_nodes, node_property_text, parse_arkui, parse_forest
from har_capture.arkui_selection import read_window_arkui, select_native_tree
from har_capture.capture_facts import normalized_arkui
from har_capture.hdc import CaptureError
from har_capture.metadata import artifact_info
from har_capture.scene_evidence import parse_source_node
from har_capture.tree_capture import save_full_arkui_tree
from tests.test_arkui_embedded import host_tree
from tests.test_coordinates import fixture
from tests.test_undefined_image_identity import raw_frame
from tests.test_window_mapping import render_line


DRAW_RECORD = "2026-10-24 07:43:16.902 Canvas Size: [285.000000, 55.000000], Command Size: 46;"


def canvas_document(records=(DRAW_RECORD,), prefix="      ", tag="Canvas"):
    """@brief 生成本层叶节点，标题之后的原文保持官方属性包装。"""
    text = f"|-> {tag},2,12 childSize:0\n{prefix}ID: 2\n{prefix}DrawCmdInfo:\n"
    return text + "".join(prefix + record + "\n" for record in records) + prefix + "Alpha: 1.000000\n"


def canvas_occurrence(indent, description, records):
    """@brief 同一帧在实际父树和断开引用位置仅有历史与遍历说明差异。"""
    body = " " * (indent + 4)
    text = raw_frame(indent, ("Canvas", 2, 12, 0), ("Column", 1, 11),
                     "RectT (5, -10) - [20 x 30]", 102)
    text = text.replace("Description: Children", "Description: " + description)
    history = body + "OpaqueDiagnostic: preserve\n" + body + "DrawCmdInfo:\n"
    history += "".join(body + record + "\n" for record in records)
    return text.replace(body + "CanvasNode[102]", history + body + "CanvasNode[102]")


def canvas_source():
    """@brief 完整森林包含唯一实际 Canvas 和另一个根下的 BrokenChildren 引用。"""
    text = raw_frame(0, ("root", 0, 10, 1), None, "RectT (0, 0) - [200 x 150]", 100)
    text += raw_frame(2, ("Column", 1, 11, 1), ("root", 0, 10), "RectT (10, 20) - [100 x 80]", 101)
    text += canvas_occurrence(4, "Children", (DRAW_RECORD,))
    text += "|-> Stack,50,60 childSize:1\n    ID: 50\n    AccessibilityId: 60\n"
    text += canvas_occurrence(2, "BrokenChildren", ())
    native = copy.deepcopy(fixture()[3]["arkui_geometry"])
    native["$children"][0]["$children"][0]["$type"] = "Canvas"
    return text, native


def canvas_fixture():
    """@brief 复用已存在的窗口和 RS 几何，只将叶节点改为 Canvas。"""
    args = list(fixture())
    text, native = canvas_source()
    args[0] = select_native_tree(parse_forest(text), native)
    args[3]["arkui_geometry"] = native
    return args


def save_bytes(stage, name, text):
    """@brief 以原始字节保存匿名来源，避免平台换行转换影响完整性断言。"""
    path = stage / name
    path.write_bytes(text.encode("utf-8"))
    return artifact_info(stage, path)


def saved_canvas_fixture(stage):
    """@brief 通过生产保存接口建立包含两次原文位置和独立几何的完整索引。"""
    args = canvas_fixture()
    window, canvas, evidence = args[1:]
    window["bundle_name"] = "com.example.canvas"
    text, native = canvas_source()
    native["appInfo"] = {"WindowID": 9, "BundleName": window["bundle_name"]}
    header = "WinId: 9\nPid: 1001\n"
    raw = save_bytes(stage, "application-response.txt", (header + text).replace("\n", "\r\n"))
    geometry = {
        "response": save_bytes(stage, "native-response.txt", header + json.dumps(native)),
        "arkui_geometry": save_bytes(stage, "native.json", json.dumps(native)),
        "render_tree": save_bytes(stage, "render-tree.txt", evidence["render_tree"]),
        "render_detached_tree": save_bytes(stage, "render-detached.txt", evidence["render_detached_tree"]),
    }
    record = save_full_arkui_tree(stage, stage / raw["file"], geometry)
    record.update(response=raw, origin={"kind": "stdout"}, session={"is_system": False})
    window["trees"] = {"arkui": record}
    return window, canvas, {9: evidence["scene"]}


class CanvasDiagnosticsTests(unittest.TestCase):
    def parse_failed(self, text, message="Canvas drawing diagnostic"):
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, message):
            parse_forest(text)

    def select_failed(self, text, native, message="unproven duplicate raw occurrence"):
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, message):
            select_native_tree(parse_forest(text), native)

    def test_history_schema_keeps_complete_raw_and_global_source_lines(self):
        for prefix in ("      ", "    | "):
            with self.subTest(prefix=prefix):
                text = ("anonymous response\n" + canvas_document(prefix=prefix)).replace("\n", "\r\n")
                node = parse_arkui(text)
                diagnostic = node["canvas_diagnostics"]
                self.assertEqual(node["source_line"], 2)
                self.assertEqual(node["raw"], "".join(text.splitlines(keepends=True)[2:]))
                self.assertEqual(diagnostic, {
                    "source_line": 4,
                    "raw": prefix + "DrawCmdInfo:\r\n" + prefix + DRAW_RECORD + "\r\n",
                    "records": [{"source_line": 5, "timestamp": "2026-10-24 07:43:16.902",
                                 "canvas_size": {"width": 285.0, "height": 55.0}, "command_size": 46}],
                })
                for attributes in (node["attributes"], node["source_attributes"]):
                    self.assertNotIn("DrawCmdInfo", attributes)
                    self.assertNotIn("2026-10-24 07", attributes)
                    self.assertEqual(attributes["Alpha"], "1.000000")
                self.assertEqual(node_property_text(node), node["raw"])

    def test_empty_and_ten_record_history_are_preserved_in_source_order(self):
        for count in (0, 10):
            rows = tuple(DRAW_RECORD.replace("Size: 46;", f"Size: {index};") for index in range(count))
            with self.subTest(count=count):
                node = parse_arkui(canvas_document(rows))
                self.assertEqual([row["command_size"] for row in node["canvas_diagnostics"]["records"]],
                                 list(range(count)))
                self.assertEqual(len(node["canvas_diagnostics"]["raw"].splitlines()), count + 1)

    def test_negative_zero_and_finite_signed_dimensions_keep_numeric_values(self):
        row = DRAW_RECORD.replace("285.000000, 55.000000", "-0.000000, -55.250000")
        diagnostic = parse_arkui(canvas_document((row,)))["canvas_diagnostics"]["records"][0]
        self.assertEqual(diagnostic["canvas_size"], {"width": -0.0, "height": -55.25})
        self.assertEqual(math.copysign(1, diagnostic["canvas_size"]["width"]), -1)

    def test_malformed_dates_sizes_commands_and_incomplete_lines_are_rejected(self):
        cases = [DRAW_RECORD.replace("2026-10-24", "2026-02-30"),
                 DRAW_RECORD.replace("07:43:16", "24:43:16"),
                 DRAW_RECORD.replace(".902", ".90"),
                 DRAW_RECORD.replace("285.000000", "285.0"),
                 DRAW_RECORD.replace("285.000000", "+285.000000"),
                 DRAW_RECORD.replace("285.000000", "2.85e2"),
                 DRAW_RECORD.replace("285.000000", "nan"),
                 DRAW_RECORD.replace("285.000000", "inf"),
                 DRAW_RECORD.replace("Size: 46;", "Size: -1;"),
                 DRAW_RECORD.replace("Size: 46;", "Size: 1.0;"),
                 DRAW_RECORD[:-1], DRAW_RECORD + " trailing",
                 "2026-10-24 07:43:16.902 incomplete", "Canvas Size: incomplete", "Command Size: 0;",
                 DRAW_RECORD.replace("285.000000", "9" * 400 + ".000000")]
        for row in cases:
            with self.subTest(row=row[:90]):
                self.parse_failed(canvas_document((row,)))

    def test_duplicate_marker_excess_history_and_wrong_property_prefix_are_rejected(self):
        self.parse_failed(canvas_document((DRAW_RECORD, "DrawCmdInfo:")), "Duplicate Canvas")
        self.parse_failed(canvas_document((DRAW_RECORD,) * 11), "official history limit")
        text = canvas_document().replace("      " + DRAW_RECORD, "     " + DRAW_RECORD)
        self.parse_failed(text, "diagnostic prefix")
        text = canvas_document().replace("      " + DRAW_RECORD, "       " + DRAW_RECORD)
        self.parse_failed(text)

    def test_noncanvas_or_absent_exact_marker_keeps_timestamp_as_a_regular_attribute(self):
        documents = [canvas_document(tag="Text"),
                     canvas_document().replace("      DrawCmdInfo:\n", ""),
                     canvas_document().replace("DrawCmdInfo:", "DrawCmdInfo: unexpected")]
        for text in documents:
            with self.subTest(text=text.splitlines()[0]):
                node = parse_arkui(text)
                self.assertNotIn("canvas_diagnostics", node)
                self.assertIn("2026-10-24 07", node["attributes"])
                self.assertIn(DRAW_RECORD, node["raw"])

    def test_unknown_attributes_and_timestamp_after_block_boundary_are_not_swallowed(self):
        text = canvas_document().replace("      Alpha:", "      Text: Canvas Size: literal\n      Alpha:")
        text += "      UnknownEvidence: Command Size: literal\n      " + DRAW_RECORD + "\n"
        node = parse_arkui(text)
        self.assertEqual(node["attributes"]["Text"], "Canvas Size: literal")
        self.assertEqual(node["source_attributes"]["UnknownEvidence"], "Command Size: literal")
        self.assertIn("2026-10-24 07", node["attributes"])
        self.assertEqual(len(node["canvas_diagnostics"]["records"]), 1)

    def test_nested_ui_extension_scopes_canvas_history_and_preserves_wrapped_crlf(self):
        text = "anonymous response\n" + host_tree(host_tree(canvas_document(), leaf=True), leaf=True)
        text = text.replace("\n", "\r\n")
        outer = parse_arkui(text)
        inner = outer["embedded_trees"][0]["roots"][0]
        canvas = inner["embedded_trees"][0]["roots"][0]
        self.assertNotIn("canvas_diagnostics", outer)
        self.assertNotIn("canvas_diagnostics", inner)
        self.assertNotIn("DrawCmdInfo", outer["attributes"])
        self.assertNotIn("DrawCmdInfo", inner["attributes"])
        marker = next(index for index, line in enumerate(text.splitlines(keepends=True), 1)
                      if "DrawCmdInfo:" in line)
        diagnostic = canvas["canvas_diagnostics"]
        self.assertEqual(diagnostic["source_line"], marker)
        self.assertEqual(diagnostic["records"][0]["source_line"], marker + 1)
        lines = text.splitlines(keepends=True)
        self.assertEqual(diagnostic["raw"], "".join(lines[marker - 1:marker + 1]))
        self.assertIn("\r\n", diagnostic["raw"])
        self.assertIn(diagnostic["raw"], canvas["raw"])
        self.assertNotIn("DrawCmdInfo:", node_property_text(outer))
        self.assertNotIn("DrawCmdInfo:", node_property_text(inner))

    def test_consumed_history_allows_only_the_proven_broken_reference_in_either_forest_order(self):
        text, native = canvas_source()
        main, alias = text.split("|-> Stack,50,60", 1)
        for source in (text, "|-> Stack,50,60" + alias + main):
            with self.subTest(alias_first=source != text):
                roots = parse_forest(source)
                before = copy.deepcopy(roots)
                selected = select_native_tree(roots, native)
                canvas = selected["children"][0]["children"][0]
                self.assertEqual(count_arkui_nodes(selected), 3)
                self.assertEqual(sum(count_arkui_nodes(root) for root in roots), 5)
                self.assertEqual(canvas["attributes"]["Description"], "Children")
                self.assertEqual(len(canvas["canvas_diagnostics"]["records"]), 1)
                self.assertEqual(canvas["source_association"]["native_parent_id"], "1")
                self.assertEqual(roots, before)

    def test_scene_source_relocation_offsets_canvas_and_nested_diagnostic_lines_without_changing_raw(self):
        documents = [canvas_document(), host_tree(host_tree(canvas_document(), leaf=True), leaf=True)]
        for text in documents:
            text = text.replace("\n", "\r\n")
            original = parse_arkui(text)
            relocated = parse_source_node(text.encode("utf-8"), 1709)
            pending = [(original, relocated)]
            while pending:
                source, target = pending.pop()
                self.assertEqual(target["source_line"], source["source_line"] + 1708)
                self.assertEqual(target["raw"], source["raw"])
                self.assertEqual(target["attributes"], source["attributes"])
                if "canvas_diagnostics" in source:
                    old, new = source["canvas_diagnostics"], target["canvas_diagnostics"]
                    self.assertEqual(new["source_line"], old["source_line"] + 1708)
                    self.assertEqual(new["raw"], old["raw"])
                    self.assertEqual(len(new["records"]), len(old["records"]))
                    for before, after in zip(old["records"], new["records"], strict=True):
                        expected = {**before, "source_line": before["source_line"] + 1708}
                        self.assertEqual(after, expected)
                pending.extend(zip(source["children"], target["children"], strict=True))
                for old, new in zip(source.get("embedded_trees", []), target.get("embedded_trees", []), strict=True):
                    self.assertEqual(new["source_line"], old["source_line"] + 1708)
                    self.assertEqual(new["raw"], old["raw"])
                    pending.extend(zip(old["roots"], new["roots"], strict=True))

    def test_broken_reference_geometry_parent_render_unknown_and_child_conflicts_remain_rejected(self):
        text, native = canvas_source()
        main, alias = text.split("|-> Stack,50,60", 1)
        mutations = [alias.replace("FrameRect: RectT (5, -10) - [20 x 30]",
                                   "FrameRect: RectT (5, -10) - [21 x 30]"),
                     alias.replace("Parent: Column,1,11", "Parent: Column,9,11"),
                     alias.replace("LastParentId: 1", "LastParentId: 9"),
                     alias.replace("CanvasNode[102]", "CanvasNode[999]"),
                     alias.replace("OpaqueDiagnostic: preserve", "OpaqueDiagnostic: conflict"),
                     alias.replace("Description: BrokenChildren", "Description: Children"),
                     alias.replace("Canvas,2,12 childSize:0", "Canvas,2,12 childSize:1"),
                     alias + "    |-> Text,3,13 childSize:0\n        ID: 3\n"]
        for index, changed in enumerate(mutations):
            with self.subTest(mutation=index):
                self.select_failed(main + "|-> Stack,50,60" + changed, native,
                                   "duplicate raw occurrence|conflicting render identities")

    def test_real_save_read_normalize_keeps_forest_history_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            window, canvas_size, scenes = saved_canvas_fixture(stage)
            before = {path.name: path.read_bytes() for path in stage.iterdir()}
            record = window["trees"]["arkui"]
            forest = json.loads((stage / record["forest"]["file"]).read_bytes())
            self.assertEqual(record["forest_node_count"], 5)
            self.assertEqual(record["node_count"], 3)
            self.assertEqual(forest[1]["children"][0]["canvas_diagnostics"]["records"], [])
            stored = read_window_arkui(stage, window)
            result = normalized_arkui(stage, window, canvas_size, scenes)["tree"]
            self.assertEqual(result["coordinate_validation"]["status"], "verified")
            self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 3)
            original = stored["children"][0]["children"][0]
            normalized = result["children"][0]["children"][0]
            for key in ("attributes", "source_attributes", "raw", "source_line", "canvas_diagnostics"):
                self.assertEqual(normalized[key], original[key])
            self.assertEqual(normalized["geometry"]["screen_bounds"],
                             {"x": 115, "y": 60, "width": 20, "height": 30})
            self.assertEqual(before, {path.name: path.read_bytes() for path in stage.iterdir()})

    def test_drawing_history_does_not_bypass_independent_native_or_render_evidence(self):
        for conflict in ("native", "parent", "missing_rs", "detached"):
            args = canvas_fixture()
            if conflict == "native":
                args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = "[16,10],[36,40]"
            elif conflict == "parent":
                column = args[3]["arkui_geometry"]["$children"][0]
                args[3]["arkui_geometry"]["$children"].append(column["$children"].pop())
            elif conflict == "missing_rs":
                args[3]["render_tree"] = "".join(row for row in args[3]["render_tree"].splitlines(keepends=True)
                                                 if "CANVAS_NODE[102]" not in row)
            else:
                args[3]["render_detached_tree"] += render_line(0, "CANVAS_NODE", 102, "5 -10 20 30",
                                                              "OtherModifiers:[]")
            with self.subTest(conflict=conflict), self.assertLogs(level="ERROR"):
                result = coordinates.normalize_arkui(*args)
            self.assertEqual(result["coordinate_validation"]["status"], "failed")
            self.assertTrue(result["coordinate_validation"]["issues"])
            self.assertEqual(count_arkui_nodes(result), 3)


if __name__ == "__main__":
    unittest.main()
