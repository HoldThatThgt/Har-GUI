"""真实项目原文、稳定采集合同与独立运行来源证明测试。"""

import copy
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import tomllib
import unittest
from contextlib import closing
from pathlib import Path

from har_capture import dataset
from har_capture.dataset_identity import (
    configuration_identity, configuration_sources, manifest_configuration, run_identity,
    stable_contract, validate_identity, verify_contract, verify_sample_contract,
)
from har_capture.hdc import CaptureError
from har_capture.signatures import digest


def write_project(configuration):
    """将完整项目写成实际 TOML，并使用原始字节更新 SHA256。"""
    script = ("import { stringify } from 'smol-toml'; let value = ''; "
              "for await (const chunk of process.stdin) value += chunk; "
              "process.stdout.write(stringify(JSON.parse(value)));")
    result = subprocess.run(["node", "--input-type=module", "-e", script],
                            input=json.dumps(configuration["project"]).encode(), capture_output=True,
                            cwd=Path(__file__).resolve().parent.parent, check=True)
    path = Path(configuration["projectPath"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.stdout)
    configuration["digests"]["project"] = hashlib.sha256(result.stdout).hexdigest()
    return result.stdout


class DatasetIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "dataset"
        profile = {"provider": "openai_compatible", "base_url": "https://model.example/v1",
                   "model": "vision", "api_key_env": "MODEL_KEY", "supports_vision": True}
        config = self.directory / "config.toml"
        config.write_text('schema_version = 1\n[profiles.vision]\nmodel = "vision"\n', encoding="utf-8")
        project = {"schema_version": 1,
                   "run": {"name": "collection", "output": "output", "max_actions": 20,
                           "max_duration_seconds": 300},
                   "agent": {"llm": "vision"},
                   "dataset": {"root": "dataset", "canvas": {"width": 3120, "height": 2080},
                               "stability_regions": []},
                   "delivery": {"directory": "{capture_id}",
                                "artifacts": [{"source": "screenshot", "path": "image.png"}]},
                   "tools": {"enabled": ["capture"]},
                   "tasks": [{"name": "editor", "target_count": 3, "app": "editor", "scene": "scene",
                              "bundle_names": ["app.editor"], "instruction": "Find a page",
                              "acceptance": "Page is visible",
                              "privacy": {"enabled": False, "reason": "Synthetic capture"}}]}
        configuration = {"project": project, "profile": profile,
                         "projectPath": str(self.directory / "project.toml"), "configPath": str(config),
                         "outputPath": str(self.directory / "output"), "datasetPath": str(self.root),
                         "digests": {"project": "", "config": hashlib.sha256(config.read_bytes()).hexdigest()}}
        write_project(configuration)
        self.identity = {**configuration_identity(configuration), "configuration": configuration, "project_sources": []}

    def changed(self, mutate):
        configuration = copy.deepcopy(self.identity["configuration"])
        mutate(configuration)
        configuration["project"]["run"]["max_actions"] += 1
        counter = getattr(self, "project_counter", 0) + 1
        self.project_counter = counter
        configuration["projectPath"] = str(self.directory / f"changed-{counter}.toml")
        write_project(configuration)
        return {**configuration_identity(configuration), "configuration": configuration,
                "project_sources": list(self.identity["project_sources"])}

    def saved_configuration(self, name="run-a"):
        return self.root / "runs" / name / "configuration.json"

    def save_configuration(self, name="run-a", identity=None):
        configuration = (identity or self.identity)["configuration"]
        path = self.saved_configuration(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(configuration), encoding="utf-8")
        path.with_name("project.toml").write_bytes(Path(configuration["projectPath"]).read_bytes())
        return path

    def capture_manifest(self):
        self.save_configuration()
        evidence = self.saved_configuration().parent / "captures" / "frame-a"
        evidence.mkdir(parents=True)
        return {"sample_id": "frame-a", "task_name": "editor", "target_bundle_name": "app.editor",
                "identity_sha256": digest(run_identity(self.identity)), "evidence_directory": str(evidence)}

    def resolve_manifest(self, manifest):
        return manifest_configuration(manifest, self.identity, configuration_sources(self.identity))

    def check_contract(self, original):
        return verify_contract(digest(run_identity(original)), self.identity, configuration_sources(self.identity))

    def test_complete_loaded_configuration_is_mandatory_even_for_an_empty_dataset(self):
        with self.assertRaisesRegex(CaptureError, "configuration"):
            dataset.dataset_status(self.root, run_identity(self.identity))
        self.assertFalse(self.root.exists())

    def test_rpc_hashes_and_quota_must_match_its_snapshot(self):
        changes = (("project_sha256", "d" * 64), ("config_sha256", "e" * 64),
                   ("tasks", [{"name": "editor", "target_count": 100}]))
        for key, value in changes:
            with self.subTest(field=key), self.assertRaisesRegex(
                    CaptureError, "does not match its loaded configuration"):
                dataset.dataset_status(self.root, {**self.identity, key: value})
        self.assertFalse(self.root.exists())

    def test_mutable_run_requirements_keep_the_original_collection_contract_and_snapshot(self):
        original = copy.deepcopy(self.identity)
        manifest = self.capture_manifest()
        path = self.saved_configuration()
        before = (path.read_bytes(), path.stat().st_mtime_ns, copy.deepcopy(manifest))

        def mutate(configuration):
            configuration["project"]["run"].update(max_actions=90, max_duration_seconds=900)
            task = configuration["project"]["tasks"][0]
            task.update(instruction="Explore another page", acceptance="Confirm its content", target_count=5)
            task["privacy"]["reason"] = "Updated explanation"

        self.identity = self.changed(mutate)
        self.assertNotEqual(run_identity(original), run_identity(self.identity))
        self.assertEqual(self.check_contract(original), original["configuration"])
        self.assertEqual(self.resolve_manifest(manifest), original["configuration"])
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns, manifest))

    def test_delivery_sources_paths_and_storage_do_not_change_collection_contract(self):
        original = copy.deepcopy(self.identity)
        self.save_configuration()

        def mutate(configuration):
            project = configuration["project"]
            project["delivery"] = {"directory": "{app}/{page_id}/{state_id}", "artifacts": [
                {"source": "application_accessibility", "path": "trees/accessibility.json"},
                {"source": "raw:windows", "path": "raw/windows.txt"}]}
            project["run"]["output"] = "new-output"
            project["dataset"]["root"] = "new-dataset"
            configuration.update(outputPath=str(self.directory / "new-output"),
                                 datasetPath=str(self.directory / "new-dataset"))

        self.identity = self.changed(mutate)
        self.identity["project_sources"] = [str(self.root)]
        self.assertNotEqual(run_identity(original), run_identity(self.identity))
        self.assertEqual(stable_contract(original["configuration"]), stable_contract(self.identity["configuration"]))
        self.assertEqual(self.check_contract(original), original["configuration"])

    def test_every_retained_collection_field_rejects_a_different_contract(self):
        original = copy.deepcopy(self.identity)
        self.save_configuration()
        changes = {
            "task": lambda c: c["project"]["tasks"][0].update(name="wrong-task"),
            "app": lambda c: c["project"]["tasks"][0].update(app="wrong-app"),
            "scene": lambda c: c["project"]["tasks"][0].update(scene="wrong-scene"),
            "bundle": lambda c: c["project"]["tasks"][0].update(bundle_names=["wrong.bundle"]),
            "canvas": lambda c: c["project"]["dataset"]["canvas"].update(width=1920),
            "stability": lambda c: c["project"]["dataset"].update(
                stability_regions=[{"bundle_name": "system.clock", "resource_id": "time"}]),
            "privacy": lambda c: c["project"]["tasks"][0]["privacy"].update(enabled=True),
            "privacy-regions": lambda c: c["project"]["tasks"][0]["privacy"].update(
                regions=[{"bundle_name": "app.editor", "resource_id": "body"}]),
            "model": lambda c: c["profile"].update(model="other-model"),
            "model-binding": lambda c: c["digests"].update(config="f" * 64),
            "tools": lambda c: c["project"]["tools"].update(enabled=["different-tool"]),
            "agent": lambda c: c["project"]["agent"].update(llm="other-profile"),
            "run-name": lambda c: c["project"]["run"].update(name="different-project"),
            "schema": lambda c: c["project"].update(schema_version=2),
        }
        for name, mutate in changes.items():
            with self.subTest(field=name):
                self.identity = original
                self.identity = self.changed(mutate)
                with self.assertRaisesRegex(CaptureError, "collection contract"):
                    self.check_contract(original)
        self.identity = original
        self.assertEqual(self.check_contract(original), original["configuration"])

    def test_three_field_identity_text_is_not_rewritten_when_current_quota_changes(self):
        self.save_configuration()
        dataset.connect_dataset(self.root, self.identity).close()
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            value = connection.execute("SELECT value FROM identity").fetchone()[0]
            self.assertEqual(set(json.loads(value)), {"project_sha256", "config_sha256", "tasks"})
            text = json.dumps(json.loads(value), indent=3)
            connection.execute("UPDATE identity SET value=?", (text,))
            connection.commit()
        self.identity = self.changed(lambda c: c["project"]["tasks"][0].update(target_count=8))
        self.assertEqual(dataset.dataset_status(self.root, self.identity)["remaining"]["editor"], 8)
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            self.assertEqual(connection.execute("SELECT value FROM identity").fetchone()[0], text)

    def test_registered_identity_digest_must_match_its_stored_value(self):
        dataset.connect_dataset(self.root, self.identity).close()
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            value = json.loads(connection.execute("SELECT value FROM identity").fetchone()[0])
            value["project_sha256"] = "d" * 64
            connection.execute("UPDATE identity SET value=?", (json.dumps(value),))
            connection.commit()
        with self.assertRaisesRegex(CaptureError, "digest does not match its original value"):
            dataset.dataset_status(self.root, self.identity)

    def test_changed_run_requires_its_original_saved_configuration(self):
        original = copy.deepcopy(self.identity)
        self.save_configuration().unlink()
        self.identity = self.changed(lambda c: c["project"]["tasks"][0].update(target_count=8))
        with self.assertRaisesRegex(CaptureError, "configuration is missing"):
            self.check_contract(original)

    def test_unchanged_identity_still_requires_its_original_capture_run_snapshot(self):
        manifest = self.capture_manifest()
        self.saved_configuration().unlink()
        with self.assertRaisesRegex(CaptureError, "Original capture run configuration is missing"):
            self.resolve_manifest(manifest)

    def test_matching_repeated_run_snapshots_are_valid_sources(self):
        manifest = self.capture_manifest()
        duplicate = self.save_configuration("run-copy")
        configuration = json.loads(duplicate.read_bytes())
        configuration["projectPath"] = str(self.directory / "elsewhere" / "project.toml")
        duplicate.write_text(json.dumps(configuration), encoding="utf-8")
        self.assertEqual(self.resolve_manifest(manifest), self.identity["configuration"])

    def test_conflicting_snapshots_cannot_share_one_original_header(self):
        manifest = self.capture_manifest()
        original = self.saved_configuration().read_bytes()
        changes = (("model", lambda c: c["profile"].update(model="forged")),
                   ("instruction", lambda c: c["project"]["tasks"][0].update(instruction="forged")),
                   ("privacy-reason", lambda c: c["project"]["tasks"][0]["privacy"].update(reason="forged")))
        for name, mutate in changes:
            with self.subTest(field=name):
                changed = json.loads(original)
                mutate(changed)
                self.saved_configuration().write_text(json.dumps(changed), encoding="utf-8")
                expected = "disagree for the same run identity" if name == "model" else "differs from its raw TOML"
                with self.assertRaisesRegex(CaptureError, expected):
                    self.resolve_manifest(manifest)

    def test_capture_run_cannot_be_rebound_to_another_matching_contract(self):
        manifest = self.capture_manifest()
        self.save_configuration("original-backup")
        self.identity = self.changed(lambda c: c["project"]["tasks"][0].update(target_count=8))
        self.save_configuration()
        with self.assertRaisesRegex(CaptureError, "origin belongs to a different run identity"):
            self.resolve_manifest(manifest)

    def test_unchanged_header_preserves_original_archive_paths_without_rebasing_evidence(self):
        self.save_configuration()
        historical = copy.deepcopy(self.identity["configuration"])
        current = copy.deepcopy(historical)
        current.update(projectPath=str(self.directory / "copy" / "project.toml"),
                       outputPath=str(self.directory / "copy" / "output"),
                       datasetPath=str(self.directory / "copy" / "dataset"))
        shutil.copytree(self.root, Path(current["datasetPath"]))
        Path(current["projectPath"]).write_bytes(Path(historical["projectPath"]).read_bytes())
        identity = {**configuration_identity(current), "configuration": current, "project_sources": []}
        manifest = {"identity_sha256": digest(run_identity(identity)),
                    "evidence_directory": str(self.directory / "raw" / "frame-a")}
        original = manifest_configuration(manifest, identity, configuration_sources(identity))
        self.assertEqual(original["projectPath"], historical["projectPath"])
        self.assertEqual(original["datasetPath"], historical["datasetPath"])
        self.assertEqual(manifest["evidence_directory"], str(self.directory / "raw" / "frame-a"))

    def sample_contract(self):
        configuration = self.identity["configuration"]
        policy = configuration["project"]["tasks"][0]["privacy"]
        sha256 = hashlib.sha256(b"synthetic final image").hexdigest()
        privacy = {"enabled": False, "status": "disabled", "reason": policy["reason"], "regions": [],
                   "before_sha256": sha256, "after_sha256": sha256,
                   "policy_sha256": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()}
        facts = {"screenshot": {"sha256": sha256}, "windows": {
            "canvas": configuration["project"]["dataset"]["canvas"], "app_windows": [{"bundle_name": "app.editor"}],
            "privacy": privacy, "stability": {"status": "verified", "rules": []}}}
        manifest = {"task_name": "editor", "target_bundle_name": "app.editor",
                    "publication_sample": {"app": "editor", "scene": "scene"}}
        return facts, manifest

    def test_sample_contract_uses_complete_facts_independently_of_selected_exports(self):
        facts, manifest = self.sample_contract()
        verify_sample_contract(facts, manifest, self.identity["configuration"])
        changes = {
            "app": lambda f, m: m["publication_sample"].update(app="forged-app"),
            "scene": lambda f, m: m["publication_sample"].update(scene="forged-scene"),
            "bundle": lambda f, m: m.update(target_bundle_name="forged.bundle"),
            "canvas": lambda f, m: f["windows"].update(canvas={"width": 1, "height": 1}),
            "privacy": lambda f, m: f["windows"]["privacy"].update(policy_sha256="0" * 64),
            "stability": lambda f, m: f["windows"]["stability"].update(
                rules=[{"bundle_name": "system.clock", "resource_id": "clock"}]),
        }
        for name, mutate in changes.items():
            with self.subTest(field=name):
                changed_facts, changed_manifest = copy.deepcopy((facts, manifest))
                mutate(changed_facts, changed_manifest)
                with self.assertRaisesRegex(CaptureError, "differs from|rules differ"):
                    verify_sample_contract(changed_facts, changed_manifest, self.identity["configuration"])

    def cross_run_manifest(self):
        manifest = self.capture_manifest()
        evidence = Path(manifest["evidence_directory"])
        candidate = {"capture_id": "frame-a", "path": str(evidence), "task_name": "editor",
                     "target_bundle_name": "app.editor", "page_name": "page", "window_changes": []}
        (evidence / "candidate.json").write_text(json.dumps(candidate), encoding="utf-8")
        self.identity = self.changed(lambda c: c["project"]["tasks"][0].update(
            target_count=8, instruction="Explore more pages", acceptance="Check the final page"))
        finalization = self.save_configuration("run-b").parent
        sample = {**candidate, "finalization_directory": str(finalization), "model_usage": {}}
        (evidence / "sample.json").write_text(json.dumps(sample), encoding="utf-8")
        manifest["identity_sha256"] = digest(run_identity(self.identity))
        return manifest

    def test_capture_and_finalization_can_use_different_explicit_matching_contract_runs(self):
        manifest = self.cross_run_manifest()
        evidence = Path(manifest["evidence_directory"])
        paths = (evidence / "candidate.json", evidence / "sample.json", self.saved_configuration())
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
        self.assertEqual(self.resolve_manifest(manifest), self.identity["configuration"])
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})

    def test_explicit_finalization_requires_its_registered_original_run_path(self):
        manifest = self.cross_run_manifest()
        path = Path(manifest["evidence_directory"]) / "sample.json"
        sample = json.loads(path.read_bytes())
        sample["finalization_directory"] = str(self.directory / "forged" / "run-b")
        path.write_text(json.dumps(sample), encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "finalization run differs from its registered identity"):
            self.resolve_manifest(manifest)

    def test_cross_run_capture_source_must_retain_its_independent_collection_contract(self):
        manifest = self.cross_run_manifest()
        path = self.saved_configuration()
        configuration = json.loads(path.read_bytes())
        configuration["project"]["tasks"][0]["app"] = "different-app"
        path.write_text(json.dumps(configuration), encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "Saved project snapshot differs from its raw TOML"):
            self.resolve_manifest(manifest)

    def test_cross_run_finalization_cannot_change_or_remove_its_original_candidate(self):
        manifest = self.cross_run_manifest()
        path = Path(manifest["evidence_directory"]) / "candidate.json"
        candidate = json.loads(path.read_bytes())
        candidate["window_changes"] = [{"kind": "forged"}]
        path.write_text(json.dumps(candidate), encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "differs from its original candidate identity"):
            self.resolve_manifest(manifest)
        path.unlink()
        with self.assertRaisesRegex(CaptureError, "no proven original candidate"):
            self.resolve_manifest(manifest)

    def test_cross_run_direct_capture_requires_its_explicit_origin_and_unchanged_identity(self):
        manifest = self.cross_run_manifest()
        evidence = Path(manifest["evidence_directory"])
        (evidence / "candidate.json").unlink()
        path = evidence / "sample.json"
        sample = json.loads(path.read_bytes())
        sample.update(origin="direct_capture", page_name="frame-a")
        path.write_text(json.dumps(sample), encoding="utf-8")
        self.assertEqual(self.resolve_manifest(manifest), self.identity["configuration"])
        sample["window_changes"] = [{"kind": "forged"}]
        path.write_text(json.dumps(sample), encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "no proven original candidate"):
            self.resolve_manifest(manifest)

    def test_current_project_raw_digest_is_verified_before_empty_dataset_status(self):
        path = Path(self.identity["configuration"]["projectPath"])
        path.write_bytes(path.read_bytes() + b"\n# changed exact bytes\n")
        with self.assertRaisesRegex(CaptureError, "Current project digest differs from its actual source"):
            dataset.dataset_status(self.root, self.identity)
        self.assertFalse(self.root.exists())

    def test_current_snapshot_content_must_match_its_unchanged_raw_digest(self):
        self.identity["configuration"]["project"]["tools"]["enabled"] = ["forged-tool"]
        with self.assertRaisesRegex(CaptureError, "Saved project snapshot differs from its raw TOML source"):
            dataset.dataset_status(self.root, self.identity)
        self.assertFalse(self.root.exists())

    def test_unique_historical_snapshot_cannot_change_tools_while_retaining_old_sha(self):
        original = copy.deepcopy(self.identity)
        path = self.save_configuration()
        self.identity = self.changed(lambda c: c["project"]["tasks"][0].update(target_count=8))
        historical = json.loads(path.read_bytes())
        historical["project"]["tools"]["enabled"] = ["forged-tool"]
        path.write_text(json.dumps(historical), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaisesRegex(CaptureError, "Saved project snapshot differs from its raw TOML source"):
            self.check_contract(original)
        self.assertEqual(path.read_bytes(), before)

    def test_unreferenced_failed_run_needs_no_historical_raw_proof(self):
        manifest = self.capture_manifest()
        unused = self.changed(lambda c: c["project"]["tasks"][0].update(instruction="Unused failed run"))
        path = self.save_configuration("unused-failed-run", unused)
        path.with_name("project.toml").unlink()
        Path(unused["configuration"]["projectPath"]).unlink()
        self.assertEqual(self.resolve_manifest(manifest), self.identity["configuration"])

    def test_historical_raw_source_must_be_explicit_when_not_archived_in_its_run(self):
        original = copy.deepcopy(self.identity)
        archive = self.save_configuration().with_name("project.toml")
        raw = archive.read_bytes()
        archive.unlink()
        self.identity = self.changed(lambda c: c["project"]["tasks"][0].update(target_count=8))
        with self.assertRaisesRegex(CaptureError, "Raw project source is missing for the saved project digest"):
            self.check_contract(original)
        declared = self.directory / "declared-projects" / "nested" / "saved.toml"
        declared.parent.mkdir(parents=True)
        declared.write_bytes(raw)
        for source in (declared, declared.parent.parent):
            with self.subTest(source=source.name):
                self.identity["project_sources"] = [str(source)]
                self.assertEqual(self.check_contract(original), original["configuration"])

    def test_raw_source_comparison_normalizes_recursive_crlf_strings_without_changing_hash(self):
        configuration = self.identity["configuration"]
        path = Path(configuration["projectPath"])
        text, count = re.subn(r'^instruction = .*$', 'instruction = """\nFirst\nSecond"""',
                              path.read_bytes().decode(), flags=re.MULTILINE)
        self.assertEqual(count, 1)
        raw = text.replace("\n", "\r\n").encode()
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
        self.assertEqual(dataset.dataset_status(self.root, self.identity)["counts"]["editor"], 0)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), self.identity["project_sha256"])

    def test_raw_project_omitted_stability_regions_uses_current_loader_default(self):
        configuration = self.identity["configuration"]
        path = Path(configuration["projectPath"])
        raw, count = re.subn(rb'^stability_regions = \[\]\r?\n', b'', path.read_bytes(), flags=re.MULTILINE)
        self.assertEqual(count, 1)
        path.write_bytes(raw)
        configuration["digests"]["project"] = hashlib.sha256(raw).hexdigest()
        self.identity.update(configuration_identity(configuration))
        self.assertEqual(dataset.dataset_status(self.root, self.identity)["counts"]["editor"], 0)

    def test_project_sources_are_mandatory_and_existing_absolute_local_paths(self):
        missing = {key: value for key, value in self.identity.items() if key != "project_sources"}
        with self.assertRaisesRegex(CaptureError, "project_sources"):
            validate_identity(missing)
        for value in (None, "source.toml", ["relative.toml"], [str(self.directory / "missing")]):
            with self.subTest(value=value), self.assertRaises(CaptureError):
                validate_identity({**self.identity, "project_sources": value})

    def test_cross_run_missing_capture_snapshot_cannot_skip_forged_finalization_proof(self):
        manifest = self.cross_run_manifest()
        self.saved_configuration().unlink()
        path = Path(manifest["evidence_directory"]) / "sample.json"
        sample = json.loads(path.read_bytes())
        sample["finalization_directory"] = str(self.directory / "forged" / "run-b")
        path.write_text(json.dumps(sample), encoding="utf-8")
        with self.assertRaisesRegex(CaptureError, "Original capture run configuration is missing"):
            self.resolve_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
