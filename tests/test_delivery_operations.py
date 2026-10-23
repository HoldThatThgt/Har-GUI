"""完整采集准备、登记和只读重导出的本地操作测试。"""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from har_capture import delivery_operations as operations
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json
from tests.delivery_fixture import CaptureFixture, delivery, snapshot_files
from tests.support import layout_data, raw_accessibility, raw_layout, save_page_stability


class DeliveryOperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = CaptureFixture(Path(self.temporary.name))
        context = self.fixture.patch_geometry()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        self.evidence = self.fixture.capture("frame-a")

    def test_prepare_has_canonical_record_and_real_image_without_delivery_side_effects(self):
        before = (self.evidence / "screenshot.png").read_bytes()
        prepared = self.fixture.prepare(self.evidence)
        manifest = prepared["manifest"]
        self.assertEqual(set(manifest["files"]), {"capture"})
        self.assertEqual(manifest["path"], str(self.evidence / "accepted"))
        self.assertEqual(manifest["screenshot"]["file"], str(self.evidence / "screenshot.png"))
        self.assertNotIn("delivery", prepared["metadata"])
        self.assertFalse((self.evidence / "delivery").exists())
        self.assertEqual((self.evidence / "screenshot.png").read_bytes(), before)
        receipt = json.loads((self.evidence / "capture-preparation.json").read_bytes())
        self.assertEqual(receipt["manifest"], manifest)

    def test_preparation_retry_rechecks_facts_and_preserves_all_existing_bytes_and_times(self):
        first = self.fixture.prepare(self.evidence)
        before = snapshot_files([self.evidence])
        self.assertEqual(self.fixture.prepare(self.evidence), first)
        self.assertEqual(before, snapshot_files([self.evidence]))

    def test_preparation_refuses_changed_privacy_reason_or_stability_declaration(self):
        self.fixture.prepare(self.evidence)
        self.fixture.policy = {**self.fixture.policy, "reason": "A changed preparation reason"}
        with self.assertRaisesRegex(CaptureError, "requirements differ"):
            self.fixture.prepare(self.evidence)

    def test_page_changed_blank_and_missing_receipts_block_preparation(self):
        metadata = json.loads((self.evidence / "metadata.json").read_bytes())
        for decision in ("page_changed", "body_blank"):
            with self.subTest(decision=decision):
                save_page_stability(self.evidence, metadata, decision=decision)
                with self.assertRaisesRegex(CaptureError, "rejected"):
                    self.fixture.prepare(self.evidence)
                self.assertFalse((self.evidence / "accepted").exists())
        (self.evidence / "page-stability.json").unlink()
        with self.assertRaises(CaptureError):
            self.fixture.prepare(self.evidence)

    def test_unobservable_page_receipt_is_honestly_retained(self):
        metadata = json.loads((self.evidence / "metadata.json").read_bytes())
        save_page_stability(self.evidence, metadata, decision="unobservable")
        prepared = self.fixture.prepare(self.evidence)
        self.assertEqual(prepared["metadata"]["page_stability"]["decision"], "unobservable")

    def test_raw_digest_and_removed_geometry_index_are_not_ignored(self):
        metadata = json.loads((self.evidence / "metadata.json").read_bytes())
        name = metadata["windows"][0]["trees"]["arkui"]["coordinate_evidence"]["render_tree"]["file"]
        record_key = next(key for key, value in metadata["files"].items() if value["file"] == name)
        del metadata["files"][record_key]
        atomic_json(self.evidence / "metadata.json", metadata)
        with self.assertRaisesRegex(CaptureError, "file index|verified file index"):
            self.fixture.prepare(self.evidence)

    def test_raw_content_tampering_blocks_preparation(self):
        with (self.evidence / "windows.txt").open("ab") as stream:
            stream.write(b"tampered original")
        with self.assertRaisesRegex(CaptureError, "digest|captured index"):
            self.fixture.prepare(self.evidence)

    def test_original_window_facts_cannot_be_changed_without_raw_evidence(self):
        metadata = json.loads((self.evidence / "metadata.json").read_bytes())
        metadata["windows"][0]["z_order"] += 1
        atomic_json(self.evidence / "metadata.json", metadata)
        with self.assertRaisesRegex(CaptureError, "original WMS"):
            self.fixture.prepare(self.evidence)

    def test_saved_preparation_canonical_record_tampering_is_not_replaced(self):
        self.fixture.prepare(self.evidence)
        record = self.evidence / "accepted" / "capture.json"
        record.write_text("{}")
        with self.assertRaisesRegex(CaptureError, "changed after preparation"):
            self.fixture.prepare(self.evidence)
        self.assertEqual(record.read_text(), "{}")

    def test_enabled_privacy_processes_same_png_and_keeps_receipt_binding(self):
        layout = layout_data()
        layout["children"][1]["attributes"]["id"] = "private-message"
        self.fixture.policy = {"enabled": True, "reason": "Synthetic private region", "regions": [
            {"bundle_name": "com.example.settings", "resource_id": "private-message"}]}
        self.fixture.identity["configuration"]["project"]["tasks"][0]["privacy"] = copy.deepcopy(self.fixture.policy)
        evidence = self.fixture.capture("private-frame", payloads={
            "layout": raw_layout(layout), "accessibility": raw_accessibility(layout["children"])})
        before = file_info(evidence / "screenshot.png")["sha256"]
        prepared = self.fixture.prepare(evidence)
        receipt = prepared["metadata"]["privacy"]
        self.assertTrue(receipt["enabled"])
        self.assertEqual(receipt["before_sha256"], before)
        self.assertEqual(receipt["after_sha256"], prepared["manifest"]["screenshot"]["sha256"])
        self.assertNotEqual(receipt["before_sha256"], receipt["after_sha256"])
        with Image.open(evidence / "screenshot.png") as screenshot:
            self.assertEqual(screenshot.getpixel((1040, 160)), (0, 0, 0))
        self.assertEqual(len(list(evidence.rglob("*.png"))), 1)

    def test_repeated_registration_is_idempotent_and_preserves_source_image(self):
        prepared = self.fixture.prepare(self.evidence)
        sample = self.fixture.accept(self.evidence, prepared)
        before = snapshot_files([self.evidence, Path(sample["path"]), self.fixture.root / "dataset.sqlite3"])
        self.assertEqual(self.fixture.accept(self.evidence, prepared), sample)
        self.assertEqual(before, snapshot_files([self.evidence, Path(sample["path"]),
                                               self.fixture.root / "dataset.sqlite3"]))
        with self.assertRaisesRegex(CaptureError, "already registered"):
            self.fixture.prepare(self.evidence)

    def test_offline_export_uses_readonly_database_and_no_capture_redaction_or_recovery(self):
        sample = self.fixture.accept(self.evidence)
        before = snapshot_files([self.evidence, Path(sample["path"]), self.fixture.root / "dataset.sqlite3"])
        forbidden = ("har_capture.dataset.connect_dataset", "har_capture.dataset.recover_dataset",
                     "har_capture.collector.capture", "har_capture.delivery_operations.redact_capture")
        with patch(forbidden[0], side_effect=AssertionError("Writable dataset connection")), \
                patch(forbidden[1], side_effect=AssertionError("Recovery was called")), \
                patch(forbidden[2], side_effect=AssertionError("Capture was called")), \
                patch(forbidden[3], side_effect=AssertionError("Redaction was called")), \
                patch("har_capture.dataset.sqlite3.connect", wraps=sqlite3.connect) as connections:
            result = self.fixture.export(sample, declaration=delivery(6))
        self.assertEqual(connections.call_count, 1)
        self.assertIn("?mode=ro", connections.call_args.args[0])
        self.assertTrue(connections.call_args.kwargs["uri"])
        self.assertEqual(result["new_samples"], 0)
        self.assertEqual(before, snapshot_files([self.evidence, Path(sample["path"]),
                                               self.fixture.root / "dataset.sqlite3"]))

    def test_export_requires_declared_output_root_and_internal_receipt(self):
        sample = self.fixture.accept(self.evidence)
        identity = self.fixture.identity
        request = {"root": str(self.fixture.root), "identity": identity, "sample_id": sample["sample_id"],
                   "delivery": identity["configuration"]["project"]["delivery"],
                   "destination": str(Path(self.temporary.name) / "wrong-output"),
                   "receipt": str(self.fixture.root / "runs" / "run-a" / "export.json")}
        with self.assertRaisesRegex(CaptureError, "configured output root"):
            operations.export_sample(request)
        request["destination"] = str(Path(identity["configuration"]["outputPath"]) / "001-running")
        request["receipt"] = str(Path(self.temporary.name) / "outside.json")
        with self.assertRaisesRegex(CaptureError, "internal run storage"):
            operations.export_sample(request)

    def test_export_never_creates_a_database_for_an_unregistered_frame(self):
        destination = Path(self.fixture.identity["configuration"]["outputPath"]) / "001-running"
        destination.mkdir(parents=True)
        atomic_json(destination / "manifest.json", {"status": "running", "samples": []})
        with self.assertRaises(sqlite3.OperationalError):
            operations.export_sample({"root": str(self.fixture.root), "identity": self.fixture.identity,
                "sample_id": "missing", "delivery": self.fixture.identity["configuration"]["project"]["delivery"],
                "destination": str(destination), "receipt": str(self.fixture.root / "runs" / "run-a" / "export.json")})
        self.assertFalse((self.fixture.root / "dataset.sqlite3").exists())
        self.assertEqual({path.name for path in destination.iterdir()}, {"manifest.json"})

    def test_offline_export_does_not_complete_an_uncommitted_prepared_capture(self):
        from har_capture import dataset
        prepared = self.fixture.prepare(self.evidence)
        connection = dataset.connect_dataset(self.fixture.root, self.fixture.identity)
        try:
            source = Path(prepared["manifest"]["path"])
            manifest = dataset.prepare_manifest(source, self.fixture.identity, self.fixture.acceptance(prepared))
            dataset.reserve_sample(connection, self.fixture.identity, source, manifest)
        finally:
            connection.close()
        before = snapshot_files([self.evidence, self.fixture.root / "dataset.sqlite3"])
        with self.assertRaisesRegex(CaptureError, "already accepted capture"):
            self.fixture.export({"sample_id": "frame-a"})
        self.assertEqual(before, snapshot_files([self.evidence, self.fixture.root / "dataset.sqlite3"]))
        self.assertTrue((self.evidence / "accepted" / "capture.json").is_file())
        self.assertFalse((self.fixture.root / "samples" / "frame-a").exists())
