"""五件套发布、跨运行去重及中断恢复测试。"""

import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from har_capture import dataset
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from tests.test_signatures import fixture


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "dataset"
        self.identity = {"project_sha256": "a" * 64, "config_sha256": "b" * 64,
                         "tasks": [{"name": "editor", "target_count": 3}, {"name": "other", "target_count": 2}]}

    def tearDown(self):
        self.temporary.cleanup()

    def delivery(self, capture_id="frame-a", x=20, task="editor"):
        source = self.directory / capture_id
        source.mkdir()
        wms, accessibility, arkui = fixture()
        wms["capture_id"] = capture_id
        wms["app_windows"][0]["bounding_box"]["x"] = x
        checks = {key: "pass" for key in dataset.CHECKS}
        wms["checks"] = checks
        wms["sample"] = {"app": "editor", "page_id": "page", "scene": "scene", "state_id": "state"}
        for index, window in enumerate(wms["app_windows"]):
            window["trees"] = {kind: {"file": f"editor_page_scene_state_{key}.json",
                                      "json_pointer": f"/windows/{index}/tree"}
                               for kind, key in (("accessibility", "windows_tree"), ("arkui", "windows_akui"))}
        for document in (accessibility, arkui):
            document.update(capture_id=capture_id, canvas=wms["canvas"])
        documents = {"wms": wms, "tree": accessibility, "windows_tree": accessibility, "windows_akui": arkui}
        files = {}
        for kind, suffix in dataset.SUFFIXES.items():
            destination = source / ("editor_page_scene_state" + suffix)
            if kind == "screenshot":
                Image.new("RGB", (3120, 2080), "white").save(destination)
            else:
                destination.write_text(json.dumps(documents[kind]), encoding="utf-8")
            files[kind] = file_info(destination)
        acceptance = {"pass": True, "task_name": task, "target_bundle_name": "app.editor",
                      "accepted_screenshot_sha256": files["screenshot"]["sha256"], "model_usage": {},
                      "export": {"status": "complete", "path": str(source), "files": files, "checks": checks}}
        return source, acceptance

    def publish(self, item):
        return dataset.publish_sample(self.root, self.identity, *item)

    def status(self):
        return dataset.dataset_status(self.root, self.identity)

    def rows(self):
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            return connection.execute("SELECT sample_id,status,error FROM samples ORDER BY sample_id").fetchall()

    def test_initial_status_has_empty_quotas(self):
        self.assertEqual(self.status()["counts"], {"editor": 0, "other": 0})
        self.assertEqual(self.status()["remaining"], {"editor": 3, "other": 2})
        self.assertFalse(self.root.exists())
        changed = {**self.identity, "config_sha256": "c" * 64}
        self.assertEqual(dataset.dataset_status(self.root, changed)["counts"]["editor"], 0)

    def test_publish_moves_exactly_five_files_and_resume_is_idempotent(self):
        source, acceptance = self.delivery()
        published = self.publish((source, acceptance))
        self.assertFalse(source.exists())
        self.assertEqual(len(list(Path(published["path"]).iterdir())), 5)
        self.assertEqual(published["publication_version"], 1)
        self.assertEqual(published["signature_version"], 1)
        self.assertEqual(self.status(), self.status())
        self.assertEqual(self.status()["counts"]["editor"], 1)
        self.assertEqual(self.rows(), [("frame-a", "committed", None)])

    def test_same_state_cannot_be_counted_twice_across_runs(self):
        self.publish(self.delivery())
        with self.assertRaisesRegex(CaptureError, "Duplicate state"):
            self.publish(self.delivery("frame-b"))
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_same_frame_state_cannot_be_assigned_to_another_task(self):
        self.publish(self.delivery())
        with self.assertRaisesRegex(CaptureError, "already belongs to task editor"):
            self.publish(self.delivery("frame-b", task="other"))
        self.assertEqual(self.status()["counts"], {"editor": 1, "other": 0})

    def test_same_page_different_geometry_is_a_distinct_valid_state(self):
        first = self.publish(self.delivery())
        second = self.publish(self.delivery("frame-b", x=40))
        self.assertEqual(first["page_hash"], second["page_hash"])
        self.assertNotEqual(first["state_hash"], second["state_hash"])
        self.assertEqual(first["files"]["screenshot"]["sha256"], second["files"]["screenshot"]["sha256"])
        self.assertEqual(self.status()["counts"]["editor"], 2)
        self.assertEqual(first["page_id"], first["page_hash"][:16])
        self.assertEqual(first["page_id"], second["page_id"])
        self.assertEqual(first["state_id"], "000001")
        self.assertEqual(second["state_id"], "000002")
        for sample in (first, second):
            expected = f"editor_{sample['page_id']}_scene_{sample['state_id']}"
            self.assertEqual({info["file"] for info in sample["files"].values()},
                             {expected + suffix for suffix in dataset.SUFFIXES.values()})
            wms = json.loads((Path(sample["path"]) / sample["files"]["wms"]["file"]).read_bytes())
            self.assertEqual(wms["sample"]["page_id"], sample["page_id"])
            self.assertEqual(wms["sample"]["state_id"], sample["state_id"])

    def test_failed_visual_acceptance_does_not_prepare_or_count(self):
        source, acceptance = self.delivery()
        acceptance["pass"] = False
        with self.assertRaisesRegex(CaptureError, "passing saved-image"):
            self.publish((source, acceptance))
        self.assertEqual(self.status()["counts"]["editor"], 0)
        self.assertEqual(self.rows(), [])

    def test_each_required_export_gate_blocks_publication(self):
        for key in dataset.CHECKS:
            with self.subTest(check=key):
                source, acceptance = self.delivery(key)
                acceptance["export"]["checks"][key] = "fail"
                with self.assertRaisesRegex(CaptureError, "all required checks"):
                    self.publish((source, acceptance))
        self.assertEqual(self.status()["counts"]["editor"], 0)

    def test_wms_gate_must_also_pass(self):
        source, acceptance = self.delivery()
        info = acceptance["export"]["files"]["wms"]
        path = source / info["file"]
        document = json.loads(path.read_bytes())
        document["checks"]["coordinates"] = "fail"
        path.write_text(json.dumps(document))
        acceptance["export"]["files"]["wms"] = file_info(path)
        with self.assertRaisesRegex(CaptureError, "must all pass"):
            self.publish((source, acceptance))

    def test_extra_file_or_second_screenshot_is_rejected(self):
        for name in ("other.png", "metadata.json"):
            with self.subTest(file=name):
                source, acceptance = self.delivery(name.replace(".", "-"))
                (source / name).write_bytes(b"extra")
                with self.assertRaisesRegex(CaptureError, "exactly one matching"):
                    self.publish((source, acceptance))

    def test_tampered_published_file_prevents_resume_without_resetting_counts(self):
        sample = self.publish(self.delivery())
        path = Path(sample["path"]) / sample["files"]["tree"]["file"]
        path.write_text("{}")
        with self.assertRaisesRegex(CaptureError, "digest mismatch"):
            self.status()
        self.assertEqual(self.rows(), [("frame-a", "committed", None)])

    def test_changed_configuration_is_not_silently_reset(self):
        self.publish(self.delivery())
        changed = {**self.identity, "config_sha256": "c" * 64}
        with self.assertRaisesRegex(CaptureError, "configuration identity differs"):
            dataset.dataset_status(self.root, changed)
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_interruption_after_prepare_recovers_source_once(self):
        source, acceptance = self.delivery()
        original = dataset.recover_dataset
        calls = 0

        def stop_after_prepare(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("process stopped after prepare")
            return original(*args)

        with patch.object(dataset, "recover_dataset", stop_after_prepare):
            with self.assertRaises(KeyboardInterrupt):
                self.publish((source, acceptance))
        self.assertEqual(self.rows(), [("frame-a", "prepared", None)])
        self.assertTrue(source.exists())
        self.assertEqual(self.status()["counts"]["editor"], 1)
        self.assertFalse(source.exists())
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_interruption_after_rename_recovers_destination_once(self):
        source, acceptance = self.delivery()
        original = Path.rename

        def rename_then_stop(path, target):
            original(path, target)
            if path == source:
                raise KeyboardInterrupt("process stopped after rename")

        with patch.object(Path, "rename", rename_then_stop):
            with self.assertRaises(KeyboardInterrupt):
                self.publish((source, acceptance))
        self.assertFalse(source.exists())
        self.assertEqual(self.rows(), [("frame-a", "prepared", None)])
        self.assertEqual(self.status()["counts"]["editor"], 1)
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_damaged_prepared_evidence_is_recorded_failed_and_not_counted(self):
        source, acceptance = self.delivery()
        original = dataset.recover_dataset
        with patch.object(dataset, "recover_dataset", side_effect=[None, KeyboardInterrupt()]):
            with self.assertRaises(KeyboardInterrupt):
                self.publish((source, acceptance))
        (source / acceptance["export"]["files"]["tree"]["file"]).unlink()
        with self.assertRaises(CaptureError):
            self.status()
        self.assertEqual(self.rows()[0][1], "failed")
        self.assertEqual(self.status()["counts"]["editor"], 0)
        self.assertTrue(self.status()["failures"][0]["error"])

    def test_unregistered_orphan_directory_is_not_silently_imported(self):
        self.status()
        (self.root / "samples" / "orphan").mkdir(parents=True)
        with self.assertRaisesRegex(CaptureError, "no registration database"):
            self.status()

    def test_recovery_rechecks_semantic_signatures(self):
        self.publish(self.delivery())
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            manifest = json.loads(connection.execute("SELECT manifest FROM samples").fetchone()[0])
            manifest["state_hash"] = "0" * 64
            connection.execute("UPDATE samples SET manifest=?", (json.dumps(manifest),))
            connection.commit()
        with self.assertRaisesRegex(CaptureError, "signature no longer matches"):
            self.status()

    def test_quota_is_enforced_before_publication(self):
        self.identity["tasks"][0]["target_count"] = 1
        self.publish(self.delivery())
        source, acceptance = self.delivery("frame-b", x=40)
        with self.assertRaisesRegex(CaptureError, "quota is already complete"):
            self.publish((source, acceptance))
        self.assertTrue(source.exists())
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_two_publishers_cannot_count_same_state_twice(self):
        self.status()
        items = [self.delivery("frame-a"), self.delivery("frame-b")]

        def attempt(item):
            try:
                return self.publish(item)
            except CaptureError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, items))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_rule_version_change_requires_explicit_new_dataset(self):
        self.publish(self.delivery())
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            manifest = json.loads(connection.execute("SELECT manifest FROM samples").fetchone()[0])
            manifest["signature_version"] = 999
            connection.execute("UPDATE samples SET manifest=?", (json.dumps(manifest),))
            connection.commit()
        with self.assertRaisesRegex(CaptureError, "rule version differs"):
            self.status()

    def test_interruption_during_each_artifact_rename_resumes_durable_naming_plan(self):
        self.identity["tasks"][0]["target_count"] = 10
        for index, kind in enumerate(dataset.SUFFIXES):
            with self.subTest(kind=kind):
                source, acceptance = self.delivery(f"rename-{kind}", x=30 + index)
                original = Path.rename
                def stop_after_named_file(path, target):
                    original(path, target)
                    if path.name == acceptance["export"]["files"][kind]["file"]:
                        raise KeyboardInterrupt("process stopped during artifact naming")
                with patch.object(Path, "rename", stop_after_named_file):
                    with self.assertRaises(KeyboardInterrupt):
                        self.publish((source, acceptance))
                snapshot = self.status()
                self.assertEqual(snapshot["counts"]["editor"], index + 1)
                sample = next(item for item in snapshot["samples"] if item["sample_id"] == f"rename-{kind}")
                self.assertEqual(sample["state_id"], f"{index + 1:06d}")
                self.assertEqual(len(list(Path(sample["path"]).iterdir())), 5)


if __name__ == "__main__":
    unittest.main()
