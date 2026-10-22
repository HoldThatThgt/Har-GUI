"""验证独立后处理的实际像素、文件摘要和中断恢复，不使用真实正文。"""

import json
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json, node_inventory, redact_capture, viewer_tree
import redact as cli


class PrivacyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        Image.new("RGB", (8, 6), "white").save(self.directory / "screenshot.png")
        self.tree = {"attributes": {"type": "TextArea", "id": "body", "text": "PRIVATE_BODY_SENTINEL",
                                    "hostWindowId": "42", "bundleName": "test.mail", "bounds": "[1,2][5,5]"},
                     "source_attributes": {"Content": "PRIVATE_BODY_SENTINEL"},
                     "raw": "PRIVATE_BODY_SENTINEL", "children": []}
        (self.directory / "accessibility.json").write_text(json.dumps([self.tree]), encoding="utf-8")
        self.metadata = {"status": "complete", "display": {"width": 8, "height": 6}, "files": {
            "screenshot": file_info(self.directory / "screenshot.png"),
            "accessibility": file_info(self.directory / "accessibility.json")}}
        (self.directory / "metadata.json").write_text(json.dumps(self.metadata), encoding="utf-8")
        self.policy = {"enabled": True, "reason": "Mask the body", "regions": [
            {"bundle_name": "test.mail", "resource_id": "body"}]}

    def test_atomic_json_preserves_equivalent_original_bytes_and_modification_time(self):
        path = self.directory / "receipt.json"
        original = b'{\r\n "text": "\\u4f60\\u597d", "nested": {"enabled": false, "count": 1}\r\n}\r\n'
        path.write_bytes(original)
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        modified = path.stat().st_mtime_ns
        atomic_json(path, {"nested": {"count": 1, "enabled": False}, "text": "你好"})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertFalse(path.with_suffix(".json.pending").exists())

    def test_atomic_json_replaces_changed_values_including_boolean_numeric_difference(self):
        path = self.directory / "receipt.json"
        path.write_bytes(b'{"enabled": false}\r\n')
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        modified = path.stat().st_mtime_ns
        atomic_json(path, {"enabled": 0})
        self.assertIs(type(json.loads(path.read_bytes())["enabled"]), int)
        self.assertNotEqual(path.stat().st_mtime_ns, modified)
        self.assertFalse(path.with_suffix(".json.pending").exists())

    def test_atomic_json_rejects_corrupt_existing_json_without_replacing_evidence(self):
        path = self.directory / "receipt.json"
        original = b'{"corrupt":'
        path.write_bytes(original)
        modified = path.stat().st_mtime_ns
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "Cannot read existing JSON"):
            atomic_json(path, {"corrupt": "new"})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.assertFalse(path.with_suffix(".json.pending").exists())

    def test_repeated_redaction_preserves_all_evidence_bytes_and_modification_times(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                directory = self.directory / str(enabled)
                directory.mkdir()
                for name in ("screenshot.png", "accessibility.json", "metadata.json"):
                    (directory / name).write_bytes((self.directory / name).read_bytes())
                policy = {**self.policy, "enabled": enabled}
                redact_capture(directory, policy)
                snapshot = {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                            for path in directory.iterdir()}
                redact_capture(directory, policy)
                self.assertEqual(snapshot, {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                                            for path in directory.iterdir()})

    def test_masks_exact_pixels_keeps_one_image_and_preserves_tree_bytes(self):
        tree_bytes = (self.directory / "accessibility.json").read_bytes()
        (self.directory / "viewer.html").write_text("contains old screenshot", encoding="utf-8")
        metadata = redact_capture(self.directory, self.policy)
        with Image.open(self.directory / "screenshot.png") as image:
            for y in range(6):
                for x in range(8):
                    expected = (0, 0, 0) if 1 <= x < 5 and 2 <= y < 5 else (255, 255, 255)
                    self.assertEqual(image.getpixel((x, y)), expected)
        self.assertEqual(metadata["files"]["screenshot"], file_info(self.directory / "screenshot.png"))
        self.assertEqual(metadata["privacy"]["status"], "verified")
        self.assertIs(metadata["privacy"]["enabled"], True)
        self.assertNotEqual(metadata["privacy"]["before_sha256"], metadata["privacy"]["after_sha256"])
        self.assertEqual(len(list(self.directory.glob("*.png"))), 1)
        self.assertFalse((self.directory / "viewer.html").exists())
        self.assertEqual((self.directory / "accessibility.json").read_bytes(), tree_bytes)

    def test_recovers_after_png_replace_before_metadata_save(self):
        with patch("har_capture.privacy.finish_redaction", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                redact_capture(self.directory, self.policy)
        recovered = redact_capture(self.directory, self.policy)
        repeated = redact_capture(self.directory, self.policy)
        self.assertEqual(recovered, repeated)
        self.assertEqual(repeated["privacy"]["after_sha256"], file_info(self.directory / "screenshot.png")["sha256"])

    def test_policy_change_and_unknown_selector_never_claim_verified(self):
        missing = {**self.policy, "regions": [{"bundle_name": "test.mail", "resource_id": "absent"}]}
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            redact_capture(self.directory, missing)
        self.assertFalse((self.directory / "privacy.json").exists())
        redact_capture(self.directory, self.policy)
        with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
            redact_capture(self.directory, {**self.policy, "reason": "Changed policy"})

    def test_enabled_without_regions_waits_without_changing_the_screenshot(self):
        before = (self.directory / "screenshot.png").read_bytes()
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "awaits independent redaction"):
            redact_capture(self.directory, {"enabled": True, "reason": "Body selector not yet verified"})
        self.assertEqual(before, (self.directory / "screenshot.png").read_bytes())
        self.assertFalse((self.directory / "privacy.json").exists())

    def test_disabled_keeps_configured_regions_but_never_resolves_or_rewrites_pixels(self):
        screenshot = self.directory / "screenshot.png"
        before, modified = screenshot.read_bytes(), screenshot.stat().st_mtime_ns
        policy = {**self.policy, "enabled": False,
                  "regions": [{"bundle_name": "test.mail", "resource_id": "not-on-this-page"}]}
        with patch("har_capture.privacy.resolve_regions", side_effect=AssertionError("Selectors were resolved")), \
                patch("har_capture.privacy.masked_image", side_effect=AssertionError("Image was rewritten")):
            metadata = redact_capture(self.directory, policy)
            self.assertEqual(redact_capture(self.directory, policy), metadata)
        self.assertEqual(screenshot.read_bytes(), before)
        self.assertEqual(screenshot.stat().st_mtime_ns, modified)
        receipt = json.loads((self.directory / "privacy.json").read_bytes())
        self.assertEqual(receipt, metadata["privacy"])
        self.assertIs(receipt["enabled"], False)
        self.assertEqual(receipt["status"], "disabled")
        self.assertEqual(receipt["regions"], [])
        self.assertEqual(receipt["before_sha256"], receipt["after_sha256"])
        self.assertFalse((self.directory / "screenshot.redacting").exists())

    def test_disabled_without_regions_does_not_read_accessibility_evidence(self):
        (self.directory / "accessibility.json").unlink()
        result = redact_capture(self.directory, {"enabled": False, "reason": "Preserve original screenshot"})
        self.assertEqual(result["privacy"]["status"], "disabled")

    def test_old_modes_missing_switch_and_invalid_regions_are_rejected(self):
        policies = [{"mode": value, "reason": "Old policy"} for value in ("none", "pending", "regions")]
        policies.extend([{**self.policy, "mode": "regions"}, {"reason": "Missing switch"},
                         {**self.policy, "enabled": 0}, {**self.policy, "regions": []},
                         {**self.policy, "enabled": False, "regions": []},
                         {**self.policy, "enabled": False, "regions": [{"resource_id": "body"}]},
                         {**self.policy, "enabled": False, "regions": [{"bundle_name": "", "resource_id": "body"}]}])
        before = (self.directory / "screenshot.png").read_bytes()
        for policy in policies:
            with self.subTest(policy=policy), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                redact_capture(self.directory, policy)
        self.assertEqual((self.directory / "screenshot.png").read_bytes(), before)
        self.assertFalse((self.directory / "privacy.json").exists())

    def test_disabled_receipt_cannot_claim_changed_pixels_or_enabled_processing(self):
        policy = {"enabled": False, "reason": "Preserve image"}
        result = redact_capture(self.directory, policy)
        receipt = result["privacy"]
        for updates in ({"before_sha256": "different"}, {"enabled": True}, {"status": "not_required"}):
            (self.directory / "privacy.json").write_text(json.dumps({**receipt, **updates}), encoding="utf-8")
            with self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                redact_capture(self.directory, policy)

    def test_redact_cli_reads_the_project_enabled_switch(self):
        project = self.directory / "project.toml"
        project.write_text('[[tasks]]\nname="mail"\n[tasks.privacy]\nenabled=false\nreason="Preserve image"\n'
                           'regions=[{bundle_name="test.mail",resource_id="not-on-this-page"}]\n', encoding="utf-8")
        output = io.StringIO()
        screenshot = (self.directory / "screenshot.png").read_bytes()
        with patch("sys.argv", ["redact.py", "apply", str(self.directory), "--project", str(project),
                                "--task", "mail"]), contextlib.redirect_stdout(output):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "disabled")
        self.assertEqual((self.directory / "screenshot.png").read_bytes(), screenshot)

    def test_local_selectors_and_shareable_tree_do_not_leak_body(self):
        inventory = node_inventory(self.directory, self.metadata)
        sanitized = viewer_tree(self.tree)
        self.assertNotIn("PRIVATE_BODY_SENTINEL", json.dumps(inventory) + json.dumps(sanitized))
        self.assertEqual(sanitized["attributes"]["bounds"], "[1,2][5,5]")
        self.assertEqual(inventory[0]["resource_id"], "body")

    def test_changed_selector_tree_is_rejected_before_redacting_any_pixels(self):
        screenshot = (self.directory / "screenshot.png").read_bytes()
        self.tree["attributes"]["bounds"] = "[0,0][8,6]"
        tree_file = self.directory / "accessibility.json"
        tree_file.write_text(json.dumps([self.tree]), encoding="utf-8")
        self.assertEqual(tree_file.stat().st_size, self.metadata["files"]["accessibility"]["bytes"])
        for operation in (lambda: node_inventory(self.directory, self.metadata),
                          lambda: redact_capture(self.directory, self.policy)):
            with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "evidence digest mismatch"):
                operation()
        self.assertEqual(screenshot, (self.directory / "screenshot.png").read_bytes())
        self.assertFalse((self.directory / "privacy.json").exists())

    def test_selector_inventory_checks_recorded_byte_count(self):
        self.metadata["files"]["accessibility"]["bytes"] += 1
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "evidence digest mismatch"):
            node_inventory(self.directory, self.metadata)

    def test_viewer_keeps_native_not_rendered_evidence(self):
        self.tree["geometry"] = {"status": "verified", "rendered": False, "render_state": "not_rendered",
                                 "render_state_source": "RenderService.allNodeNotOnTree",
                                 "clipping_status": "not_applicable", "screen_bounds": None}
        self.assertEqual(viewer_tree(self.tree)["geometry"], self.tree["geometry"])


if __name__ == "__main__":
    unittest.main()
