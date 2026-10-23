"""后处理重试与正式登记后的证据指针恢复测试。"""

import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from har_capture import delivery_operations as operations
from har_capture.collector import capture
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json
from tests import test_dataset
from tests.support import fake_hdc, save_page_stability, synthetic_closing_geometry, synthetic_screen_geometry


class DeliveryOperationTests(unittest.TestCase):
    def setUp(self):
        geometry = patch("har_capture.window_geometry.attach_captured_screen_geometry",
                         side_effect=synthetic_screen_geometry)
        geometry.start()
        self.addCleanup(geometry.stop)
        closing = patch("har_capture.window_geometry.read_window_geometry", side_effect=synthetic_closing_geometry)
        closing.start()
        self.addCleanup(closing.stop)
        self.helper = test_dataset.DatasetTests()
        self.helper.setUp()
        evidence = self.helper.directory / "evidence"
        captured = capture(fake_hdc(), self.helper.directory / "captures",
                           target={"window_id": 92, "bundle_name": "com.example.settings"})
        captured["path"].rename(evidence)
        self.evidence = evidence
        self.policy = {"enabled": False, "reason": "Synthetic fixture"}
        self.helper.identity["configuration"]["project"]["tasks"][0]["privacy"] = self.policy
        self.helper.save_configuration("run-a")
        self.options = {"app": "editor", "page_id": "frame-a", "scene": "scene", "state_id": "frame-a",
                        "canvas": {"width": 3120, "height": 2080}, "stability_regions": []}
        self.metadata = json.loads((evidence / "metadata.json").read_bytes())
        self.metadata["capture_id"] = "frame-a"
        atomic_json(evidence / "metadata.json", self.metadata)
        save_page_stability(evidence, self.metadata)
        atomic_json(evidence / "candidate.json", {"capture_id": "frame-a", "task_name": "editor",
                    "target_bundle_name": "com.example.settings", "page_name": "frame-a", "path": str(evidence),
                    "window_changes": []})
        self.prepare_request = {"directory": str(evidence), "privacy": self.policy, "options": self.options}
        with patch("har_capture.export.load_scene_evidence", return_value={}), \
                patch("har_capture.export.normalized_arkui", side_effect=self.normalized):
            prepared = operations.prepare_delivery(self.prepare_request)
        self.metadata, manifest = prepared["metadata"], prepared["manifest"]
        self.receipt = json.loads((evidence / "delivery-preparation.json").read_bytes())
        acceptance = {"pass": True, "task_name": "editor", "target_bundle_name": "com.example.settings",
                      "accepted_screenshot_sha256": manifest["files"]["screenshot"]["sha256"],
                      "model_usage": {}, "export": manifest}
        self.publish_request = {"root": str(self.helper.root), "identity": self.helper.identity,
                                "directory": str(evidence / "delivery"), "evidence_directory": str(evidence),
                                "acceptance": acceptance}

    def normalized(self, evidence, window, canvas, scenes):
        return {"window_id": window["window_id"], "bundle_name": window["bundle_name"],
                "tree": json.loads((evidence / window["trees"]["arkui"]["file"]).read_bytes())}

    def tearDown(self):
        self.helper.tearDown()

    def snapshot_files(self):
        return {str(path.relative_to(self.helper.directory)):
                (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in self.helper.directory.rglob("*") if path.is_file()}

    def historical_receipts(self):
        # 在发布前构造旧格式合成夹具，所有后续恢复都必须保留这些原始字节。
        metadata = copy.deepcopy(self.metadata)
        metadata.pop("page_stability")
        metadata["files"].pop("page_observation")
        for name in ("page-observation.json", "page-stability.json"):
            (self.evidence / name).unlink()
        metadata["stability"] = {"status": "verified", "rule_version": 1, "rules": [],
                                 "regions": [], "allowed_text_fields": ["text", "originalText"]}
        metadata["consistency"].update(status="verified", stable=True)
        metadata["consistency"]["pixels"]["status"] = "verified"
        observations = {"stability": metadata["stability"], "consistency": metadata["consistency"],
                        "pixel_consistency": metadata["consistency"]["pixels"]}
        for kind, value in observations.items():
            path = self.evidence / metadata["files"][kind]["file"]
            atomic_json(path, value)
            metadata["files"][kind] = file_info(path)
        original_path = self.evidence / "capture-evidence.json"
        original = json.loads(original_path.read_bytes())
        screenshot = original["files"]["screenshot"]
        original.update(stability=metadata["stability"], consistency=metadata["consistency"],
                        files={**metadata["files"], "screenshot": screenshot})
        atomic_json(original_path, original)
        manifest = metadata["delivery"]
        wms_path = Path(manifest["path"]) / manifest["files"]["wms"]["file"]
        wms = json.loads(wms_path.read_bytes())
        wms.pop("page_stability")
        wms.update(stability=metadata["stability"], consistency=metadata["consistency"])
        atomic_json(wms_path, wms)
        manifest["files"]["wms"] = file_info(wms_path)
        self.receipt["manifest_sha256"] = operations.manifest_digest(manifest)
        for name, value in (("metadata.json", metadata), ("delivery-manifest.json", manifest),
                            ("delivery-preparation.json", self.receipt)):
            atomic_json(self.evidence / name, value)
        self.publish_request["acceptance"]["export"] = manifest

    def test_old_published_receipts_restore_without_new_page_checks_or_byte_changes(self):
        self.historical_receipts()
        published = operations.publish_delivery(self.publish_request)
        original = self.snapshot_files()
        hooks = ("verify_page_stability", "verify_project_rules", "verify_capture_evidence")
        with patch.object(operations, hooks[0], side_effect=AssertionError("New semantic check was called")), \
                patch.object(operations, hooks[1], side_effect=AssertionError("New rule check was called")), \
                patch.object(operations, hooks[2], side_effect=AssertionError("New capture check was called")):
            for _ in range(2):
                status = operations.resume_dataset(self.publish_request)
                self.assertEqual(status["counts"]["editor"], 1)
                self.assertEqual(len(status["samples"]), 1)
                self.assertEqual(operations.publish_delivery(self.publish_request), published)
                self.assertEqual(self.snapshot_files(), original)
        self.assertFalse((self.evidence / "page-observation.json").exists())
        self.assertFalse((self.evidence / "page-stability.json").exists())

    def preserve_alternate_json(self, names):
        for name in names:
            path = self.evidence / name
            value = json.loads(path.read_bytes())
            original = json.dumps(value, ensure_ascii=True, sort_keys=True, indent=1).replace("\n", "\r\n")
            path.write_bytes((original + "\r\n").encode("utf-8"))
            os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

    def test_completed_preparation_preserves_original_json_bytes_and_all_file_modification_times(self):
        self.preserve_alternate_json(("metadata.json", "delivery-preparation.json", "delivery-manifest.json"))
        before = self.snapshot_files()
        first = operations.prepare_delivery(self.prepare_request)
        self.assertEqual(first, operations.prepare_delivery(self.prepare_request))
        self.assertEqual(before, self.snapshot_files())

    def test_published_resume_prepare_and_republication_preserve_all_bytes_times_and_quotas(self):
        published = operations.publish_delivery(self.publish_request)
        self.preserve_alternate_json(("metadata.json", "delivery-preparation.json", "delivery-manifest.json",
                                      "publication.json"))
        before = self.snapshot_files()
        for _ in range(2):
            status = operations.resume_dataset(self.publish_request)
            prepared = operations.prepare_delivery(self.prepare_request)
            self.assertEqual(operations.publish_delivery(self.publish_request), published)
            self.assertEqual(status["counts"], {"editor": 1, "other": 0})
            self.assertEqual(status["remaining"], {"editor": 2, "other": 2})
            self.assertEqual(status["failures"], [])
            self.assertEqual(prepared["manifest"]["files"], published["files"])
        self.assertEqual(before, self.snapshot_files())
        self.assertEqual(len(list(Path(published["path"]).iterdir())), 5)
        self.assertEqual(self.helper.rows(), [("frame-a", "committed", None)])

    def test_prepared_retry_is_idempotent_and_validates_all_files(self):
        first = operations.prepare_delivery(self.prepare_request)
        self.assertEqual(first, operations.prepare_delivery(self.prepare_request))
        path = self.evidence / "delivery" / first["manifest"]["files"]["tree"]["file"]
        path.write_text("{}")
        with self.assertRaisesRegex(CaptureError, "digest mismatch"):
            operations.prepare_delivery(self.prepare_request)

    def test_prepared_retry_rejects_changed_rules(self):
        before = self.snapshot_files()
        for field in ("options", "privacy"):
            request = copy.deepcopy(self.prepare_request)
            request[field]["reason" if field == "privacy" else "scene"] = "changed"
            with self.assertRaisesRegex(CaptureError, "rules or capture identity"):
                operations.prepare_delivery(request)
        self.assertEqual(before, self.snapshot_files())

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

    def interrupt_publication(self, filename):
        real = operations.atomic_json
        def interrupt(path, value):
            if path.name == filename:
                raise KeyboardInterrupt()
            return real(path, value)
        with patch.object(operations, "atomic_json", interrupt):
            with self.assertRaises(KeyboardInterrupt):
                operations.publish_delivery(self.publish_request)
        self.assertEqual(self.helper.rows(), [("frame-a", "committed", None)])

    def assert_interrupted_write_recovers(self, filename):
        self.interrupt_publication(filename)
        if filename == "delivery-preparation.json":
            self.assertTrue((self.evidence / "publication.json").is_file())
            receipt = json.loads((self.evidence / "delivery-preparation.json").read_bytes())
            delivery = json.loads((self.evidence / "delivery-manifest.json").read_bytes())
            self.assertNotEqual(receipt["manifest_sha256"], operations.manifest_digest(delivery))
        self.assertEqual(operations.resume_dataset(self.publish_request)["counts"]["editor"], 1)
        receipt = json.loads((self.evidence / "delivery-preparation.json").read_bytes())
        delivery = json.loads((self.evidence / "delivery-manifest.json").read_bytes())
        self.assertEqual(receipt["manifest_sha256"], operations.manifest_digest(delivery))
        before = self.snapshot_files()
        with patch.object(operations, "atomic_json", side_effect=AssertionError("Completed publication was written")):
            self.assertEqual(operations.resume_dataset(self.publish_request)["counts"]["editor"], 1)
        self.assertEqual(before, self.snapshot_files())

    def test_interruption_before_metadata_write_is_recoverable(self):
        self.assert_interrupted_write_recovers("metadata.json")

    def test_interruption_before_delivery_manifest_write_is_recoverable(self):
        self.assert_interrupted_write_recovers("delivery-manifest.json")

    def test_interruption_before_publication_receipt_write_is_recoverable(self):
        self.assert_interrupted_write_recovers("publication.json")

    def test_interruption_before_final_preparation_write_is_recoverable(self):
        self.assert_interrupted_write_recovers("delivery-preparation.json")

    def test_missing_completed_publication_is_not_recreated_as_an_interruption(self):
        operations.publish_delivery(self.publish_request)
        (self.evidence / "publication.json").unlink()
        before = self.snapshot_files()
        with patch.object(operations, "atomic_json", side_effect=AssertionError("Tampered publication was repaired")):
            with self.assertRaisesRegex(CaptureError, "interrupted atomic synchronization"):
                operations.resume_dataset(self.publish_request)
        self.assertEqual(before, self.snapshot_files())

    def test_tampered_publication_documents_are_never_repaired(self):
        operations.publish_delivery(self.publish_request)
        mutations = {
            "metadata.json": lambda value: value["delivery"].update(path="D:/forged/sample"),
            "delivery-manifest.json": lambda value: value.update(capture_id="forged"),
            "publication.json": lambda value: value.update(task_name="forged"),
            "delivery-preparation.json": lambda value: value.update(rules_sha256="0" * 64),
        }
        for filename, mutate in mutations.items():
            with self.subTest(file=filename):
                path = self.evidence / filename
                original, modified = path.read_bytes(), json.loads(path.read_bytes())
                mutate(modified)
                atomic_json(path, modified)
                before = self.snapshot_files()
                with patch.object(operations, "atomic_json", side_effect=AssertionError("Tampering was repaired")):
                    with self.assertRaises(CaptureError):
                        operations.resume_dataset(self.publish_request)
                self.assertEqual(before, self.snapshot_files())
                path.write_bytes(original)

    def test_candidate_or_raw_tampering_blocks_completion_before_any_sync_write(self):
        self.interrupt_publication("publication.json")
        candidate = self.evidence / "candidate.json"
        original = candidate.read_bytes()
        changed = json.loads(original)
        changed["target_bundle_name"] = "forged.bundle"
        atomic_json(candidate, changed)
        before = self.snapshot_files()
        with patch.object(operations, "atomic_json", side_effect=AssertionError("Unverified publication was written")):
            with self.assertRaisesRegex(CaptureError, "candidate differs"):
                operations.resume_dataset(self.publish_request)
        self.assertEqual(before, self.snapshot_files())
        candidate.write_bytes(original)
        (self.evidence / "accessibility.json").write_text("[]", encoding="utf-8")
        before = self.snapshot_files()
        with patch.object(operations, "atomic_json", side_effect=AssertionError("Unverified publication was written")):
            with self.assertRaisesRegex(CaptureError, "Capture artifact digest mismatch"):
                operations.resume_dataset(self.publish_request)
        self.assertEqual(before, self.snapshot_files())

    def direct_sample(self):
        candidate = json.loads((self.evidence / "candidate.json").read_bytes())
        candidate.update(origin="direct_capture", finalization_directory=str(self.helper.directory / "output" / "run-a"))
        atomic_json(self.evidence / "sample.json", candidate)
        (self.evidence / "candidate.json").unlink()

    def test_direct_capture_requires_its_recorded_identity_and_original_finalization_run(self):
        self.direct_sample()
        operations.publish_delivery(self.publish_request)
        path = self.evidence / "sample.json"
        original = path.read_bytes()
        mutations = {"origin": "agent", "capture_id": "wrong-capture", "task_name": "wrong-task",
                     "target_bundle_name": "wrong.bundle", "finalization_directory": "D:/missing-run"}
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = json.loads(original)
                changed[field] = value
                atomic_json(path, changed)
                before = self.snapshot_files()
                with patch.object(operations, "atomic_json", side_effect=AssertionError("Invalid direct sample written")):
                    with self.assertRaises(CaptureError):
                        operations.resume_dataset(self.publish_request)
                self.assertEqual(before, self.snapshot_files())
                path.write_bytes(original)

    def test_missing_agent_candidate_is_not_synthesized(self):
        operations.publish_delivery(self.publish_request)
        (self.evidence / "candidate.json").unlink()
        before = self.snapshot_files()
        with self.assertRaisesRegex(CaptureError, "sample.json"):
            operations.resume_dataset(self.publish_request)
        self.assertEqual(before, self.snapshot_files())

    def test_completed_copy_resumes_without_reading_or_writing_original_paths(self):
        operations.publish_delivery(self.publish_request)
        before_original = self.snapshot_files()
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "project"
            shutil.copytree(self.helper.directory, copied)
            configuration = copy.deepcopy(self.helper.identity["configuration"])
            for key, name in (("projectPath", "project.toml"), ("configPath", "config.toml"),
                              ("datasetPath", "dataset"), ("outputPath", "output")):
                configuration[key] = str(copied / name)
            identity = {**self.helper.identity, "configuration": configuration}
            request = {"root": str(copied / "dataset"), "identity": identity}
            before = {str(p.relative_to(copied)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                      for p in copied.rglob("*") if p.is_file()}
            real = Path.open
            def copy_only(path, *args, **kwargs):
                if path.resolve().is_relative_to(self.helper.directory):
                    raise AssertionError("Original project path was accessed")
                return real(path, *args, **kwargs)
            with patch.object(Path, "open", copy_only), patch.object(
                    operations, "atomic_json", side_effect=AssertionError("Completed copy was written")):
                status = operations.resume_dataset(request)
            self.assertEqual(status["counts"]["editor"], 1)
            after = {str(p.relative_to(copied)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                     for p in copied.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
        self.assertEqual(before_original, self.snapshot_files())

    def test_interrupted_copy_completes_actual_files_and_retains_original_receipt_paths(self):
        self.interrupt_publication("publication.json")
        before_original = self.snapshot_files()
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "project"
            shutil.copytree(self.helper.directory, copied)
            configuration = copy.deepcopy(self.helper.identity["configuration"])
            for key, name in (("projectPath", "project.toml"), ("configPath", "config.toml"),
                              ("datasetPath", "dataset"), ("outputPath", "output")):
                configuration[key] = str(copied / name)
            request = {"root": str(copied / "dataset"),
                       "identity": {**self.helper.identity, "configuration": configuration}}
            real = Path.open
            def copy_only(path, *args, **kwargs):
                if path.resolve().is_relative_to(self.helper.directory):
                    raise AssertionError("Original project path was accessed")
                return real(path, *args, **kwargs)
            with patch.object(Path, "open", copy_only):
                status = operations.resume_dataset(request)
            sample = status["samples"][0]
            receipt = json.loads((copied / "evidence" / "publication.json").read_bytes())
            self.assertEqual(receipt["path"], str(self.helper.root / "samples" / "frame-a"))
            self.assertEqual(receipt["evidence_directory"], str(self.evidence))
            self.assertEqual(sample["path"], str(copied / "dataset" / "samples" / "frame-a"))
            metadata = json.loads((copied / "evidence" / "metadata.json").read_bytes())
            self.assertEqual(metadata["delivery"]["path"], receipt["path"])
            with patch.object(operations, "atomic_json", side_effect=AssertionError("Completed copy was written")):
                operations.resume_dataset(request)
        self.assertEqual(before_original, self.snapshot_files())

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
        with patch("har_capture.export.load_scene_evidence", return_value={}), \
                patch("har_capture.export.normalized_arkui", side_effect=self.normalized):
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

    def test_published_metadata_cannot_remove_a_geometry_file_index(self):
        operations.publish_delivery(self.publish_request)
        path = self.evidence / "metadata.json"
        metadata = json.loads(path.read_bytes())
        del metadata["files"]["geometry_92_arkui_geometry"]
        atomic_json(path, metadata)
        before = self.snapshot_files()
        with patch.object(operations, "atomic_json", side_effect=AssertionError("Tampered metadata was repaired")):
            with self.assertRaisesRegex(CaptureError, "metadata|original capture|capture evidence"):
                operations.resume_dataset(self.publish_request)
        self.assertEqual(before, self.snapshot_files())

    def test_published_metadata_cannot_change_original_window_facts(self):
        operations.publish_delivery(self.publish_request)
        path = self.evidence / "metadata.json"
        metadata = json.loads(path.read_bytes())
        metadata["windows"][0]["bounding_box"]["x"] += 1
        atomic_json(path, metadata)
        before = self.snapshot_files()
        with patch.object(operations, "atomic_json", side_effect=AssertionError("Tampered metadata was repaired")):
            with self.assertRaisesRegex(CaptureError, "metadata|original capture|capture evidence"):
                operations.resume_dataset(self.publish_request)
        self.assertEqual(before, self.snapshot_files())

    def test_prepared_retry_recomputes_coverage_from_raw_wms_sessions(self):
        metadata = copy.deepcopy(self.metadata)
        metadata["coverage"]["covered_window_ids"] = []
        atomic_json(self.evidence / "metadata.json", metadata)
        with self.assertRaisesRegex(CaptureError, "coverage does not match"):
            operations.prepare_delivery(self.prepare_request)

    def test_prepared_retry_recomputes_diagnostics_and_rejects_internally_conflicting_pixel_counts(self):
        metadata = copy.deepcopy(self.metadata)
        pixels = json.loads((self.evidence / "pixel-consistency.json").read_bytes())
        pixels["changed_pixel_count"] = 199
        atomic_json(self.evidence / "pixel-consistency.json", pixels)
        metadata["files"]["pixel_consistency"] = file_info(self.evidence / "pixel-consistency.json")
        atomic_json(self.evidence / "metadata.json", metadata)
        with self.assertRaisesRegex(CaptureError, "conflict with their observation"):
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
