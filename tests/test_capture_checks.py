"""原始差异是诊断证据，不能代替目标页面语义。"""

import copy
import unittest

from har_capture.capture_checks import compare_capture_states, compare_screen_pixels, verify_pixel_receipt
from har_capture.hdc import CaptureError
from tests.support import png_bytes


class CaptureStateTests(unittest.TestCase):
    def state(self):
        return {"windows": {1: {"window_id": 1, "bundle_name": "target", "z_order": 2}},
                "layout": {"attributes": {"blur": "", "text": "page", "accessibilityId": "18"}, "children": []}}

    def test_equal_raw_observations_do_not_claim_page_semantics_or_atomicity(self):
        before = self.state()
        result = compare_capture_states(before, copy.deepcopy(before))
        self.assertEqual(result["status"], "observed")
        self.assertFalse(result["atomic"])
        self.assertNotIn("stable", result)
        self.assertNotIn("canonicalization", result)
        self.assertFalse(any(check["changed"] for check in result["checks"].values()))

    def test_text_blur_identity_layout_and_order_changes_remain_unmodified_diagnostics(self):
        for field, value in (("blur", "0.00px"), ("text", "another page"),
                             ("accessibilityId", str((307 << 40) | 18)), ("bounds", "[1,2][30,40]")):
            with self.subTest(field=field):
                before, after = self.state(), self.state()
                after["layout"]["attributes"][field] = value
                original = copy.deepcopy((before, after))
                check = compare_capture_states(before, after)["checks"]["layout"]
                self.assertTrue(check["changed"])
                self.assertNotEqual(check["before_sha256"], check["after_sha256"])
                self.assertEqual((before, after), original)

    def test_background_window_and_other_display_changes_are_recorded_without_rejecting(self):
        before, after = self.state(), self.state()
        after["windows"][8] = {"window_id": 8, "bundle_name": "background", "display_id": 2, "z_order": 9}
        result = compare_capture_states(before, after)
        self.assertTrue(result["checks"]["windows"]["changed"])
        self.assertFalse(result["checks"]["layout"]["changed"])

    def test_body_blank_is_recorded_and_requires_a_separate_semantic_verdict(self):
        before, after = self.state(), self.state()
        after["layout"] = {"attributes": {}, "children": []}
        result = compare_capture_states(before, after)
        self.assertEqual(result["status"], "observed")
        self.assertTrue(result["checks"]["layout"]["changed"])

    def test_pixels_preserve_every_change_and_allow_nonzero_diagnostics(self):
        pixels = compare_screen_pixels(png_bytes(), png_bytes("black"))
        self.assertEqual(pixels["status"], "observed")
        self.assertTrue(pixels["changed"])
        self.assertEqual(pixels["changed_pixel_count"], 48)
        self.assertEqual(pixels["changed_bounds"], [0, 0, 8, 6])
        self.assertNotIn("declared_regions", pixels)
        verify_pixel_receipt(pixels, {"width": 8, "height": 6})

    def test_unchanged_pixel_receipt_is_observed_without_same_page_claim(self):
        pixels = compare_screen_pixels(png_bytes(), png_bytes())
        self.assertFalse(pixels["changed"])
        self.assertEqual(pixels["changed_pixel_count"], 0)
        self.assertIsNone(pixels["changed_bounds"])
        verify_pixel_receipt(pixels, {"width": 8, "height": 6})

    def test_pixel_dimension_mismatch_remains_a_capture_coordinate_error(self):
        with self.assertRaisesRegex(CaptureError, "dimensions changed"):
            compare_screen_pixels(png_bytes(), png_bytes(size=(9, 6)))

    def test_corrupt_pixel_observations_are_rejected_independently(self):
        pixels = compare_screen_pixels(png_bytes(), png_bytes("black"))
        mutations = ({"changed": False}, {"changed_pixel_count": -1}, {"changed_bounds": [0, 0, 9, 6]},
                     {"before_sha256": "bad"}, {"width": 9}, {"status": "verified"})
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(CaptureError):
                verify_pixel_receipt({**pixels, **mutation}, {"width": 8, "height": 6})
