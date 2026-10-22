"""验证原生零值表示与实际屏幕变化的区别。"""

import copy
import unittest

from har_capture.capture_checks import canonical_state, compare_capture_states
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

    def web_state(self, tree_id, element_id=18, component="Web"):
        identity = str((tree_id << 40) | element_id)
        child = {"attributes": {"type": "paragraph", "hostWindowId": "98", "accessibilityId": identity,
                                "hashcode": "98:" + identity, "text": "same body", "bounds": "[1,2][30,40]"},
                 "children": []}
        return {"windows": {}, "layout": {"attributes": {"type": component, "hostWindowId": "98",
                "accessibilityId": "23", "hashcode": "98:23"}, "children": [child]}}

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

    def test_xcomponent_provider_registration_changes_preserve_original_evidence(self):
        before, after = self.web_state(280, 0, "XComponent"), self.web_state(307, 0, "XComponent")
        originals = copy.deepcopy((before, after))
        result = compare_capture_states(before, after)
        self.assertEqual(result["canonicalization"]["rule_version"], "uitest-semantic-state-v2")
        self.assertEqual((before, after), originals)
        self.assertEqual(canonical_state(before), canonical_state(after))

    def test_host_identity_and_each_non_runtime_field_remain_strict(self):
        changes = {"accessibilityId": "24", "hashcode": "98:24", "hostWindowId": "99",
                   "type": "UIExtensionComponent", "text": "changed title", "bounds": "[1,2][30,41]"}
        for component in ("Web", "XComponent"):
            for field, value in changes.items():
                with self.subTest(component=component, field=field):
                    before, after = self.web_state(280, 18, component), self.web_state(307, 18, component)
                    after["layout"]["attributes"][field] = value
                    with self.assertRaises(CaptureError):
                        compare_capture_states(before, after)

    def test_missing_host_identity_cannot_authorize_provider_normalization(self):
        for key in ("accessibilityId", "hashcode", "hostWindowId"):
            before, after = self.web_state(280), self.web_state(307)
            for state in (before, after):
                del state["layout"]["attributes"][key]
            with self.assertRaises(CaptureError):
                compare_capture_states(before, after)

    def test_provider_namespace_partitions_cannot_split_or_merge(self):
        for source_ids, target_ids in (((280, 280), (307, 308)), ((280, 281), (307, 307))):
            before, after = self.web_state(source_ids[0]), self.web_state(target_ids[0])
            before["layout"]["children"].append(self.web_state(source_ids[1], 19)["layout"]["children"][0])
            after["layout"]["children"].append(self.web_state(target_ids[1], 19)["layout"]["children"][0])
            with self.assertRaises(CaptureError):
                compare_capture_states(before, after)

    def test_distinct_provider_namespaces_may_be_renamed_without_merging(self):
        before, after = self.web_state(280), self.web_state(307)
        before["layout"]["children"].append(self.web_state(281, 18)["layout"]["children"][0])
        after["layout"]["children"].append(self.web_state(308, 18)["layout"]["children"][0])
        self.assertEqual(compare_capture_states(before, after)["status"], "verified")

    def test_other_hosts_invalid_hashes_and_unregistered_ids_remain_strict(self):
        for component in ("Web", "XComponent"):
            for first_id, second_id in ((0, 307), (8192, 8193), (-280, -307)):
                before = self.web_state(first_id, 18, component)
                after = self.web_state(second_id, 18, component)
                with self.assertRaises(CaptureError):
                    compare_capture_states(before, after)
            for field, value in (("hostWindowId", "99"), ("hashcode", "unknown")):
                before, after = self.web_state(280, 18, component), self.web_state(307, 18, component)
                for state in (before, after):
                    state["layout"]["children"][0]["attributes"][field] = value
                with self.assertRaises(CaptureError):
                    compare_capture_states(before, after)

    def test_nested_component_host_identity_is_not_renamed(self):
        before, after = self.web_state(280), self.web_state(307)
        for state in (before, after):
            state["layout"]["children"][0]["attributes"]["type"] = "XComponent"
        with self.assertRaises(CaptureError):
            compare_capture_states(before, after)

    def test_memory_usage_text_is_not_discarded_even_on_native_tabs(self):
        before, after = self.web_state(280, 18, "XComponent"), self.web_state(307, 18, "XComponent")
        for state, amount in ((before, "178"), (after, "143")):
            node = state["layout"]["children"][0]
            node["attributes"].update(type="tab", text=f"Document - 内存用量 - {amount} MB",
                                      originalText=f"Document - 内存用量 - {amount} MB")
        with self.assertRaises(CaptureError):
            compare_capture_states(before, after)

    def test_xcomponent_content_geometry_low_identity_structure_and_window_order_remain_strict(self):
        before = self.web_state(280, 18, "XComponent")
        for field, value in (("text", "new body"), ("originalText", "new title"),
                             ("bounds", "[1,2][31,40]"), ("description", "new label")):
            after = self.web_state(307, 18, "XComponent")
            after["layout"]["children"][0]["attributes"][field] = value
            with self.assertRaises(CaptureError):
                compare_capture_states(before, after)
        with self.assertRaises(CaptureError):
            compare_capture_states(before, self.web_state(307, 19, "XComponent"))
        for changed in ("node_added", "node_removed", "z_order"):
            after = self.web_state(307, 18, "XComponent")
            if changed == "node_added":
                after["layout"]["children"].append(copy.deepcopy(after["layout"]["children"][0]))
            elif changed == "node_removed":
                after["layout"]["children"].clear()
            else:
                after["windows"] = {98: {"z_order": 101}}
            with self.assertRaises(CaptureError):
                compare_capture_states(before, after)

    def test_host_tree_identity_does_not_get_reclassified_as_a_child_provider(self):
        for host_tree_id in (1, 280):
            before, after = self.web_state(host_tree_id), self.web_state(307)
            for state in (before, after):
                host_id = str((host_tree_id << 40) | 23)
                state["layout"]["attributes"].update(accessibilityId=host_id, hashcode="98:" + host_id)
            with self.assertRaises(CaptureError):
                compare_capture_states(before, after)

    def test_provider_scope_cannot_cross_another_window(self):
        before, after = self.web_state(280), self.web_state(307)
        for state in (before, after):
            child = state["layout"]["children"][0]
            state["layout"]["children"] = [{"attributes": {"type": "Column", "hostWindowId": "99"},
                                            "children": [child]}]
        with self.assertRaises(CaptureError):
            compare_capture_states(before, after)

    def test_malformed_raw_identity_cannot_collide_with_comparison_representation(self):
        candidates = [
            ("1:18", "98:1:18"),
            ([1, 18], ["98", 1, 18]),
            ({"tree_id": 1, "element_id": 18}, {"window_id": "98", "tree_id": 1, "element_id": 18}),
        ]
        for identity, hashcode in candidates:
            before, after = self.web_state(280), self.web_state(307)
            after["layout"]["children"][0]["attributes"].update(accessibilityId=identity, hashcode=hashcode)
            with self.assertRaises(CaptureError):
                compare_capture_states(before, after)

    def test_registration_namespaces_are_independent_between_hosts(self):
        before, after = {"windows": {}, "layout": []}, {"windows": {}, "layout": []}
        for source_tree, target_tree, host_id in ((280, 307, "23"), (280, 308, "24")):
            for state, tree_id in ((before, source_tree), (after, target_tree)):
                host = self.web_state(tree_id, 18, "XComponent")["layout"]
                host["attributes"].update(accessibilityId=host_id, hashcode="98:" + host_id)
                state["layout"].append(host)
        self.assertEqual(compare_capture_states(before, after)["status"], "verified")

    def test_provider_child_order_remains_strict(self):
        before, after = self.web_state(280), self.web_state(307)
        for state, tree_id in ((before, 280), (after, 307)):
            state["layout"]["children"].append(self.web_state(tree_id, 19)["layout"]["children"][0])
        after["layout"]["children"].reverse()
        with self.assertRaises(CaptureError):
            compare_capture_states(before, after)
