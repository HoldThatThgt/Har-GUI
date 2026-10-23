"""项目动态声明只记录来源，全部变化留在原始观测中。"""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from har_capture.collector import capture
from har_capture.delivery_operations import prepare_delivery, verify_capture_evidence
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json
from har_capture.stability import observe_stability, validate_rules, verify_project_rules
from tests.support import (fake_hdc, layout_data, png_bytes, raw_layout, save_page_stability,
                           synthetic_closing_geometry, synthetic_screen_geometry)

RULES = [{"bundle_name": "system.test", "resource_id": "clock"}]
TARGET = {"window_id": 92, "bundle_name": "com.example.settings"}


def pixel_changes(points, size=(8, 6)):
    image = Image.new("RGB", size, "white")
    for point in points:
        image.putpixel(point, (0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class StabilityTests(unittest.TestCase):
    def test_declarations_are_copied_without_matching_nodes_or_generating_masks(self):
        declared = copy.deepcopy(RULES)
        receipt = observe_stability(declared)
        declared[0]["resource_id"] = "modified"
        self.assertEqual(receipt["rules"], RULES)
        self.assertEqual(receipt["method"], "observation_integrity")
        self.assertNotIn("regions", receipt)
        self.assertNotIn("allowed_text_fields", receipt)
        self.assertIn("no page stability claim", receipt["scope"])

    def test_rules_remain_source_bound_and_reject_invalid_configuration(self):
        for invalid in ({}, [{**RULES[0], "bounds": [0, 0, 8, 6]}], RULES * 2,
                        [{**RULES[0], "resource_id": " "}]):
            with self.subTest(invalid=invalid), self.assertRaises(CaptureError):
                validate_rules(invalid)
        verify_project_rules({"stability": observe_stability(RULES)}, {"stability_regions": RULES})
        with self.assertRaisesRegex(CaptureError, "current project rules"):
            verify_project_rules({"stability": observe_stability(RULES)}, {})


class StabilityCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        for name, effect in (("attach_captured_screen_geometry", synthetic_screen_geometry),
                             ("read_window_geometry", synthetic_closing_geometry)):
            geometry = patch("har_capture.window_geometry." + name, side_effect=effect)
            geometry.start()
            self.addCleanup(geometry.stop)

    def dynamic_capture(self):
        layout = layout_data()
        layout["children"][0]["attributes"]["text"] = "Background changed"
        layout["children"][0]["children"] = [{"attributes": {"text": "new background node"}, "children": []}]
        hdc = fake_hdc({"closing_layout": raw_layout(layout),
                        "closing_screen": pixel_changes([(1100, 200), (2998, 2014)], (3120, 2080))})
        return capture(hdc, self.output, 0, RULES, TARGET)

    def test_missing_declared_resources_and_changed_background_do_not_reject_capture(self):
        result = self.dynamic_capture()
        directory = result["path"]
        metadata = json.loads((directory / "metadata.json").read_bytes())
        self.assertEqual(metadata["stability"], observe_stability(RULES))
        self.assertTrue(metadata["consistency"]["checks"]["layout"]["changed"])
        self.assertEqual(metadata["consistency"]["pixels"]["changed_pixel_count"], 2)
        self.assertEqual((directory / "screenshot.png").read_bytes(), png_bytes(size=(3120, 2080)))
        self.assertEqual(len(list(directory.rglob("*.png"))), 1)
        verify_capture_evidence(directory, metadata)

    def test_nonzero_raw_changes_can_prepare_with_bound_target_semantic_receipt(self):
        directory = self.dynamic_capture()["path"]
        metadata = json.loads((directory / "metadata.json").read_bytes())
        save_page_stability(directory, metadata)
        request = {"directory": str(directory), "privacy": {"enabled": False, "reason": "Synthetic image"},
                   "options": {"app": "test", "page_id": "page", "scene": "scene", "state_id": "state",
                               "canvas": {"width": 3120, "height": 2080}, "stability_regions": RULES}}
        def normalized(evidence, window, canvas, scenes):
            return {"window_id": window["window_id"], "bundle_name": window["bundle_name"], "tree": {}}
        with patch("har_capture.export.load_scene_evidence", return_value={}), \
                patch("har_capture.export.normalized_arkui", side_effect=normalized):
            prepared = prepare_delivery(request)
        wms = json.loads((Path(prepared["manifest"]["path"]) / prepared["manifest"]["files"]["wms"]["file"]).read_bytes())
        self.assertEqual(wms["page_stability"]["decision"], "same_page")
        self.assertEqual(wms["consistency"]["pixels"]["changed_pixel_count"], 2)
        self.assertEqual(len(list(directory.rglob("*.png"))), 1)
        self.assertEqual(prepare_delivery(request), prepared)

    def test_diagnostic_receipt_tampering_is_still_rejected(self):
        directory = self.dynamic_capture()["path"]
        metadata = json.loads((directory / "metadata.json").read_bytes())
        changed = {**metadata["stability"], "rules": []}
        atomic_json(directory / "stability.json", changed)
        metadata["stability"] = changed
        metadata["files"]["stability"] = file_info(directory / "stability.json")
        with self.assertRaisesRegex(CaptureError, "consistency receipt"):
            metadata["consistency"]["checks"]["layout"]["changed"] = False
            verify_capture_evidence(directory, metadata)
