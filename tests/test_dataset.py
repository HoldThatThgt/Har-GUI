"""真实 SQLite 的成功采集登记、去重、配额与只读导出测试。"""

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from har_capture import dataset
from har_capture.hdc import CaptureError
from tests.delivery_fixture import CaptureFixture, delivery, snapshot_files
from tests.support import layout_data, raw_accessibility, raw_layout


def changed_payload(text):
    layout = layout_data()
    layout["children"][0]["attributes"]["text"] = text
    return {"layout": raw_layout(layout), "accessibility": raw_accessibility(layout["children"])}


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = CaptureFixture(Path(self.temporary.name))
        context = self.fixture.patch_geometry()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)

    def status(self):
        return dataset.dataset_status(self.fixture.root, self.fixture.identity)

    def rows(self):
        with closing(sqlite3.connect(self.fixture.root / "dataset.sqlite3")) as connection:
            return connection.execute("SELECT sample_id,status,error FROM samples ORDER BY sample_id").fetchall()

    def accepted(self, capture_id="frame-a", payloads=None):
        evidence = self.fixture.capture(capture_id, payloads=payloads)
        return evidence, self.fixture.accept(evidence)

    def test_empty_status_has_quotas_without_creating_database(self):
        self.assertEqual(self.status()["counts"], {"forms": 0})
        self.assertEqual(self.status()["remaining"], {"forms": 3})
        self.assertFalse((self.fixture.root / "dataset.sqlite3").exists())

    def test_registration_stores_canonical_facts_and_leaves_only_png_in_original_evidence(self):
        evidence, sample = self.accepted()
        self.assertFalse((evidence / "accepted").exists())
        self.assertEqual({path.name for path in Path(sample["path"]).iterdir()}, {"capture.json"})
        self.assertTrue((evidence / "screenshot.png").is_file())
        self.assertEqual(len(list(self.fixture.root.rglob("*.png"))), 1)
        self.assertEqual(json.loads((evidence / "publication.json").read_bytes()), sample)
        self.assertEqual(self.status()["counts"], {"forms": 1})
        self.assertEqual(self.rows(), [("frame-a", "committed", None)])

    def test_same_state_cannot_count_twice_across_internal_runs(self):
        self.accepted()
        self.fixture.run_name = "run-b"
        evidence = self.fixture.capture("frame-b")
        with self.assertRaisesRegex(CaptureError, "Duplicate capture state"):
            self.fixture.accept(evidence)
        self.assertEqual(self.status()["counts"], {"forms": 1})

    def test_distinct_observed_content_is_a_distinct_state_on_the_same_page(self):
        _, first = self.accepted()
        _, second = self.accepted("frame-b", changed_payload("Different observed document"))
        self.assertEqual(first["page_hash"], second["page_hash"])
        self.assertNotEqual(first["state_hash"], second["state_hash"])
        self.assertEqual((first["state_id"], second["state_id"]), ("000001", "000002"))
        self.assertEqual(self.status()["counts"], {"forms": 2})

    def test_quota_is_enforced_before_second_registration(self):
        self.fixture.identity["configuration"]["project"]["tasks"][0]["target_count"] = 1
        self.accepted()
        evidence = self.fixture.capture("frame-b", changed_payload("Different state"))
        with self.assertRaisesRegex(CaptureError, "quota"):
            self.fixture.accept(evidence)
        self.assertEqual(self.rows(), [("frame-a", "committed", None)])

    def test_failed_or_mismatched_image_acceptance_never_counts(self):
        evidence = self.fixture.capture("frame-a")
        prepared = self.fixture.prepare(evidence)
        for change in ({"pass": False}, {"accepted_screenshot_sha256": "0" * 64}):
            acceptance = {**self.fixture.acceptance(prepared), **change}
            with self.subTest(change=change), self.assertRaises(CaptureError):
                dataset.publish_sample(self.fixture.root, self.fixture.identity,
                                       Path(prepared["manifest"]["path"]), acceptance)
        self.assertEqual(self.status()["counts"], {"forms": 0})

    def test_prepared_capture_record_must_equal_reverified_complete_facts(self):
        evidence = self.fixture.capture("frame-a")
        prepared = self.fixture.prepare(evidence)
        path = evidence / "accepted" / "capture.json"
        stored = json.loads(path.read_bytes())
        stored["windows"]["app_windows"][0]["bundle_name"] = "forged.bundle"
        path.write_text(json.dumps(stored))
        from har_capture.metadata import file_info
        prepared["manifest"]["files"]["capture"] = file_info(path)
        with self.assertRaisesRegex(CaptureError, "facts differ"):
            self.fixture.accept(evidence, prepared)
        self.assertEqual(self.status()["counts"], {"forms": 0})

    def test_published_file_tampering_prevents_resume_without_resetting_row(self):
        _, sample = self.accepted()
        (Path(sample["path"]) / "capture.json").write_text("{}")
        with self.assertRaisesRegex(CaptureError, "digest"):
            self.status()
        self.assertEqual(self.rows(), [("frame-a", "committed", None)])

    def test_interrupted_reservation_recovers_prepared_source_once(self):
        evidence = self.fixture.capture("frame-a")
        prepared = self.fixture.prepare(evidence)
        original = dataset.recover_dataset
        calls = 0

        def interrupted(connection, root, identity):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("Interrupted after durable reservation")
            return original(connection, root, identity)

        with patch.object(dataset, "recover_dataset", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.fixture.accept(evidence, prepared)
        self.assertEqual(self.rows(), [("frame-a", "prepared", None)])
        self.assertTrue((evidence / "accepted").exists())
        self.assertEqual(self.status()["counts"], {"forms": 1})
        self.assertEqual(self.status()["counts"], {"forms": 1})

    def test_interrupted_after_atomic_move_recovers_existing_destination(self):
        evidence = self.fixture.capture("frame-a")
        prepared = self.fixture.prepare(evidence)

        def interrupted(connection, root, row, identity):
            Path(row["source"]).rename(root / "samples" / row["sample_id"])
            raise RuntimeError("Interrupted after canonical record move")

        with patch.object(dataset, "complete_prepared", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.fixture.accept(evidence, prepared)
        self.assertFalse((evidence / "accepted").exists())
        self.assertEqual(self.rows(), [("frame-a", "prepared", None)])
        self.assertEqual(self.status()["counts"], {"forms": 1})

    def test_unregistered_sample_directory_is_not_imported(self):
        self.accepted()
        (self.fixture.root / "samples" / "orphan").mkdir()
        with self.assertRaisesRegex(CaptureError, "unregistered"):
            self.status()

    def test_signature_and_final_publication_receipt_are_rechecked(self):
        evidence, sample = self.accepted()
        receipt = evidence / "publication.json"
        forged = json.loads(receipt.read_bytes())
        forged["task_name"] = "another"
        receipt.write_text(json.dumps(forged))
        with self.assertRaisesRegex(CaptureError, "receipt differs"):
            dataset.read_accepted_sample(self.fixture.root, self.fixture.identity, sample["sample_id"])
        self.assertEqual(self.rows(), [("frame-a", "committed", None)])

    def test_concurrent_publishers_cannot_count_one_observed_state_twice(self):
        first = self.fixture.capture("frame-a")
        second = self.fixture.capture("frame-b")
        prepared = [self.fixture.prepare(first), self.fixture.prepare(second)]

        def publish(item):
            evidence, preparation = item
            try:
                return self.fixture.accept(evidence, preparation)
            except CaptureError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(publish, zip((first, second), prepared)))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(self.status()["counts"], {"forms": 1})

    def test_equivalent_integer_json_reencoding_keeps_original_raw_window_signatures(self):
        evidence, sample = self.accepted()
        path = evidence / "metadata.json"
        value = json.loads(path.read_text(), parse_float=lambda text: int(float(text))
                           if float(text).is_integer() else float(text))
        path.write_text(json.dumps(value))
        before = snapshot_files([evidence, self.fixture.root / "dataset.sqlite3", Path(sample["path"])])
        result = self.fixture.export(sample, declaration=delivery(6))
        self.assertEqual(result["sample"]["state_hash"], sample["state_hash"])
        self.assertEqual(before, snapshot_files([evidence, self.fixture.root / "dataset.sqlite3",
                                               Path(sample["path"])]))

    def test_changed_delivery_rules_export_five_then_six_without_writing_old_facts_or_database(self):
        evidence, sample = self.accepted()
        run = evidence.parent.parent
        old = [evidence, self.fixture.root / "dataset.sqlite3", Path(sample["path"]),
               run / "configuration.json", run / "project.toml"]
        before = snapshot_files(old)
        first = self.fixture.export(sample, "001-running", delivery())
        second = self.fixture.export(sample, "002-running", delivery(6, "{app}/{capture_id}"))
        self.assertEqual((len(first["export"]["files"]), len(second["export"]["files"])), (5, 6))
        self.assertEqual((first["new_samples"], second["new_samples"]), (0, 0))
        self.assertEqual(before, snapshot_files(old))
        self.assertEqual(self.status()["counts"], {"forms": 1})

    def test_explicit_historical_five_file_manifest_is_verified_readonly_without_format_guessing(self):
        evidence, sample = self.accepted()
        sample = self.fixture.historical_five_files(sample)
        self.assertEqual(set(sample["files"]), {"tree", "windows_tree", "windows_akui", "screenshot", "wms"})
        run = evidence.parent.parent
        old = [evidence, self.fixture.root / "dataset.sqlite3", Path(sample["path"]),
               run / "configuration.json", run / "project.toml"]
        before = snapshot_files(old)
        result = self.fixture.export(sample, "001-running", delivery(6))
        self.assertEqual(result["new_samples"], 0)
        self.assertEqual(len(result["export"]["files"]), 6)
        self.assertEqual(before, snapshot_files(old))

    def test_manifest_verification_accepts_actual_nested_file_set_and_rejects_extra_or_missing(self):
        root = Path(self.temporary.name) / "manifest-files"
        (root / "nested").mkdir(parents=True)
        (root / "nested" / "actual.bin").write_bytes(b"actual")
        from har_capture.metadata import file_info
        files = {"declared-source": {**file_info(root / "nested" / "actual.bin"), "file": "nested/actual.bin"}}
        dataset.verify_files(root, files)
        (root / "extra.txt").write_text("must not be ignored")
        with self.assertRaisesRegex(CaptureError, "differ"):
            dataset.verify_files(root, files)
        (root / "extra.txt").unlink()
        (root / "nested" / "actual.bin").unlink()
        with self.assertRaises(CaptureError):
            dataset.verify_files(root, files)
