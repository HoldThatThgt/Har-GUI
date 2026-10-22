"""验证显式系统叶子动态范围、严格像素边界与项目规则绑定。"""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from har_capture.capture_checks import compare_capture_states, compare_screen_pixels, verify_pixel_receipt
from har_capture.collector import capture
from har_capture.delivery_operations import prepare_delivery, verify_capture_evidence
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from har_capture.stability import resolve_stability, validate_rules, verify_project_rules
from tests.support import (fake_hdc, layout_data, png_bytes, raw_accessibility, raw_layout,
                           raw_system_arkui, system_session_details)


RULES = [{"bundle_name": "system.test", "resource_id": "clock"}]


def leaf(kind="Text", bounds="[2,2][5,5]"):
    return {"attributes": {"type": kind, "id": "clock", "hostWindowId": "92", "displayId": "0",
                           "bounds": bounds, "text": "12:00", "originalText": "12:00"}, "children": []}


def synthetic_states():
    node = {"attributes": {"bundleName": "system.test", "hostWindowId": "92"}, "children": [leaf()]}
    window = {"window_id": 92, "process_id": 202, "display_id": 0, "z_order": 3,
              "bounding_box": {"x": 0, "y": 0, "width": 8, "height": 6}}
    state = {"layout": {"attributes": {}, "children": [node]}, "windows": {92: window}}
    return {"before": state, "after": copy.deepcopy(state)}


def system_proof():
    return {"display": {"id": 0, "width": 8, "height": 6}, "sessions": {92: {
        "window_id": 92, "process_id": 202, "display_id": 0, "is_system": True, "is_visible": True,
        "rs_visible": True, "visibility_state": 0}}}


def pixel_changes(points, size=(8, 6)):
    image = Image.new("RGB", size, "white")
    for point in points:
        image.putpixel(point, (0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def dynamic_capture(output, undeclared_pixel=False):
    layout = layout_data()
    layout["children"][1]["attributes"]["bundleName"] = "system.test"
    layout["children"][1]["children"] = [leaf(bounds="[1100,200][1120,220]")]
    after = copy.deepcopy(layout)
    after["children"][1]["children"][0]["attributes"].update(text="12:01", originalText="12:01")
    point = (1099, 200) if undeclared_pixel else (1100, 200)
    hdc = fake_hdc({"layout": raw_layout(layout), "closing_layout": raw_layout(after),
                   "accessibility": raw_accessibility(layout["children"]), "arkui_92": raw_system_arkui(),
                   "arkui_response_92": raw_system_arkui(),
                   "closing_screen": pixel_changes([point], (3120, 2080))},
                  command_outputs={"session_details": system_session_details()})
    return capture(hdc, output, 0, RULES)


class StabilityTests(unittest.TestCase):
    def test_only_declared_text_changes_are_compared_without_modifying_raw_trees(self):
        states = synthetic_states()
        attrs = states["after"]["layout"]["children"][0]["children"][0]["attributes"]
        attrs.update(text="12:01", originalText="12:01")
        original = copy.deepcopy(states)
        with self.assertRaises(CaptureError):
            compare_capture_states(states["before"], states["after"])
        evidence = resolve_stability(states, system_proof(), RULES)
        result = compare_capture_states(states["before"], states["after"], evidence)
        self.assertEqual(result["status"], "verified")
        self.assertNotEqual(result["checks"]["layout"]["raw_before_sha256"],
                            result["checks"]["layout"]["raw_after_sha256"])
        self.assertEqual(states, original)
        for field, value in (("enabled", "false"), ("accessibilityId", "different")):
            changed = copy.deepcopy(states)
            changed["after"]["layout"]["children"][0]["children"][0]["attributes"][field] = value
            with self.assertRaises(CaptureError):
                compare_capture_states(changed["before"], changed["after"], evidence)

    def test_overlapping_images_are_retained_and_only_declared_pixels_are_ignored(self):
        states = synthetic_states()
        for state in states.values():
            state["layout"]["children"][0]["children"] = [leaf("Image"), leaf("Image")]
        evidence = resolve_stability(states, system_proof(), RULES)
        self.assertEqual(len(evidence["regions"]), 2)
        before = png_bytes()
        after = pixel_changes([(2, 2), (4, 4)])
        self.assertEqual(compare_screen_pixels(before, after)["status"], "failed")
        pixels = compare_screen_pixels(before, after, evidence["regions"])
        self.assertEqual(pixels["raw_changed_pixel_count"], 2)
        self.assertEqual(pixels["changed_pixel_count"], 0)
        self.assertNotEqual(pixels["before_sha256"], pixels["after_sha256"])
        verify_pixel_receipt(pixels, evidence)
        outside = compare_screen_pixels(before, pixel_changes([(5, 4)]), evidence["regions"])
        self.assertEqual(outside["changed_pixel_count"], 1)
        self.assertEqual(outside["status"], "failed")
        with self.assertRaises(CaptureError):
            verify_pixel_receipt(pixels, {**evidence, "regions": []})

    def test_application_container_missing_and_changed_matches_are_rejected(self):
        cases = []
        app = system_proof()
        app["sessions"][92]["is_system"] = False
        cases.append((synthetic_states(), app))
        identity = system_proof()
        identity["sessions"][92]["process_id"] = 999
        cases.append((synthetic_states(), identity))
        for field, value in (("type", "Column"), ("bounds", "[2,2][6,5]"), ("hostWindowId", "93")):
            states = synthetic_states()
            states["after"]["layout"]["children"][0]["children"][0]["attributes"][field] = value
            cases.append((states, system_proof()))
        missing = synthetic_states()
        missing["after"]["layout"]["children"][0]["children"] = []
        cases.append((missing, system_proof()))
        container = synthetic_states()
        container["before"]["layout"]["children"][0]["children"][0]["children"] = [leaf()]
        cases.append((container, system_proof()))
        for states, proof in cases:
            with self.subTest(states=states, proof=proof), self.assertRaises(CaptureError):
                resolve_stability(states, proof, RULES)

    def test_rules_are_explicit_strict_selectors_and_project_must_match(self):
        for invalid in ({}, [{**RULES[0], "bounds": [0, 0, 8, 6]}], RULES * 2, [{**RULES[0], "resource_id": " "}]):
            with self.assertRaises(CaptureError):
                validate_rules(invalid)
        evidence = resolve_stability(synthetic_states(), system_proof(), RULES)
        verify_project_rules({"stability": evidence}, {"stability_regions": RULES})
        with self.assertRaises(CaptureError):
            verify_project_rules({"stability": evidence}, {})

    def test_missing_text_field_and_other_window_content_still_fail(self):
        states = synthetic_states()
        states["after"]["layout"]["children"][0]["children"][0]["attributes"].pop("text")
        evidence = resolve_stability(states, system_proof(), RULES)
        with self.assertRaises(CaptureError):
            compare_capture_states(states["before"], states["after"], evidence)
        states = synthetic_states()
        states["after"]["layout"]["children"][0]["attributes"]["text"] = "Changed parent"
        with self.assertRaises(CaptureError):
            compare_capture_states(states["before"], states["after"], evidence)

    def test_bounds_and_all_matches_must_remain_inside_one_proven_system_window(self):
        for bounds in ("[2,2][2,5]", "[-1,2][5,5]", "[2,2][9,5]"):
            states = synthetic_states()
            for state in states.values():
                state["layout"]["children"][0]["children"][0]["attributes"]["bounds"] = bounds
            with self.assertRaises(CaptureError):
                resolve_stability(states, system_proof(), RULES)
        states = synthetic_states()
        for state in states.values():
            other = copy.deepcopy(state["layout"]["children"][0])
            other["children"][0]["attributes"]["hostWindowId"] = "93"
            state["layout"]["children"].append(other)
        with self.assertRaisesRegex(CaptureError, "or window changed"):
            resolve_stability(states, system_proof(), RULES)


class StabilityCaptureTests(unittest.TestCase):
    def setUp(self):
        from tests.support import synthetic_screen_geometry
        geometry = patch("har_capture.window_geometry.attach_captured_screen_geometry",
                         side_effect=synthetic_screen_geometry)
        geometry.start()
        self.addCleanup(geometry.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)

    def test_capture_and_prepare_keep_one_original_image_and_full_raw_trees(self):
        directory = dynamic_capture(self.output)
        metadata = json.loads((directory / "metadata.json").read_bytes())
        self.assertEqual((directory / "screenshot.png").read_bytes(), png_bytes(size=(3120, 2080)))
        self.assertEqual(len(list(directory.rglob("*.png"))), 1)
        self.assertEqual(metadata["stability"]["rules"], RULES)
        self.assertEqual(metadata["consistency"]["pixels"]["raw_changed_pixel_count"], 1)
        self.assertEqual(json.loads((directory / "closing-layout.json").read_bytes())["children"][1][
            "children"][0]["attributes"]["text"], "12:01")
        verify_capture_evidence(directory, metadata)
        options = {"app": "test", "page_id": "page", "scene": "test", "state_id": "state",
                   "canvas": {"width": 3120, "height": 2080}, "stability_regions": RULES}
        request = {"directory": str(directory), "options": options,
                   "privacy": {"enabled": False, "reason": "Synthetic test image"}}
        with self.assertRaisesRegex(CaptureError, "current project rules"):
            prepare_delivery({**request, "options": {**options, "stability_regions": []}})
        def normalized(evidence, window, canvas, scenes):
            return {"window_id": window["window_id"], "bundle_name": window["bundle_name"], "tree": {}}
        with patch("har_capture.export.load_scene_evidence", return_value={}), \
                patch("har_capture.export.normalized_arkui", side_effect=normalized):
            result = prepare_delivery(request)
        wms = json.loads((directory / "delivery" / result["manifest"]["files"]["wms"]["file"]).read_bytes())
        self.assertEqual(wms["stability"], metadata["stability"])
        self.assertEqual(len(list(directory.rglob("*.png"))), 1)
        self.assertEqual(prepare_delivery(request), result)

    def test_undeclared_pixel_is_rejected_and_no_second_screenshot_is_saved(self):
        with self.assertRaisesRegex(CaptureError, "Screen content changed"):
            dynamic_capture(self.output, undeclared_pixel=True)
        directory = next(self.output.iterdir())
        self.assertTrue(directory.name.endswith(".failed"))
        self.assertFalse((directory / "metadata.json").exists())
        self.assertEqual(len(list(directory.rglob("*.png"))), 1)

    def test_tampered_leaf_bounds_are_recomputed_from_raw_trees(self):
        directory = dynamic_capture(self.output)
        metadata = json.loads((directory / "metadata.json").read_bytes())
        metadata["stability"]["regions"][0]["bounds"][0] -= 1
        path = directory / "stability.json"
        path.write_text(json.dumps(metadata["stability"]), encoding="utf-8")
        metadata["files"]["stability"] = file_info(path)
        with self.assertRaisesRegex(CaptureError, "original system leaf evidence"):
            verify_capture_evidence(directory, metadata)
