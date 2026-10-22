"""后处理重试与正式登记后的证据指针恢复测试。"""

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from har_capture import delivery_operations as operations
from har_capture.collector import capture
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json
from tests import test_dataset
from tests.support import fake_hdc


class DeliveryOperationTests(unittest.TestCase):
    def setUp(self):
        self.helper = test_dataset.DatasetTests()
        self.helper.setUp()
        evidence = self.helper.directory / "evidence"
        capture(fake_hdc(), self.helper.directory / "captures").rename(evidence)
        self.evidence = evidence
        self.policy = {"enabled": False, "reason": "Synthetic fixture"}
        self.options = {"app": "editor", "page_id": "page", "scene": "scene", "state_id": "state",
                        "canvas": {"width": 3120, "height": 2080}}
        self.metadata = json.loads((evidence / "metadata.json").read_bytes())
        self.metadata["capture_id"] = "frame-a"
        atomic_json(evidence / "metadata.json", self.metadata)
        self.prepare_request = {"directory": str(evidence), "privacy": self.policy, "options": self.options}
        with patch("har_capture.export.normalized_arkui", side_effect=self.normalized):
            prepared = operations.prepare_delivery(self.prepare_request)
        self.metadata, manifest = prepared["metadata"], prepared["manifest"]
        self.receipt = json.loads((evidence / "delivery-preparation.json").read_bytes())
        acceptance = {"pass": True, "task_name": "editor", "target_bundle_name": "com.example.settings",
                      "accepted_screenshot_sha256": manifest["files"]["screenshot"]["sha256"],
                      "model_usage": {}, "export": manifest}
        self.publish_request = {"root": str(self.helper.root), "identity": self.helper.identity,
                                "directory": str(evidence / "delivery"), "evidence_directory": str(evidence),
                                "acceptance": acceptance}

    def normalized(self, evidence, window, canvas):
        return {"window_id": window["window_id"], "bundle_name": window["bundle_name"],
                "tree": json.loads((evidence / window["trees"]["arkui"]["file"]).read_bytes())}

    def tearDown(self):
        self.helper.tearDown()

    def test_prepared_retry_is_idempotent_and_validates_all_files(self):
        first = operations.prepare_delivery(self.prepare_request)
        self.assertEqual(first, operations.prepare_delivery(self.prepare_request))
        path = self.evidence / "delivery" / first["manifest"]["files"]["tree"]["file"]
        path.write_text("{}")
        with self.assertRaisesRegex(CaptureError, "digest mismatch"):
            operations.prepare_delivery(self.prepare_request)

    def test_prepared_retry_rejects_changed_rules(self):
        for field in ("options", "privacy"):
            request = copy.deepcopy(self.prepare_request)
            request[field]["reason" if field == "privacy" else "scene"] = "changed"
            with self.assertRaisesRegex(CaptureError, "rules or capture identity"):
                operations.prepare_delivery(request)

    def test_prepare_rejects_legacy_privacy_policy_instead_of_reinterpreting_it(self):
        request = {**self.prepare_request, "privacy": {"mode": "none", "reason": "Synthetic fixture"}}
        with self.assertRaisesRegex(CaptureError, "mode is not supported"):
            operations.prepare_delivery(request)

    def test_started_preparation_with_completed_metadata_is_recovered(self):
        receipt = {key: value for key, value in self.receipt.items() if key != "manifest_sha256"}
        receipt["status"] = "started"
        atomic_json(self.evidence / "delivery-preparation.json", receipt)
        operations.prepare_delivery(self.prepare_request)
        saved = json.loads((self.evidence / "delivery-preparation.json").read_bytes())
        self.assertEqual(saved["status"], "complete")
        self.assertEqual(saved["manifest_sha256"], self.receipt["manifest_sha256"])

    def test_interruption_after_database_commit_repairs_all_evidence_pointers(self):
        with patch.object(operations, "synchronize_publication", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                operations.publish_delivery(self.publish_request)
        self.assertEqual(self.helper.rows(), [("frame-a", "committed", None)])
        self.assertFalse((self.evidence / "delivery").exists())
        status = operations.resume_dataset(self.publish_request)
        self.assertEqual(status["counts"]["editor"], 1)
        metadata = json.loads((self.evidence / "metadata.json").read_bytes())
        shot = Path(metadata["files"]["screenshot"]["file"])
        self.assertTrue(shot.is_absolute())
        self.assertTrue(shot.is_file())
        self.assertTrue((self.evidence / "publication.json").is_file())
        self.assertEqual(operations.resume_dataset(self.publish_request)["counts"]["editor"], 1)
        self.assertEqual(operations.publish_delivery(self.publish_request)["sample_id"], "frame-a")
        prepared = operations.prepare_delivery(self.prepare_request)
        self.assertEqual(prepared["manifest"]["files"], status["samples"][0]["files"])

    def test_interruption_during_each_pointer_write_is_recoverable(self):
        for filename in ("metadata.json", "delivery-manifest.json", "publication.json"):
            with self.subTest(file=filename):
                result = operations.publish_delivery(self.publish_request) if filename == "metadata.json" else result
                real = operations.atomic_json
                def interrupt(path, value):
                    if path.name == filename:
                        raise KeyboardInterrupt()
                    return real(path, value)
                with patch.object(operations, "atomic_json", interrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        operations.synchronize_publication(result)
                self.assertEqual(operations.resume_dataset(self.publish_request)["counts"]["editor"], 1)

    def test_uncommitted_export_only_removes_owned_files_and_restores_single_png(self):
        metadata = copy.deepcopy(self.metadata)
        del metadata["delivery"]
        metadata["files"]["screenshot"]["file"] = "screenshot.png"
        operations.restore_uncommitted_delivery(self.evidence, metadata, self.prepare_request)
        self.assertTrue((self.evidence / "screenshot.png").is_file())
        self.assertFalse((self.evidence / "delivery").exists())
        self.assertEqual(len(list(self.evidence.rglob("*.png"))), 1)

    def test_uncommitted_export_with_unknown_files_is_not_removed(self):
        metadata = copy.deepcopy(self.metadata)
        del metadata["delivery"]
        metadata["files"]["screenshot"]["file"] = "screenshot.png"
        unknown = self.evidence / "delivery" / "unrelated.txt"
        unknown.write_text("keep")
        with self.assertRaisesRegex(CaptureError, "unrecognized files"):
            operations.restore_uncommitted_delivery(self.evidence, metadata, self.prepare_request)
        self.assertTrue(unknown.is_file())

    def test_prepare_recovers_moved_image_before_raw_verification(self):
        metadata = copy.deepcopy(self.metadata)
        del metadata["delivery"]
        metadata["files"]["screenshot"]["file"] = "screenshot.png"
        atomic_json(self.evidence / "metadata.json", metadata)
        atomic_json(self.evidence / "delivery-preparation.json", {**self.receipt, "status": "started"})
        self.assertFalse((self.evidence / "screenshot.png").exists())
        with patch("har_capture.export.normalized_arkui", side_effect=self.normalized):
            prepared = operations.prepare_delivery(self.prepare_request)
        self.assertEqual(prepared["manifest"]["status"], "complete")
        self.assertEqual(len(list(self.evidence.rglob("*.png"))), 1)
        operations.verify_capture_evidence(self.evidence, prepared["metadata"])

    def test_prepared_retry_rechecks_raw_evidence_digests(self):
        (self.evidence / "accessibility.json").write_text("[]")
        with self.assertRaisesRegex(CaptureError, "Capture artifact digest mismatch"):
            operations.prepare_delivery(self.prepare_request)

    def test_prepared_retry_rejects_omitted_geometry_file_index(self):
        metadata = copy.deepcopy(self.metadata)
        del metadata["files"]["geometry_92_arkui_geometry"]
        atomic_json(self.evidence / "metadata.json", metadata)
        with self.assertRaisesRegex(CaptureError, "differs from the verified file index"):
            operations.prepare_delivery(self.prepare_request)

    def test_prepared_retry_recomputes_coverage_from_raw_wms_sessions(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["coverage"]["covered_window_ids"] = []
        atomic_json(self.evidence / "metadata.json", metadata)
        with self.assertRaisesRegex(CaptureError, "coverage does not match"):
            operations.prepare_delivery(self.prepare_request)

    def test_prepared_retry_recomputes_consistency_and_rejects_pixel_tolerance(self):
        metadata = copy.deepcopy(self.metadata)
        pixels = json.loads((self.evidence / "pixel-consistency.json").read_bytes())
        pixels["changed_pixel_count"] = 199
        atomic_json(self.evidence / "pixel-consistency.json", pixels)
        metadata["files"]["pixel_consistency"] = file_info(self.evidence / "pixel-consistency.json")
        atomic_json(self.evidence / "metadata.json", metadata)
        with self.assertRaisesRegex(CaptureError, "not passed exact comparison"):
            operations.prepare_delivery(self.prepare_request)

    def test_stable_publication_names_update_every_window_tree_reference(self):
        published = operations.publish_delivery(self.publish_request)
        wms = json.loads((Path(published["path"]) / published["files"]["wms"]["file"]).read_bytes())
        for index, window in enumerate(wms["app_windows"]):
            for kind, key in (("accessibility", "windows_tree"), ("arkui", "windows_akui")):
                reference = window["trees"][kind]
                self.assertEqual(reference["file"], published["files"][key]["file"])
                self.assertEqual(reference["json_pointer"], f"/windows/{index}/tree")
                document = json.loads((Path(published["path"]) / reference["file"]).read_bytes())
                self.assertEqual(document["windows"][index]["window_id"], window["window_id"])
        self.assertEqual(operations.resume_dataset(self.publish_request)["counts"]["editor"], 1)


if __name__ == "__main__":
    unittest.main()
