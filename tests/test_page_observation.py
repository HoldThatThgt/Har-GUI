"""通过离线设备替身验证目标观测及模型语义收据的完整绑定。"""

import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from har_capture.collector import capture
from har_capture.delivery_operations import prepare_delivery
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from har_capture.page_observation import crop_bounds, verify_page_observation, verify_page_stability
from har_capture.privacy import atomic_json, redact_capture
from tests.support import (FIXTURES, fake_hdc, layout_data, raw_accessibility, save_page_stability,
                           synthetic_closing_geometry, synthetic_screen_geometry)

TARGET = {"window_id": 92, "bundle_name": "com.example.settings"}


class PageObservationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name)
        for name, effect in (("attach_captured_screen_geometry", synthetic_screen_geometry),
                             ("read_window_geometry", synthetic_closing_geometry)):
            mock = patch("har_capture.window_geometry." + name, side_effect=effect)
            mock.start()
            self.addCleanup(mock.stop)
        self.result = capture(fake_hdc(), self.output, target=TARGET)
        self.directory = self.result["path"]
        self.metadata = json.loads((self.directory / "metadata.json").read_bytes())
        self.receipt = save_page_stability(self.directory, self.metadata)

    def rewrite_observation(self, mutate):
        path = self.directory / "page-observation.json"
        observation = json.loads(path.read_bytes())
        mutate(observation)
        atomic_json(path, observation)
        self.metadata["files"]["page_observation"] = file_info(path)
        self.receipt["observation_sha256"] = self.metadata["files"]["page_observation"]["sha256"]
        atomic_json(self.directory / "page-stability.json", self.receipt)

    def test_capture_has_one_persistent_image_and_transient_pair_bound_to_exact_sources(self):
        transient = self.result["page_observation"]
        observation = verify_page_observation(self.directory, self.metadata)
        self.assertEqual(observation, transient["receipt"])
        self.assertEqual(observation["target"], {**TARGET, "process_id": 202, "display_id": 0})
        self.assertEqual(observation["before"]["screenshot_sha256"],
                         hashlib.sha256((self.directory / "screenshot.png").read_bytes()).hexdigest())
        for name in ("before", "after"):
            raw = base64.b64decode(transient[name + "_image"].split(",", 1)[1], validate=True)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), observation[name]["crop_sha256"])
            with Image.open(io.BytesIO(raw)) as crop:
                bounds = observation[name]["crop_bounds"]
                self.assertEqual(crop.size, (bounds["width"], bounds["height"]))
        self.assertEqual(len(list(self.directory.rglob("*.png"))), 1)
        for path in self.directory.rglob("*.json"):
            self.assertNotIn(b"data:image/png;base64,", path.read_bytes())
            self.assertNotIn(b"before_image", path.read_bytes())

    def test_semantic_receipt_is_postcapture_and_does_not_expand_the_raw_file_index(self):
        self.assertNotIn("page_stability", self.metadata["files"])
        original = json.loads((self.directory / "capture-evidence.json").read_bytes())
        self.assertNotIn("page_stability", original)
        self.assertEqual(original["files"], self.metadata["files"])
        self.assertEqual(verify_page_stability(self.directory, self.metadata), self.receipt)

    def test_same_page_and_unobservable_are_honest_distinct_accepted_decisions(self):
        for decision in ("same_page", "unobservable"):
            receipt = save_page_stability(self.directory, self.metadata, decision,
                                          "Occluded body cannot be established" if decision == "unobservable"
                                          else "Same target page with dynamic text")
            self.assertEqual(verify_page_stability(self.directory, self.metadata), receipt)

    def test_page_navigation_and_blank_body_are_rejected_even_with_identical_raw_pixels(self):
        self.assertFalse(self.metadata["consistency"]["pixels"]["changed"])
        for decision in ("page_changed", "body_blank"):
            save_page_stability(self.directory, self.metadata, decision)
            with self.subTest(decision=decision), self.assertRaisesRegex(CaptureError, decision):
                verify_page_stability(self.directory, self.metadata)

    def test_missing_semantic_receipt_cannot_prepare_or_recompute_from_the_single_saved_image(self):
        (self.directory / "page-stability.json").unlink()
        request = {"directory": str(self.directory), "privacy": {"enabled": False, "reason": "Synthetic"},
                   "options": {"app": "test", "page_id": "page", "scene": "scene", "state_id": "state",
                               "canvas": {"width": 3120, "height": 2080}, "stability_regions": []}}
        with self.assertRaisesRegex(CaptureError, "page-stability.json"):
            prepare_delivery(request)
        self.assertFalse((self.directory / "delivery").exists())
        self.assertEqual(len(list(self.directory.rglob("*.png"))), 1)

    def test_raw_capture_without_target_is_complete_but_not_semantically_publishable(self):
        result = capture(fake_hdc(), self.output)
        metadata = json.loads((result["path"] / "metadata.json").read_bytes())
        self.assertEqual(metadata["status"], "complete")
        self.assertIsNone(result["page_observation"])
        with self.assertRaisesRegex(CaptureError, "indexed target page observation"):
            verify_page_stability(result["path"], metadata)

    def test_explicit_wrong_target_never_selects_a_focused_or_other_visible_window(self):
        for target in ({"window_id": 92, "bundle_name": "com.example.reader"},
                       {"window_id": 999, "bundle_name": "com.example.settings"}):
            with self.subTest(target=target), self.assertRaisesRegex(CaptureError, "unique captured"):
                capture(fake_hdc(), self.output, target=target)

    def test_target_process_replacement_is_an_identity_error_independent_of_page_semantics(self):
        windows = (FIXTURES / "windows.txt").read_text(encoding="utf-8")
        changed = windows.replace(" 202 ", " 909 ")
        self.assertNotEqual(windows, changed)
        with self.assertRaisesRegex(CaptureError, "target window identity changed"):
            capture(fake_hdc(command_outputs={"closing_windows": changed}), self.output, target=TARGET)

    def test_target_geometry_change_is_an_independent_coordinate_error(self):
        def moved(_hdc, windows):
            result = copy.deepcopy(windows)
            for window in result:
                if window["window_id"] == TARGET["window_id"]:
                    window["screen_bounds"]["x"] += 0.25
            return result
        with patch("har_capture.window_geometry.read_window_geometry", side_effect=moved), \
                self.assertRaisesRegex(CaptureError, "target geometry changed"):
            capture(fake_hdc(), self.output, target=TARGET)

    def test_missing_target_identity_is_rejected_before_any_device_call(self):
        for target in ({"window_id": 92}, {"window_id": True, "bundle_name": "app"},
                       {"window_id": 0, "bundle_name": "app"}):
            hdc = fake_hdc()
            with self.subTest(target=target), self.assertRaisesRegex(CaptureError, "explicit target"):
                capture(hdc, self.output, target=target)
            hdc.run.assert_not_called()

    def test_semantic_target_digest_capture_id_decision_and_evidence_are_all_bound(self):
        mutations = ({"capture_id": "another"}, {"target": {**self.receipt["target"], "window_id": 80}},
                     {"target": {**self.receipt["target"], "display_id": False}},
                     {"observation_sha256": "0" * 64}, {"decision": "maybe"}, {"decision": []},
                     {"evidence": " "}, {"rule_version": True}, {"extra": "field"})
        for mutation in mutations:
            atomic_json(self.directory / "page-stability.json", {**self.receipt, **mutation})
            with self.subTest(mutation=mutation), self.assertRaises(CaptureError):
                verify_page_stability(self.directory, self.metadata)

    def test_observation_tampering_is_rejected_without_rewriting_the_capture_index(self):
        path = self.directory / "page-observation.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(CaptureError, "captured file index"):
            verify_page_stability(self.directory, self.metadata)

    def test_wrong_raw_source_digest_and_original_screenshot_are_rejected(self):
        for field in ("windows_sha256", "layout_sha256", "screenshot_sha256"):
            original = self.result["page_observation"]["receipt"]
            atomic_json(self.directory / "page-observation.json", original)
            self.rewrite_observation(lambda observation: observation["before"].update({field: "0" * 64}))
            with self.subTest(field=field), self.assertRaises(CaptureError):
                verify_page_stability(self.directory, self.metadata)

    def test_forged_target_and_crop_geometry_are_rejected(self):
        mutations = [lambda observation: observation["target"].update(process_id=999),
                     lambda observation: observation["before"]["screen_bounds"].update(x=0),
                     lambda observation: observation["after"]["crop_bounds"].update(width=True)]
        for mutation in mutations:
            atomic_json(self.directory / "page-observation.json", self.result["page_observation"]["receipt"])
            self.rewrite_observation(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(CaptureError):
                verify_page_stability(self.directory, self.metadata)

    def test_fractional_envelope_preserves_float_bounds_and_clips_only_the_crop(self):
        bounds = {"x": -0.25, "y": 1.75, "width": 10.5, "height": 4.5}
        original = dict(bounds)
        self.assertEqual(crop_bounds(bounds, {"width": 8, "height": 6}),
                         {"x": 0, "y": 1, "width": 8, "height": 5})
        self.assertEqual(bounds, original)

    def test_nonfinite_or_empty_geometry_never_becomes_a_valid_crop(self):
        for bounds in ({"x": float("nan"), "y": 0, "width": 5, "height": 5},
                       {"x": 0, "y": 0, "width": -1, "height": 5},
                       {"x": 10, "y": 0, "width": 5, "height": 5}):
            with self.subTest(bounds=bounds), self.assertRaises(CaptureError):
                crop_bounds(bounds, {"width": 8, "height": 6})

    def test_enabled_and_disabled_privacy_use_the_matching_actual_original_receipt(self):
        for enabled in (False, True):
            roots = layout_data()["children"]
            roots[1]["attributes"]["id"] = "private-body"
            result = capture(fake_hdc({"accessibility": raw_accessibility(roots)}), self.output, target=TARGET)
            directory = result["path"]
            original = (directory / "screenshot.png").read_bytes()
            metadata = json.loads((directory / "metadata.json").read_bytes())
            save_page_stability(directory, metadata)
            policy = {"enabled": enabled, "reason": "Synthetic private content", "regions": [{
                "bundle_name": TARGET["bundle_name"], "resource_id": "private-body" if enabled else "absent"}]}
            metadata = redact_capture(directory, policy)
            self.assertEqual(verify_page_stability(directory, metadata)["decision"], "same_page")
            self.assertEqual(metadata["privacy"]["before_sha256"], hashlib.sha256(original).hexdigest())
            self.assertEqual((directory / "screenshot.png").read_bytes() == original, not enabled)
            self.assertEqual(len(list(directory.rglob("*.png"))), 1)
            if not enabled:
                self.assertEqual(metadata["privacy"]["regions"], [])

    def test_metadata_cannot_forge_original_image_identity_without_matching_privacy_json(self):
        metadata = redact_capture(self.directory, {"enabled": False, "reason": "Synthetic"})
        metadata["privacy"]["before_sha256"] = "0" * 64
        with self.assertRaisesRegex(CaptureError, "saved privacy receipt"):
            verify_page_stability(self.directory, metadata)
        metadata = json.loads((self.directory / "metadata.json").read_bytes())
        (self.directory / "privacy.json").unlink()
        with self.assertRaisesRegex(CaptureError, "privacy.json"):
            verify_page_stability(self.directory, metadata)
