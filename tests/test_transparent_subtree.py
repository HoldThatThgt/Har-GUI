"""透明祖先可以阻止子节点准备，但不能替代节点身份、逻辑几何和来源核验。"""

import copy
import unittest

from har_capture.coordinates import normalize_arkui
from tests.test_viewport_geometry import native_rectangle, raw_rectangle, viewport_fixture
from tests.test_window_mapping import component, no_draw_leaf_fixture, render_line


def transparent_fixture(applied=False):
    """匿名保留已准备透明按钮与有绘制命令、正面积且尚未准备的字形。"""
    args = viewport_fixture()
    parent = component(182, "Button", raw_rectangle(453, 561, 91, 91), {"Alpha": "0.000000"})
    child = component(183, "SymbolGlyph", raw_rectangle(23, 23, 46, 46))
    child["attributes"]["FrameRect"] = raw_rectangle(22.5, 22.5, 46, 46)
    child["source_attributes"] = dict(child["attributes"])
    parent["raw"] = ("CanvasNode[182] child[183] lazyLoad[0] nodeState[ACTIVE]\n"
                     "  | Alpha: 0.000000\n")
    child["raw"] = "CanvasNode[183] child[] lazyLoad[0] nodeState[ACTIVE]\n"
    args[0]["children"], parent["children"] = [parent], [child]
    args[3]["arkui_geometry"]["$children"] = [{
        "$ID": 182, "$type": "Button", "$rect": native_rectangle(453, 561, 91, 91), "opacity": 0,
        "$children": [{"$ID": 183, "$type": "SymbolGlyph", "$rect": native_rectangle(476, 584, 46, 46)}]}]
    parent_suffix = ("Alpha[0.000], PrepareSeq: 293, PostPrepareSeq: 268, VsyncId: 2901764, "
                     "IsSubTreeSkipped: 1, hasDrawContent: 0, hasChildrenDrawContent: 0, "
                     "OtherModifiers:[pid:2000->Alpha:[0.0], pid:2000->AlphaOffscreen:[true]], "
                     "sortedChildren: 1(183 )")
    child_suffix = ("PrepareSeq: 0, PostPrepareSeq: 0, VsyncId: 0, IsSubTreeSkipped: 0, "
                    "hasDrawContent: 0, hasChildrenDrawContent: 0, "
                    "DrawCmdModifiers2:[ContentStyle:[PATH_OPITEM: [path]], OverlayStyle:[]], "
                    "OtherModifiers:[pid:2000->Bounds:[x:23.0 y:23.0 z:46.0 w:46.0], "
                    "pid:2000->Frame:[x:23.0 y:23.0 z:46.0 w:46.0]]")
    bounds = "23 23 46 46" if applied else "-inf -inf -inf -inf"
    if applied:
        child_suffix = child_suffix.replace("PrepareSeq: 0", "PrepareSeq: 292")
        child_suffix = child_suffix.replace("VsyncId: 0", "VsyncId: 2901764")
    rows = args[3]["render_tree"].splitlines(keepends=True)[:5]
    args[3]["render_tree"] = "".join(rows) + render_line(5, "CANVAS_NODE", 182, "453 561 91 91", parent_suffix)
    args[3]["render_tree"] += render_line(6, "CANVAS_NODE", 183, bounds, child_suffix)
    return args


def rewrite_render(args, render_id, mutate):
    """只修改指定真实层级的匿名渲染节点行。"""
    rows = args[3]["render_tree"].splitlines(keepends=True)
    matches = [index for index, row in enumerate(rows) if f"CANVAS_NODE[{render_id}]," in row]
    if len(matches) != 1:
        raise AssertionError("Synthetic render mutation requires one exact source node")
    original = rows[matches[0]]
    rows[matches[0]] = mutate(original)
    if rows[matches[0]] == original:
        raise AssertionError("Synthetic render mutation did not change its target")
    args[3]["render_tree"] = "".join(rows)


def raw_alpha(args, value):
    """使匿名属性和原文同时表达同一项缺失、重复或冲突的透明度证据。"""
    parent = args[0]["children"][0]
    for attributes in (parent["attributes"], parent["source_attributes"]):
        if value is None:
            attributes.pop("Alpha", None)
        else:
            attributes["Alpha"] = value
    values = [] if value is None else value if isinstance(value, list) else [value]
    parent["raw"] = parent["raw"].split("\n", 1)[0] + "\n"
    parent["raw"] += "".join(f"  | Alpha: {entry}\n" for entry in values)


def add_render_body(row, location, text):
    """将同名文字放在绘制内容或修饰器正文，不能充当官方节点头部字段。"""
    if "OtherModifiers:[" not in row:
        raise AssertionError("Synthetic render body insertion source is missing")
    if location == "drawing":
        replacement = f"DrawCmdModifiers2:[ContentStyle:[TEXT_OPITEM:[{text}]]], OtherModifiers:["
    elif location == "modifier":
        replacement = f"OtherModifiers:[Body:[{text}], "
    else:
        raise AssertionError("Unknown synthetic render body location")
    return row.replace("OtherModifiers:[", replacement, 1)


class TransparentSubtreeTests(unittest.TestCase):
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

    def assert_transparent(self, node, state):
        geometry = node["geometry"]
        self.assertFalse(geometry["rendered"])
        self.assertEqual(geometry["render_state"], "not_rendered")
        self.assertEqual(geometry["render_state_source"], "RenderService.zero_alpha_ancestor")
        self.assertEqual(geometry["source"]["zero_alpha_ancestor"], {"node_id": "182", "render_node_id": "182"})
        self.assertEqual(geometry["source"]["render_geometry_state"], state)
        self.assertEqual(geometry["source"]["no_draw_render_node_ids"], [])
        self.assertEqual(geometry["source"]["no_draw_detached_render_node_ids"], [])

    def test_positive_unprepared_glyph_preserves_logical_box_and_commands_without_claiming_no_content(self):
        args = transparent_fixture()
        original = copy.deepcopy(args)
        result = self.verified(args)
        parent, child = result["children"][0], result["children"][0]["children"][0]
        self.assertEqual(result["coordinate_validation"]["frame_nodes"], 3)
        self.assertEqual(result["coordinate_validation"]["verified_frame_nodes"], 3)
        self.assert_transparent(parent, "applied")
        self.assert_transparent(child, "unprepared")
        self.assertEqual(child["attributes"]["FrameRect"], raw_rectangle(22.5, 22.5, 46, 46))
        self.assertEqual(child["geometry"]["native_window_rect"], native_rectangle(476, 584, 46, 46))
        self.assertEqual(child["geometry"]["screen_bounds"], {"x": 1022, "y": 788, "width": 46, "height": 46})
        self.assertEqual(child["geometry"]["rectangular_clip_bounds"], child["geometry"]["screen_bounds"])
        self.assertIn("PATH_OPITEM", args[3]["render_tree"])
        self.assertEqual(args, original)

    def test_applied_descendant_remains_transparent_and_keeps_current_geometry_validation(self):
        result = self.verified(transparent_fixture(applied=True))
        parent = result["children"][0]
        self.assert_transparent(parent, "applied")
        self.assert_transparent(parent["children"][0], "applied")
        self.assertTrue(result["geometry"]["rendered"])
        self.assertNotIn("zero_alpha_ancestor", result["geometry"]["source"])

    def test_finite_ancestor_rectangular_clip_is_not_confused_with_transparency(self):
        args = transparent_fixture()
        rewrite_render(args, 182, lambda row: row.replace("Alpha[0.000]", "ClipToFrame[True], Alpha[0.000]")
                       .replace("pid:2000->Alpha:[0.0]", "ClipToFrame:[true], pid:2000->Alpha:[0.0]"))
        child = self.verified(args)["children"][0]["children"][0]
        self.assert_transparent(child, "unprepared")
        clip = next(item for item in child["geometry"]["clip_chain"] if item.get("render_node_id") == "182")
        self.assertEqual(clip["mode"], "frame")
        self.assertEqual(clip["bounds"], {"x": 999, "y": 765, "width": 91, "height": 91})

    def test_native_zero_opacity_is_required_as_a_typed_finite_value(self):
        for value in (None, False, "0", 0.00001, 1, float("nan")):
            args = transparent_fixture()
            native = args[3]["arkui_geometry"]["$children"][0]
            if value is None:
                native.pop("opacity")
            else:
                native["opacity"] = value
            with self.subTest(value=value):
                self.assert_failed(args)

    def test_raw_alpha_must_be_present_unique_and_exact_zero(self):
        for value in (None, "0.000001", "0.1", "nan", ["0.000000", "0.000000"], ["0.000000", "1"]):
            args = transparent_fixture()
            raw_alpha(args, value)
            with self.subTest(value=value):
                self.assert_failed(args)

    def test_current_alpha_must_be_present_unique_and_zero_independently_of_pending_modifier(self):
        for value in ("", "Alpha[0.001],", "Alpha[0.000001],", "Alpha[nan],",
                      "Alpha[0.000], Alpha[0.000],", "Alpha[0.000], Alpha[1.000],"):
            args = transparent_fixture()
            rewrite_render(args, 182, lambda row: row.replace("Alpha[0.000],", value))
            with self.subTest(value=value):
                self.assert_failed(args)

    def test_pending_alpha_must_be_present_unique_and_zero_independently_of_current_properties(self):
        for value in ("", "pid:2000->Alpha:[0.001],", "pid:2000->Alpha:[nan],",
                      "pid:2000->Alpha:[0.0], pid:2000->Alpha:[0.0],",
                      "pid:2000->Alpha:[0.0], pid:2000->Alpha:[1.0],"):
            args = transparent_fixture()
            rewrite_render(args, 182, lambda row: row.replace("pid:2000->Alpha:[0.0],", value))
            with self.subTest(value=value):
                self.assert_failed(args)

    def test_zero_printed_alpha_does_not_replace_a_nonzero_native_value(self):
        args = transparent_fixture()
        args[3]["arkui_geometry"]["$children"][0]["opacity"] = 0.0000001
        self.assert_failed(args)

    def test_raw_alpha_difference_warning_cannot_supply_agreed_zero_opacity(self):
        args = transparent_fixture()
        parent = args[0]["children"][0]
        parent["attributes"]["Alpha has difference,arkui"] = "0.000000"
        parent["source_attributes"]["Alpha has difference,arkui"] = "0.000000"
        parent["raw"] += "  | Alpha has difference,arkui:0.000000\n"
        self.assert_failed(args)

    def test_transparent_ancestor_requires_unambiguous_prepare_vsync_and_skip_observation(self):
        replacements = []
        for name, original in (("PrepareSeq", 293), ("PostPrepareSeq", 268), ("VsyncId", 2901764)):
            old = f"{name}: {original},"
            replacements.extend(((old, ""), (old, f"{name}: 0,"), (old, old + " " + old)))
        replacements.extend((("IsSubTreeSkipped: 1,", ""), ("IsSubTreeSkipped: 1,", "IsSubTreeSkipped: 0,"),
                             ("IsSubTreeSkipped: 1,", "IsSubTreeSkipped: 1, IsSubTreeSkipped: 1,")))
        for old, new in replacements:
            args = transparent_fixture()
            rewrite_render(args, 182, lambda row: row.replace(old, new))
            with self.subTest(old=old, new=new):
                self.assert_failed(args)

    def test_unprepared_geometry_needs_zero_prepare_postprepare_and_vsync_evidence(self):
        for field in ("PrepareSeq", "PostPrepareSeq", "VsyncId"):
            for value in (None, "1", "0, " + field + ": 0"):
                args = transparent_fixture()
                replacement = "" if value is None else f"{field}: {value},"
                rewrite_render(args, 183, lambda row: row.replace(f"{field}: 0,", replacement, 1))
                with self.subTest(field=field, value=value):
                    self.assert_failed(args)

    def test_unprepared_positive_geometry_is_not_accepted_from_its_own_dual_zero_flags(self):
        args = transparent_fixture()
        raw_alpha(args, None)
        args[3]["arkui_geometry"]["$children"][0].pop("opacity")
        rewrite_render(args, 182, lambda row: row.replace("Alpha[0.000],", "")
                       .replace("pid:2000->Alpha:[0.0], ", ""))
        self.assert_failed(args)

    def test_shared_transition_or_recorded_animation_blocks_the_transparent_ancestor_proof(self):
        for render_id in (182, 183):
            for marker in ("SharedTransitionParam: [182 -> 999]", "RSAnimationManager: [animation]",
                           "animationInfo: [active]", "animation:1"):
                args = transparent_fixture()
                rewrite_render(args, render_id, lambda row: row.rstrip("\n") + ", " + marker + "\n")
                with self.subTest(render_id=render_id, marker=marker):
                    self.assert_failed(args)

    def test_actual_render_parent_must_contain_the_proven_transparent_ancestor(self):
        args = transparent_fixture()
        rewrite_render(args, 183, lambda row: row[2:])
        self.assert_failed(args)

    def test_native_parent_and_component_identity_remain_independently_required(self):
        for corruption in ("parent", "type"):
            args = transparent_fixture()
            native = args[3]["arkui_geometry"]
            parent = native["$children"][0]
            if corruption == "parent":
                native["$children"].append(parent["$children"].pop())
            else:
                parent["$children"][0]["$type"] = "Text"
            with self.subTest(corruption=corruption):
                self.assert_failed(args, "native_geometry_")

    def test_unprepared_native_origin_and_size_still_require_raw_projection(self):
        for box in ((477, 584, 46, 46), (476, 584, 47, 46), (476, 585, 46, 46)):
            args = transparent_fixture()
            args[3]["arkui_geometry"]["$children"][0]["$children"][0]["$rect"] = native_rectangle(*box)
            with self.subTest(box=box):
                self.assert_failed(args, "native_geometry_local_projection_mismatch")

    def test_pending_bounds_and_frame_must_each_match_the_raw_local_box(self):
        for name in ("Bounds", "Frame"):
            for value in (None, "x:24.0 y:23.0 z:46.0 w:46.0", "x:23.0 y:23.0 z:47.0 w:46.0",
                          "x:23.0 y:23.0 z:46.0 w:46.0], pid:2000->" + name + ":[x:23 y:23 z:46 w:46"):
                args = transparent_fixture()
                old = f"pid:2000->{name}:[x:23.0 y:23.0 z:46.0 w:46.0]"
                replacement = "" if value is None else f"pid:2000->{name}:[{value}]"
                rewrite_render(args, 183, lambda row: row.replace(old, replacement))
                with self.subTest(name=name, value=value):
                    self.assert_failed(args)

    def test_pending_geometry_must_not_be_bound_to_a_foreign_process(self):
        args = transparent_fixture()
        rewrite_render(args, 183, lambda row: row.replace("pid:2000->", "pid:2001->"))
        self.assert_failed(args)

    def test_pending_ancestor_alpha_must_not_be_bound_to_a_foreign_process(self):
        args = transparent_fixture()
        rewrite_render(args, 182, lambda row: row.replace("pid:2000->Alpha:[0.0]", "pid:2001->Alpha:[0.0]"))
        self.assert_failed(args)

    def test_preparation_proof_requires_unique_dual_zero_flags_for_ancestor_and_unprepared_child(self):
        for render_id in (182, 183):
            for field in ("hasDrawContent", "hasChildrenDrawContent"):
                for replacement in ("", f"{field}: 1,", f"{field}: 0, {field}: 0,",
                                    f"{field}: 0, {field}: 1,"):
                    args = transparent_fixture()
                    rewrite_render(args, render_id, lambda row: row.replace(f"{field}: 0,", replacement))
                    with self.subTest(render_id=render_id, field=field, replacement=replacement):
                        self.assert_failed(args)

    def test_explicitly_disabled_unprepared_clips_do_not_invent_current_clip_geometry(self):
        args = transparent_fixture()
        rewrite_render(args, 183, lambda row: row.replace("OtherModifiers:[",
                       "ClipToBounds[False], ClipToFrame[False], OtherModifiers:["
                       "ClipToBounds:[false], ClipToFrame:[false], "))
        child = self.verified(args)["children"][0]["children"][0]
        self.assert_transparent(child, "unprepared")
        self.assertFalse(any(item.get("render_node_id") == "183" for item in child["geometry"]["clip_chain"]))

    def test_transparency_does_not_hide_conflicting_applied_child_or_parent_geometry(self):
        for render_id, old, new in ((183, "23 23 46 46", "24 23 46 46"),
                                   (182, "453 561 91 91", "454 561 91 91")):
            args = transparent_fixture(applied=True)
            rewrite_render(args, render_id, lambda row: row.replace(old, new))
            with self.subTest(render_id=render_id):
                self.assert_failed(args, "scene_render_bounds_mismatch")

    def test_partial_nonfinite_or_mixed_applied_and_unprepared_boxes_remain_invalid(self):
        for bounds, frame in (("-inf 23 46 46", "-inf -inf -inf -inf"),
                              ("-inf -inf -inf -inf", "23 23 46 46"),
                              ("23 23 46 46", "-inf -inf -inf -inf")):
            args = transparent_fixture()
            rewrite_render(args, 183, lambda row: row.replace(
                "Bounds[-inf -inf -inf -inf] Frame[-inf -inf -inf -inf]", f"Bounds[{bounds}] Frame[{frame}]"))
            with self.subTest(bounds=bounds, frame=frame):
                self.assert_failed(args)

    def test_unknown_transform_and_clip_modifiers_cannot_be_hidden_by_zero_opacity(self):
        for render_id in (182, 183):
            for modifier in ("Rotation:[5]", "CustomClip:[0 0 46 46]", "Scale:[x:2 y:1]"):
                args = transparent_fixture()
                rewrite_render(args, render_id, lambda row: row.replace("OtherModifiers:[",
                               "OtherModifiers:[" + modifier + ", "))
                with self.subTest(render_id=render_id, modifier=modifier):
                    self.assert_failed(args)

    def test_unprepared_enabled_rectangular_clip_cannot_use_pending_box_as_current_clip(self):
        for name in ("ClipToBounds", "ClipToFrame"):
            args = transparent_fixture()
            rewrite_render(args, 183, lambda row: row.replace("OtherModifiers:[",
                           f"{name}[True], OtherModifiers:[{name}:[true], "))
            with self.subTest(name=name):
                self.assert_failed(args)

    def test_duplicate_current_bounds_or_frame_cannot_hide_a_conflicting_applied_box(self):
        for extra in ("Bounds[-inf -inf -inf -inf]", "Frame[-inf -inf -inf -inf]",
                      "Bounds[24 23 46 46] Frame[24 23 46 46]"):
            args = transparent_fixture()
            rewrite_render(args, 183, lambda row: row.replace("Frame[-inf -inf -inf -inf],",
                           f"Frame[-inf -inf -inf -inf], {extra},", 1))
            with self.subTest(extra=extra):
                self.assert_failed(args)

    def test_drawing_property_and_header_words_do_not_change_valid_current_properties_or_raw_content(self):
        args = transparent_fixture()
        text = ("Properties: Bounds[24 23 46 46] Frame[24 23 46 46] Alpha[1.000] "
                "PrepareSeq: 17, PostPrepareSeq: 18, VsyncId: 19, "
                "hasDrawContent: 1, hasChildrenDrawContent: 1,")
        rewrite_render(args, 183, lambda row: add_render_body(row, "drawing", text))
        original = copy.deepcopy(args)
        child = self.verified(args)["children"][0]["children"][0]
        self.assert_transparent(child, "unprepared")
        self.assertIn(text, args[3]["render_tree"])
        self.assertEqual(args, original)

    def test_prepare_fields_in_drawing_or_modifier_body_cannot_replace_missing_official_header(self):
        for render_id, values in ((182, (293, 268, 2901764)), (183, (0, 0, 0))):
            for field, value in zip(("PrepareSeq", "PostPrepareSeq", "VsyncId"), values, strict=True):
                for location in ("drawing", "modifier"):
                    args = transparent_fixture()
                    marker = f"{field}: {value},"
                    def mutate(row):
                        if marker not in row.partition("Properties:")[0]:
                            raise AssertionError("Synthetic official header mutation source is missing")
                        return add_render_body(row.replace(marker, "", 1), location, marker)
                    rewrite_render(args, render_id, mutate)
                    with self.subTest(render_id=render_id, field=field, location=location):
                        self.assert_failed(args)

    def test_draw_flags_in_drawing_or_modifier_body_cannot_replace_transparency_header_proof(self):
        for render_id in (182, 183):
            for field in ("hasDrawContent", "hasChildrenDrawContent"):
                for location in ("drawing", "modifier"):
                    args = transparent_fixture()
                    marker = f"{field}: 0,"
                    def mutate(row):
                        if marker not in row.partition("Properties:")[0]:
                            raise AssertionError("Synthetic official header mutation source is missing")
                        return add_render_body(row.replace(marker, "", 1), location, marker)
                    rewrite_render(args, render_id, mutate)
                    with self.subTest(render_id=render_id, field=field, location=location):
                        self.assert_failed(args)

    def test_draw_body_cannot_supply_missing_current_alpha_or_geometry(self):
        for render_id, marker, text in ((182, "Alpha[0.000],", "Alpha[0.000]"),
                                        (183, "Bounds[-inf -inf -inf -inf]", "Bounds[-inf -inf -inf -inf]"),
                                        (183, "Frame[-inf -inf -inf -inf]", "Frame[-inf -inf -inf -inf]")):
            args = transparent_fixture()
            rewrite_render(args, render_id, lambda row: add_render_body(row.replace(marker, "", 1), "drawing", text))
            with self.subTest(render_id=render_id, marker=marker):
                self.assert_failed(args)

    def test_empty_leaf_no_draw_proof_also_requires_real_header_flags(self):
        for field in ("hasDrawContent", "hasChildrenDrawContent"):
            for location in ("drawing", "modifier"):
                args = no_draw_leaf_fixture()
                marker = f"{field}: 0,"
                def mutate(row):
                    if marker not in row.partition("Properties:")[0]:
                        raise AssertionError("Synthetic official header mutation source is missing")
                    return add_render_body(row.replace(marker, "", 1), location, marker)
                rewrite_render(args, 104, mutate)
                with self.subTest(field=field, location=location):
                    self.assert_failed(args)


if __name__ == "__main__":
    unittest.main()
