"""验证原生零值表示与实际屏幕变化的区别。"""

import copy
import unittest

from har_capture.capture_checks import compare_capture_states
from har_capture.hdc import CaptureError


class CaptureStateTests(unittest.TestCase):
    def state(self, blur):
        return {"windows": {}, "layout": [{"attributes": {"blur": blur, "text": "page"}, "children": []}]}

    def test_native_zero_blur_forms_have_same_meaning_without_modifying_raw_evidence(self):
        before, after = self.state(""), self.state("0.00px")
        original = copy.deepcopy(before)
        result = compare_capture_states(before, after)
        check = result["checks"]["layout"]
        self.assertEqual(check["before_sha256"], check["after_sha256"])
        self.assertNotEqual(check["raw_before_sha256"], check["raw_after_sha256"])
        self.assertEqual(before, original)

    def test_nonzero_blur_and_other_attribute_changes_are_rejected(self):
        with self.assertRaises(CaptureError):
            compare_capture_states(self.state(""), self.state("1.00px"))
        changed = self.state("")
        changed["layout"][0]["attributes"]["text"] = "other page"
        with self.assertRaises(CaptureError):
            compare_capture_states(self.state(""), changed)

    def test_missing_blur_is_not_invented(self):
        changed = self.state("")
        del changed["layout"][0]["attributes"]["blur"]
        with self.assertRaises(CaptureError):
            compare_capture_states(self.state(""), changed)

    def web_state(self, tree_id, element_id=18):
        identity = str((tree_id << 40) | element_id)
        child = {"attributes": {"type": "paragraph", "hostWindowId": "98", "accessibilityId": identity,
                                "hashcode": "98:" + identity, "text": "same body", "bounds": "[1,2][30,40]"},
                 "children": []}
        return {"windows": {}, "layout": {"attributes": {"type": "Web"}, "children": [child]}}

    def test_web_registration_namespace_is_not_page_content(self):
        before, after = self.web_state(153), self.web_state(161)
        result = compare_capture_states(before, after)
        self.assertEqual(result["status"], "verified")
        self.assertNotEqual(result["checks"]["layout"]["raw_before_sha256"],
                            result["checks"]["layout"]["raw_after_sha256"])
        self.assertEqual(before["layout"]["children"][0]["attributes"]["accessibilityId"], str((153 << 40) | 18))

    def test_web_element_identity_and_content_remain_strict(self):
        before = self.web_state(153)
        with self.assertRaises(CaptureError):
            compare_capture_states(before, self.web_state(161, 19))
        for field, value in (("text", "changed body"), ("bounds", "[1,2][30,41]"), ("hostWindowId", "99")):
            after = self.web_state(161)
            after["layout"]["children"][0]["attributes"][field] = value
            with self.assertRaises(CaptureError):
                compare_capture_states(before, after)

    def test_non_web_identity_changes_are_not_normalized(self):
        before, after = self.web_state(153), self.web_state(161)
        for state in (before, after):
            state["layout"]["attributes"]["type"] = "Column"
        with self.assertRaises(CaptureError):
            compare_capture_states(before, after)
