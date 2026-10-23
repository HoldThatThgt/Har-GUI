"""稳定采集合同、原有登记格式与逐运行来源核验测试。"""

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tomllib
import unittest
from contextlib import closing
from pathlib import Path

from har_capture import dataset
from har_capture.dataset_identity import (
    configuration_identity, configuration_sources, manifest_configuration, relocate_evidence, run_identity,
)
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from tests import test_dataset


class DatasetIdentityTests(unittest.TestCase):
    setUp = test_dataset.DatasetTests.setUp
    tearDown = test_dataset.DatasetTests.tearDown
    delivery = test_dataset.DatasetTests.delivery
    publish = test_dataset.DatasetTests.publish
    status = test_dataset.DatasetTests.status
    save_configuration = test_dataset.DatasetTests.save_configuration

    def changed(self, mutate):
        configuration = copy.deepcopy(self.identity["configuration"])
        mutate(configuration)
        configuration["project"]["run"]["max_actions"] += 1
        counter = getattr(self, "project_counter", 0) + 1
        self.project_counter = counter
        configuration["projectPath"] = str(self.directory / f"changed-{counter}.toml")
        test_dataset.write_project(configuration)
        return {**configuration_identity(configuration), "configuration": configuration,
                "project_sources": list(self.identity["project_sources"])}

    def registered_strings(self):
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            return (connection.execute("SELECT digest,value FROM identity").fetchall(),
                    connection.execute("SELECT sample_id,manifest FROM samples ORDER BY sample_id").fetchall())

    def saved_configuration(self, name="run-a"):
        return Path(self.identity["configuration"]["outputPath"]) / name / "configuration.json"

    def test_complete_loaded_configuration_is_mandatory_even_for_an_empty_dataset(self):
        with self.assertRaisesRegex(CaptureError, "configuration"):
            dataset.dataset_status(self.root, run_identity(self.identity))
        self.assertFalse(self.root.exists())

    def test_rpc_hashes_and_quota_must_match_its_snapshot(self):
        for key, value in (("project_sha256", "d" * 64), ("config_sha256", "e" * 64),
                           ("tasks", [{"name": "editor", "target_count": 100}])):
            with self.subTest(field=key):
                changed = {**self.identity, key: value}
                with self.assertRaisesRegex(CaptureError, "does not match its loaded configuration"):
                    dataset.dataset_status(self.root, changed)

    def test_mutable_run_fields_resume_old_rows_and_new_receipt_keeps_its_own_run(self):
        first = self.publish(self.delivery())
        before = self.registered_strings()
        artifacts = {path: (file_info(path), path.stat().st_mtime_ns)
                     for path in Path(first["path"]).iterdir()}

        def mutate(configuration):
            project = configuration["project"]
            project["run"].update(max_actions=90, max_duration_seconds=900)
            task = project["tasks"][0]
            task.update(instruction="Explore another page", acceptance="Confirm its content", target_count=5)
            task["privacy"]["reason"] = "Updated explanation"

        self.identity = self.changed(mutate)
        self.run_name = "run-b"
        self.assertEqual(self.status()["counts"]["editor"], 1)
        self.assertEqual(self.status()["remaining"]["editor"], 4)
        self.assertEqual(before, self.registered_strings())
        self.assertEqual(artifacts, {path: (file_info(path), path.stat().st_mtime_ns) for path in artifacts})
        second = self.publish(self.delivery("frame-b", x=40))
        self.assertNotEqual(first["identity_sha256"], second["identity_sha256"])
        self.assertEqual(self.registered_strings()[0], before[0])
        self.assertEqual(self.registered_strings()[1][0], before[1][0])
        self.assertEqual(self.status()["counts"]["editor"], 2)

    def test_existing_three_key_identity_and_original_manifest_text_are_not_rewritten(self):
        self.publish(self.delivery())
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            value = connection.execute("SELECT value FROM identity").fetchone()[0]
            manifest = connection.execute("SELECT manifest FROM samples").fetchone()[0]
            self.assertEqual(set(json.loads(value)), {"project_sha256", "config_sha256", "tasks"})
            connection.execute("UPDATE identity SET value=?", (json.dumps(json.loads(value), indent=3),))
            connection.execute("UPDATE samples SET manifest=?", (json.dumps(json.loads(manifest), indent=3),))
            connection.commit()
        before = self.registered_strings()
        self.identity = self.changed(lambda cfg: cfg["project"]["tasks"][0].update(target_count=8))
        self.assertEqual(self.status()["remaining"]["editor"], 7)
        self.assertEqual(before, self.registered_strings())

    def test_current_quota_can_be_lower_than_history_without_deleting_samples(self):
        self.publish(self.delivery())
        self.publish(self.delivery("frame-b", x=40))
        self.identity = self.changed(lambda cfg: cfg["project"]["tasks"][0].update(target_count=1))
        self.run_name = "run-b"
        self.assertEqual(self.status()["counts"]["editor"], 2)
        self.assertEqual(self.status()["remaining"]["editor"], 0)
        with self.assertRaisesRegex(CaptureError, "quota is already complete"):
            self.publish(self.delivery("frame-c", x=60))
        self.assertEqual(self.status()["counts"]["editor"], 2)

    def test_every_retained_collection_field_rejects_a_different_contract(self):
        self.publish(self.delivery())
        changes = {
            "task": lambda c: c["project"]["tasks"][0].update(name="wrong-task"),
            "app": lambda c: c["project"]["tasks"][0].update(app="wrong-app"),
            "scene": lambda c: c["project"]["tasks"][0].update(scene="wrong-scene"),
            "bundle": lambda c: c["project"]["tasks"][0].update(bundle_names=["wrong.bundle"]),
            "canvas": lambda c: c["project"]["dataset"]["canvas"].update(width=1920),
            "stability": lambda c: c["project"]["dataset"].update(
                stability_regions=[{"bundle_name": "system.clock", "resource_id": "time"}]),
            "privacy": lambda c: c["project"]["tasks"][0]["privacy"].update(enabled=True),
            "regions": lambda c: c["project"]["tasks"][0]["privacy"].update(
                regions=[{"bundle_name": "app.editor", "resource_id": "body"}]),
            "model": lambda c: c["profile"].update(model="other-model"),
            "model-binding": lambda c: c["digests"].update(config="f" * 64),
            "tools": lambda c: c["project"]["tools"].update(enabled=["different-tool"]),
            "run-name": lambda c: c["project"]["run"].update(name="different-project"),
        }
        for name, mutate in changes.items():
            with self.subTest(field=name):
                with self.assertRaisesRegex(CaptureError, "collection contract"):
                    dataset.dataset_status(self.root, self.changed(mutate))
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_changed_run_requires_its_original_saved_configuration(self):
        self.publish(self.delivery())
        self.saved_configuration().unlink()
        changed = self.changed(lambda cfg: cfg["project"]["tasks"][0].update(target_count=8))
        with self.assertRaisesRegex(CaptureError, "configuration is missing"):
            dataset.dataset_status(self.root, changed)

    def test_unchanged_creation_identity_still_requires_its_original_capture_run_snapshot(self):
        self.publish(self.delivery())
        self.saved_configuration().unlink()
        before = self.registered_strings()
        with self.assertRaisesRegex(CaptureError, "Original capture run configuration is missing"):
            self.status()
        self.assertEqual(before, self.registered_strings())

    def test_matching_repeated_run_snapshots_are_valid_sources(self):
        self.publish(self.delivery())
        duplicate = self.saved_configuration("run-copy")
        duplicate.parent.mkdir()
        configuration = copy.deepcopy(self.identity["configuration"])
        configuration["projectPath"] = str(self.directory / "elsewhere" / "project.toml")
        duplicate.write_text(json.dumps(configuration), encoding="utf-8")
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_conflicting_snapshots_cannot_share_one_original_header(self):
        self.publish(self.delivery())
        original = json.loads(self.saved_configuration().read_bytes())
        for name, mutate in (("model", lambda c: c["profile"].update(model="forged")),
                             ("instruction", lambda c: c["project"]["tasks"][0].update(instruction="forged")),
                             ("privacy-reason", lambda c: c["project"]["tasks"][0]["privacy"].update(reason="forged"))):
            with self.subTest(field=name):
                changed = copy.deepcopy(original)
                mutate(changed)
                self.saved_configuration().write_text(json.dumps(changed), encoding="utf-8")
                expected = "disagree for the same run identity" if name == "model" else "differs from its raw TOML"
                with self.assertRaisesRegex(CaptureError, expected):
                    self.status()
        self.saved_configuration().write_text(json.dumps(original), encoding="utf-8")

    def test_registered_identity_digest_must_match_its_stored_value(self):
        self.publish(self.delivery())
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            value = json.loads(connection.execute("SELECT value FROM identity").fetchone()[0])
            value["project_sha256"] = "d" * 64
            connection.execute("UPDATE identity SET value=?", (json.dumps(value),))
            connection.commit()
        with self.assertRaisesRegex(CaptureError, "digest does not match its original value"):
            self.status()

    def test_capture_run_cannot_be_rebound_to_another_matching_contract(self):
        self.publish(self.delivery())
        backup = self.saved_configuration("original-backup")
        backup.parent.mkdir()
        shutil.copyfile(self.saved_configuration(), backup)
        self.identity = self.changed(lambda cfg: cfg["project"]["tasks"][0].update(target_count=8))
        self.saved_configuration().write_text(json.dumps(self.identity["configuration"]), encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "origin belongs to a different run identity"):
            self.status()

    def test_relocation_uses_one_project_relative_mapping(self):
        original = self.identity["configuration"]
        current = copy.deepcopy(original)
        current["projectPath"] = str(self.directory / "copy" / "project.toml")
        manifest = {"evidence_directory": str(self.directory / "output" / "run-a" / "captures" / "frame-a")}
        expected = self.directory / "copy" / "output" / "run-a" / "captures" / "frame-a"
        self.assertEqual(relocate_evidence(manifest, original, current), expected.resolve())

    def test_unchanged_header_uses_archived_project_path_as_its_relocation_anchor(self):
        sample = self.publish(self.delivery())
        historical = copy.deepcopy(self.identity["configuration"])
        current = copy.deepcopy(historical)
        current["projectPath"] = str(self.directory / "copy" / "project.toml")
        current["outputPath"] = str(self.directory / "copy" / "output")
        current["datasetPath"] = str(self.directory / "copy" / "dataset")
        shutil.copytree(Path(historical["outputPath"]), Path(current["outputPath"]))
        Path(current["projectPath"]).write_bytes(Path(historical["projectPath"]).read_bytes())
        identity = {**configuration_identity(current), "configuration": current, "project_sources": []}
        original = manifest_configuration(sample, identity, configuration_sources(identity))
        self.assertEqual(original["projectPath"], historical["projectPath"])
        expected = self.directory / "copy" / "output" / "run-a" / "captures" / "frame-a"
        self.assertEqual(relocate_evidence(sample, original, current), expected.resolve())

    def test_sample_contract_is_verified_even_when_tampered_file_digests_are_updated(self):
        sample = self.publish(self.delivery())
        path = Path(sample["path"]) / sample["files"]["wms"]["file"]
        original = path.read_bytes()
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            saved = connection.execute("SELECT manifest FROM samples").fetchone()[0]
        changes = {
            "app": lambda wms: wms["sample"].update(app="forged-app"),
            "scene": lambda wms: wms["sample"].update(scene="forged-scene"),
            "privacy": lambda wms: wms["privacy"].update(policy_sha256="0" * 64),
            "stability": lambda wms: wms["stability"].update(
                rules=[{"bundle_name": "system.clock", "resource_id": "clock"}]),
        }
        for name, mutate in changes.items():
            with self.subTest(field=name):
                document = json.loads(original)
                mutate(document)
                path.write_text(json.dumps(document), encoding="utf-8")
                manifest = json.loads(saved)
                manifest["files"]["wms"] = file_info(path)
                with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
                    connection.execute("UPDATE samples SET manifest=?", (json.dumps(manifest),))
                    connection.commit()
                with self.assertRaisesRegex(CaptureError, "differs from|rules differ"):
                    self.status()
        path.write_bytes(original)
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            connection.execute("UPDATE samples SET manifest=?", (saved,))
            connection.commit()
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def cross_run_delivery(self):
        source, acceptance = self.delivery()
        evidence = source.parent
        candidate = {"capture_id": "frame-a", "path": str(evidence), "task_name": "editor",
                     "target_bundle_name": "app.editor", "page_name": "page", "window_changes": []}
        (evidence / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
        def mutable(configuration):
            configuration["project"]["run"].update(max_actions=80, max_duration_seconds=500)
            configuration["project"]["tasks"][0].update(
                target_count=8, instruction="Explore more pages", acceptance="Check the final page")
        self.identity = self.changed(mutable)
        self.run_name = "run-b"
        self.save_configuration("run-b")
        snapshot = self.saved_configuration("run-b")
        sample = {**candidate, "finalization_directory": str(snapshot.parent),
                  "accepted_screenshot_sha256": acceptance["accepted_screenshot_sha256"], "model_usage": {}}
        (evidence / "sample.json").write_text(json.dumps(sample), encoding="utf-8")
        return source, acceptance

    def test_capture_and_finalization_can_use_different_explicit_matching_contract_runs(self):
        delivery = self.cross_run_delivery()
        candidate_path = delivery[0].parent / "candidate.json"
        snapshot = self.saved_configuration("run-a")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (candidate_path, snapshot)}
        sample = self.publish(delivery)
        self.assertEqual(sample["identity_sha256"], dataset.digest(run_identity(self.identity)))
        self.assertEqual(self.status()["counts"]["editor"], 1)
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before})

    def test_explicit_finalization_requires_its_original_absolute_run_path(self):
        delivery = self.cross_run_delivery()
        self.publish(delivery)
        path = delivery[0].parent / "sample.json"
        sample = json.loads(path.read_bytes())
        sample["finalization_directory"] = str(self.directory / "forged" / "run-b")
        path.write_text(json.dumps(sample), encoding="utf-8")
        before = self.registered_strings()
        with self.assertRaisesRegex(CaptureError, "finalization directory differs"):
            self.status()
        self.assertEqual(before, self.registered_strings())

    def test_cross_run_capture_source_must_retain_its_independent_collection_contract(self):
        self.publish(self.cross_run_delivery())
        path = self.saved_configuration("run-a")
        configuration = json.loads(path.read_bytes())
        configuration["project"]["tasks"][0]["app"] = "different-app"
        path.write_text(json.dumps(configuration), encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "Saved project snapshot differs from its raw TOML"):
            self.status()

    def test_cross_run_finalization_cannot_change_or_remove_its_original_candidate(self):
        delivery = self.cross_run_delivery()
        self.publish(delivery)
        path = delivery[0].parent / "candidate.json"
        candidate = json.loads(path.read_bytes())
        candidate["window_changes"] = [{"kind": "forged"}]
        path.write_text(json.dumps(candidate), encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "differs from its original candidate identity"):
            self.status()
        path.unlink()
        with self.assertRaisesRegex(CaptureError, "no proven original candidate"):
            self.status()

    def test_current_project_raw_digest_is_verified_before_empty_dataset_status(self):
        path = Path(self.identity["configuration"]["projectPath"])
        path.write_bytes(path.read_bytes() + b"\n# changed exact bytes\n")
        with self.assertRaisesRegex(CaptureError, "Current project digest differs from its actual source"):
            self.status()
        self.assertFalse(self.root.exists())

    def test_current_snapshot_content_must_match_its_unchanged_raw_digest(self):
        self.identity["configuration"]["project"]["tools"]["enabled"] = ["forged-tool"]
        with self.assertRaisesRegex(CaptureError, "Saved project snapshot differs from its raw TOML source"):
            self.status()
        self.assertFalse(self.root.exists())

    def test_unique_historical_snapshot_cannot_change_tools_while_retaining_old_sha(self):
        self.publish(self.delivery())
        self.identity = self.changed(lambda cfg: cfg["project"]["tasks"][0].update(target_count=8))
        path = self.saved_configuration("run-a")
        historical = json.loads(path.read_bytes())
        self.assertNotEqual(historical["digests"]["project"], self.identity["project_sha256"])
        historical["project"]["tools"]["enabled"] = ["forged-tool"]
        path.write_text(json.dumps(historical), encoding="utf-8")
        before = self.registered_strings()
        with self.assertRaisesRegex(CaptureError, "Saved project snapshot differs from its raw TOML source"):
            self.status()
        self.assertEqual(before, self.registered_strings())

    def test_unreferenced_failed_run_needs_no_historical_raw_proof(self):
        configuration = copy.deepcopy(self.identity["configuration"])
        configuration["project"]["tasks"][0]["instruction"] = "Unused failed run"
        configuration["projectPath"] = str(self.directory / "unreferenced.toml")
        test_dataset.write_project(configuration)
        path = self.saved_configuration("unused-failed-run")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(configuration), encoding="utf-8")
        Path(configuration["projectPath"]).unlink()
        self.publish(self.delivery())
        self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_historical_raw_source_must_be_explicit_when_not_archived_in_its_run(self):
        self.publish(self.delivery())
        archive = self.saved_configuration().with_name("project.toml")
        raw = archive.read_bytes()
        archive.unlink()
        self.identity = self.changed(lambda cfg: cfg["project"]["tasks"][0].update(target_count=8))
        with self.assertRaisesRegex(CaptureError, "Raw project source is missing for the saved project digest"):
            self.status()
        declared = self.directory / "declared-projects" / "nested" / "saved.toml"
        declared.parent.mkdir(parents=True)
        declared.write_bytes(raw)
        for source in (declared, declared.parent.parent):
            with self.subTest(source=source.name):
                self.identity["project_sources"] = [str(source)]
                self.assertEqual(self.status()["counts"]["editor"], 1)

    def test_raw_source_comparison_normalizes_recursive_crlf_strings_without_changing_hash(self):
        configuration = self.identity["configuration"]
        path = Path(configuration["projectPath"])
        text = path.read_bytes().decode("utf-8")
        text, replacements = re.subn(r'^instruction = .*$', 'instruction = """\nFirst\nSecond"""',
                                     text, flags=re.MULTILINE)
        self.assertEqual(replacements, 2)
        raw = text.replace("\n", "\r\n").encode("utf-8")
        script = ("import { parse } from 'smol-toml'; let value = ''; "
                  "for await (const chunk of process.stdin) value += chunk; "
                  "process.stdout.write(JSON.stringify(parse(value)));")
        result = subprocess.run(["node", "--input-type=module", "-e", script], input=raw,
                                capture_output=True, check=True, cwd=Path(__file__).resolve().parent.parent)
        configuration["project"] = json.loads(result.stdout)
        configuration["digests"]["project"] = hashlib.sha256(raw).hexdigest()
        path.write_bytes(raw)
        self.identity.update(configuration_identity(configuration))
        self.assertIn("\r\n", configuration["project"]["tasks"][0]["instruction"])
        self.assertNotIn("\r\n", tomllib.loads(raw.decode())["tasks"][0]["instruction"])
        self.assertEqual(self.status()["counts"]["editor"], 0)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.identity["project_sha256"])

    def test_raw_project_omitted_stability_regions_uses_current_loader_default(self):
        configuration = self.identity["configuration"]
        path = Path(configuration["projectPath"])
        raw, count = re.subn(rb'^stability_regions = \[\]\r?\n', b'', path.read_bytes(), flags=re.MULTILINE)
        self.assertEqual(count, 1)
        path.write_bytes(raw)
        configuration["digests"]["project"] = hashlib.sha256(raw).hexdigest()
        self.identity.update(configuration_identity(configuration))
        self.assertEqual(self.status()["counts"]["editor"], 0)

    def test_project_sources_are_mandatory_and_must_be_existing_absolute_local_paths(self):
        missing = {key: value for key, value in self.identity.items() if key != "project_sources"}
        with self.assertRaisesRegex(CaptureError, "project_sources"):
            dataset.dataset_status(self.root, missing)
        for value in (None, "source.toml", ["relative.toml"], [str(self.directory / "missing")]):
            with self.subTest(value=value):
                with self.assertRaises(CaptureError):
                    dataset.dataset_status(self.root, {**self.identity, "project_sources": value})

    def test_cross_run_missing_capture_snapshot_cannot_skip_forged_finalization_proof(self):
        delivery = self.cross_run_delivery()
        self.publish(delivery)
        self.saved_configuration("run-a").unlink()
        path = delivery[0].parent / "sample.json"
        sample = json.loads(path.read_bytes())
        sample["finalization_directory"] = str(self.directory / "forged" / "run-b")
        path.write_text(json.dumps(sample), encoding="utf-8")
        before = self.registered_strings()
        with self.assertRaisesRegex(CaptureError, "Original capture run configuration is missing"):
            self.status()
        self.assertEqual(before, self.registered_strings())


if __name__ == "__main__":
    unittest.main()
