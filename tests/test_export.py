"""声明式产物、真实文件清单及完整事实门槛测试。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from har_capture.capture_facts import build_capture_facts, classify_window_mode, normalized_arkui
from har_capture import export
from har_capture.hdc import CaptureError
from har_capture.privacy import atomic_json
from tests.delivery_fixture import CaptureFixture, delivery, snapshot_files


def window(window_id=1, bundle="app.test", x=0, width=4, window_type=1):
    return {"window_id": window_id, "bundle_name": bundle, "window_type": window_type,
            "window_mode": 102, "screen_bounds": {"x": x, "y": 0, "width": width, "height": 6}}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = CaptureFixture(Path(self.temporary.name))
        self.geometry = self.fixture.patch_geometry()
        self.geometry.__enter__()
        self.addCleanup(self.geometry.__exit__, None, None, None)
        self.evidence = self.fixture.capture("frame-a")
        prepared = self.fixture.prepare(self.evidence)
        self.facts = json.loads((Path(prepared["manifest"]["path"]) / "capture.json").read_bytes())
        self.naming = {"app": "Example", "scene": "Forms", "task": "forms", "capture_id": "frame-a",
                       "page_id": "page", "state_id": "000001"}
        self.output = Path(self.temporary.name) / "results" / "001-running"
        self.output.mkdir(parents=True)
        atomic_json(self.output / "manifest.json", {"status": "running", "samples": []})
        self.receipt = self.fixture.root / "runs" / "exports" / "receipt.json"

    def write(self, declaration):
        return export.write_export(self.facts, declaration, self.naming, self.output, self.receipt)

    def test_one_source_and_six_sources_are_real_files_and_preserve_evidence(self):
        before = snapshot_files([self.evidence])
        result = self.write({"directory": ".", "artifacts": [{"source": "screenshot", "path": "frame.png"}]})
        self.assertEqual(set(result["files"]), {"screenshot"})
        self.assertEqual((self.output / "frame.png").read_bytes(), Path(self.facts["screenshot"]["file"]).read_bytes())
        second = self.output.parent / "002-running"
        second.mkdir()
        atomic_json(second / "manifest.json", {"status": "running", "samples": []})
        result = export.write_export(self.facts, delivery(6), self.naming, second, self.receipt.with_name("six.json"))
        self.assertEqual(len(result["files"]), 6)
        self.assertEqual(len(list(second.iterdir())), 7)
        self.assertEqual(before, snapshot_files([self.evidence]))

    def test_nested_window_references_only_name_selected_real_application_trees(self):
        declaration = {"directory": "{app}/{capture_id}", "artifacts": [
            {"source": "application_accessibility", "path": "trees/access.json"},
            {"source": "application_arkui", "path": "arkui.json"},
            {"source": "windows", "path": "windows/info.json"}]}
        self.write(declaration)
        windows = json.loads((self.output / "Example/frame-a/windows/info.json").read_bytes())
        for item in windows["app_windows"]:
            self.assertEqual(item["trees"]["accessibility"]["file"], "../trees/access.json")
            self.assertEqual(item["trees"]["arkui"]["file"], "../arkui.json")
        self.assertNotIn("trees", self.facts["windows"]["app_windows"][0])

    def test_windows_only_does_not_fabricate_paired_tree_files(self):
        self.write({"directory": ".", "artifacts": [{"source": "windows", "path": "windows.json"}]})
        document = json.loads((self.output / "windows.json").read_bytes())
        self.assertTrue(all("trees" not in item for item in document["app_windows"]))
        self.assertEqual({path.name for path in self.output.iterdir()}, {"windows.json", "manifest.json"})

    def test_one_selected_application_tree_has_only_its_own_reference(self):
        self.write({"directory": ".", "artifacts": [
            {"source": "application_arkui", "path": "components.json"},
            {"source": "windows", "path": "windows.json"}]})
        document = json.loads((self.output / "windows.json").read_bytes())
        self.assertTrue(all(set(item["trees"]) == {"arkui"} for item in document["app_windows"]))

    def test_raw_sources_keep_original_bytes_and_capture_metadata_is_actual(self):
        self.write({"directory": "raw", "artifacts": [
            {"source": "raw:layout", "path": "layout-original.json"},
            {"source": "capture_metadata", "path": "metadata.json"}]})
        self.assertEqual((self.output / "raw/layout-original.json").read_bytes(),
                         (self.evidence / self.facts["metadata"]["files"]["layout"]["file"]).read_bytes())
        self.assertEqual(json.loads((self.output / "raw/metadata.json").read_bytes()), self.facts["metadata"])

    def test_missing_source_is_rejected_before_any_artifact_or_receipt_write(self):
        with self.assertRaisesRegex(CaptureError, "not indexed"):
            self.write({"directory": ".", "artifacts": [
                {"source": "screenshot", "path": "valid.png"}, {"source": "raw:missing", "path": "missing.txt"}]})
        self.assertEqual({path.name for path in self.output.iterdir()}, {"manifest.json"})
        self.assertFalse(self.receipt.exists())

    def test_path_placeholders_and_source_uniqueness_are_strict(self):
        bad_paths = ["../escape", "/absolute", "C:/absolute", "folder/../escape", "folder//file",
                     "manifest.json", "{app.__class__}.json", "{app[0]}.json", "{app!r}.json", "{app:>3}.json"]
        for path in bad_paths:
            with self.subTest(path=path), self.assertRaises((CaptureError, ValueError)):
                self.write({"directory": ".", "artifacts": [{"source": "capture_metadata", "path": path}]})
        for artifacts in ([{"source": "windows", "path": "a"}, {"source": "windows", "path": "b"}],
                          [{"source": "windows", "path": "a"}, {"source": "capture_metadata", "path": "A"}],
                          [{"source": "windows", "path": "a"}, {"source": "capture_metadata", "path": "a/b"}]):
            with self.subTest(artifacts=artifacts), self.assertRaises(CaptureError):
                self.write({"directory": ".", "artifacts": artifacts})

    def test_interrupted_export_reuses_existing_bytes_and_fills_only_missing_files(self):
        declaration = delivery(6)
        original = export.write_artifacts

        def interrupted(destination, plan, contents):
            first = next(iter(plan["files"]))
            original(destination, {**plan, "files": {first: plan["files"][first]}}, {first: contents[first]})
            raise RuntimeError("Injected interruption after one artifact")

        with patch.object(export, "write_artifacts", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.write(declaration)
        existing = next(path for path in self.output.iterdir() if path.name != "manifest.json")
        before = snapshot_files([existing])
        self.assertEqual(json.loads(self.receipt.read_bytes())["status"], "started")
        result = self.write(declaration)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(before, snapshot_files([existing]))
        self.write(declaration)
        self.assertEqual(len(json.loads((self.output / "manifest.json").read_bytes())["samples"]), 1)

    def test_interrupted_partial_image_copy_resumes_from_owned_temporary_file(self):
        declaration = delivery(6)
        before = snapshot_files([self.evidence])

        def half_copy(source, target):
            target.write(source.read(16))
            raise RuntimeError("Interrupted inside screenshot copy")

        with patch("har_capture.export.shutil.copyfileobj", side_effect=half_copy), self.assertRaises(RuntimeError):
            self.write(declaration)
        plan = json.loads(self.receipt.read_bytes())
        screenshot = plan["files"]["screenshot"]
        final = self.output / screenshot["file"]
        temporary = export.pending_path(self.output, screenshot["file"])
        self.assertFalse(final.exists())
        self.assertEqual(temporary.read_bytes(), Path(self.facts["screenshot"]["file"]).read_bytes()[:16])
        published = [path for path in self.output.iterdir() if path.is_file() and path.name != "manifest.json"]
        published_before = snapshot_files(published)
        result = self.write(declaration)
        self.assertEqual(len(result["files"]), 6)
        self.assertEqual(final.read_bytes(), Path(self.facts["screenshot"]["file"]).read_bytes())
        self.assertFalse((self.output / ".pending").exists())
        self.assertEqual(before, snapshot_files([self.evidence]))
        self.assertEqual(published_before, snapshot_files(published))
        self.assertEqual(len(list(self.output.iterdir())), 7)

    def test_partial_run_manifest_write_resumes_only_its_exact_planned_prefix(self):
        original = export.atomic_json

        def interrupted(path, value):
            if path == self.output / "manifest.json":
                data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
                path.with_suffix(path.suffix + ".pending").write_bytes(data[:len(data) // 2])
                raise RuntimeError("Interrupted during manifest write")
            return original(path, value)

        with patch.object(export, "atomic_json", side_effect=interrupted), self.assertRaises(RuntimeError):
            self.write(delivery(6))
        pending = self.output / "manifest.json.pending"
        self.assertTrue(pending.is_file())
        self.assertEqual(json.loads((self.output / "manifest.json").read_bytes())["samples"], [])
        published = [path for path in self.output.iterdir() if path.name not in ("manifest.json", pending.name)]
        before = snapshot_files(published)
        result = self.write(delivery(6))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(before, snapshot_files(published))
        self.assertFalse(pending.exists())
        self.assertFalse((self.output / ".pending").exists())
        self.assertEqual(len(json.loads((self.output / "manifest.json").read_bytes())["samples"]), 1)

    def test_temporary_artifact_without_a_matching_receipt_is_not_claimed(self):
        declaration = delivery()
        plan, _ = export.export_plan(self.facts, declaration, self.naming)
        temporary = export.pending_path(self.output, next(iter(plan["files"].values()))["file"])
        temporary.parent.mkdir()
        temporary.write_bytes(b"unowned partial bytes")
        before = snapshot_files([self.output])
        with self.assertRaisesRegex(CaptureError, "durable receipt"):
            self.write(declaration)
        self.assertEqual(before, snapshot_files([self.output]))
        self.assertFalse(self.receipt.exists())

    def test_unknown_temporary_file_is_not_deleted_even_with_a_matching_receipt(self):
        declaration = delivery()
        plan, _ = export.export_plan(self.facts, declaration, self.naming)
        export.prepare_receipt(self.receipt, plan)
        (self.output / ".pending").mkdir()
        (self.output / ".pending" / "unknown").write_bytes(b"unowned")
        before = snapshot_files([self.output, self.receipt])
        with self.assertRaisesRegex(CaptureError, "unknown temporary artifacts"):
            self.write(declaration)
        self.assertEqual(before, snapshot_files([self.output, self.receipt]))

    def test_unknown_manifest_pending_is_not_rewritten_or_deleted(self):
        declaration = delivery()
        plan, _ = export.export_plan(self.facts, declaration, self.naming)
        export.prepare_receipt(self.receipt, plan)
        (self.output / "manifest.json.pending").write_text('{"status":"different transaction"}')
        before = snapshot_files([self.output, self.receipt])
        with self.assertRaisesRegex(CaptureError, "differs from its durable export plan"):
            self.write(declaration)
        self.assertEqual(before, snapshot_files([self.output, self.receipt]))

    def test_manifest_pending_without_receipt_is_not_claimed(self):
        (self.output / "manifest.json.pending").write_bytes(b'{\n  "status":')
        before = snapshot_files([self.output])
        with self.assertRaisesRegex(CaptureError, "durable receipt"):
            self.write(delivery())
        self.assertEqual(before, snapshot_files([self.output]))
        self.assertFalse(self.receipt.exists())

    def test_completed_plan_only_cleans_its_owned_temporary_residue(self):
        declaration = delivery()
        result = self.write(declaration)
        published = [self.output / item["file"] for item in result["files"].values()]
        before = snapshot_files(published)
        temporary = export.pending_path(self.output, next(iter(result["files"].values()))["file"])
        temporary.parent.mkdir()
        temporary.write_bytes(b"owned leftover")
        manifest = json.loads((self.output / "manifest.json").read_bytes())
        data = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        (self.output / "manifest.json.pending").write_bytes(data)
        self.write(declaration)
        self.assertEqual(before, snapshot_files(published))
        self.assertFalse((self.output / ".pending").exists())
        self.assertFalse((self.output / "manifest.json.pending").exists())

    def test_reserved_windows_names_and_private_paths_are_rejected_before_writes(self):
        for path in ("CON", "NUL.png", "COM1.json", "LPT1.txt", "folder/aux.bin", "folder/PRN",
                     "manifest.json.pending", ".pending/anything"):
            with self.subTest(path=path), self.assertRaises(CaptureError):
                self.write({"directory": ".", "artifacts": [{"source": "screenshot", "path": path}]})
            self.assertEqual({item.name for item in self.output.iterdir()}, {"manifest.json"})
            self.assertFalse(self.receipt.exists())
        with self.assertRaises(CaptureError):
            self.write({"directory": "COM9", "artifacts": [{"source": "screenshot", "path": "valid.png"}]})

    def test_normal_pending_extension_does_not_conflict_with_temporary_artifact_storage(self):
        self.write({"directory": ".", "artifacts": [
            {"source": "screenshot", "path": "a.pending"}, {"source": "capture_metadata", "path": "a"}]})
        self.assertEqual((self.output / "a.pending").read_bytes(), Path(self.facts["screenshot"]["file"]).read_bytes())
        self.assertEqual(json.loads((self.output / "a").read_bytes()), self.facts["metadata"])
        self.assertEqual({item.name for item in self.output.iterdir()}, {"a.pending", "a", "manifest.json"})

    def test_existing_conflicts_unknown_files_and_changed_receipt_rules_are_not_overwritten(self):
        declaration = {"directory": ".", "artifacts": [{"source": "screenshot", "path": "shot.png"}]}
        self.write(declaration)
        before = snapshot_files([self.output, self.receipt])
        changed = copy.deepcopy(declaration)
        changed["artifacts"][0]["path"] = "other.png"
        with self.assertRaises(CaptureError):
            self.write(changed)
        self.assertEqual(before, snapshot_files([self.output, self.receipt]))
        (self.output / "shot.png").write_bytes(b"conflicting content")
        with self.assertRaisesRegex(CaptureError, "conflicts"):
            self.write(declaration)
        self.assertEqual((self.output / "shot.png").read_bytes(), b"conflicting content")

    def test_unindexed_output_file_prevents_export(self):
        (self.output / "unknown.txt").write_text("retain this file")
        with self.assertRaisesRegex(CaptureError, "outside its manifest"):
            self.write(delivery())
        self.assertFalse(self.receipt.exists())
        self.assertEqual((self.output / "unknown.txt").read_text(), "retain this file")

    def test_fewer_selected_files_still_require_every_application_arkui(self):
        self.fixture.identity["configuration"]["project"]["delivery"] = {
            "directory": ".", "artifacts": [{"source": "screenshot", "path": "one.png"}]}
        with patch("har_capture.capture_facts.normalized_arkui", side_effect=CaptureError("Unverified app geometry")):
            with self.assertRaisesRegex(CaptureError, "Unverified app geometry"):
                build_capture_facts(self.evidence, self.facts["metadata"], self.fixture.options)
        self.assertEqual({path.name for path in self.output.iterdir()}, {"manifest.json"})

    def test_canonical_facts_keep_every_covered_tree_and_complete_raw_window_inventory(self):
        covered = set(self.facts["metadata"]["coverage"]["covered_window_ids"])
        applications = {window["window_id"] for window in self.facts["windows"]["app_windows"]}
        self.assertEqual({item["window_id"] for item in self.facts["accessibility"]["windows"]}, covered)
        self.assertEqual({item["window_id"] for item in self.facts["arkui"]["windows"]}, applications)
        self.assertEqual({item["window_id"] for item in self.facts["windows"]["windows"]},
                         {item["window_id"] for item in self.facts["metadata"]["window_inventory"]})

    def test_capture_facts_refuse_metadata_not_saved_in_original_evidence(self):
        changed = copy.deepcopy(self.facts["metadata"])
        changed["windows"][0]["z_order"] += 1
        with self.assertRaisesRegex(CaptureError, "actual saved metadata"):
            build_capture_facts(self.evidence, changed, self.fixture.options)

    def test_failed_native_coordinate_normalization_never_writes_failure_files_into_evidence(self):
        current = self.facts["metadata"]["windows"][0]
        before = snapshot_files([self.evidence])
        with patch("har_capture.coordinates.normalize_arkui", return_value={
                "coordinate_validation": {"status": "failed", "issues": ["Synthetic proof failure"]}}):
            with self.assertRaisesRegex(CaptureError, "coordinates are not verified"):
                normalized_arkui(self.evidence, current, self.facts["canvas"], {current["window_id"]: {}})
        self.assertEqual(before, snapshot_files([self.evidence]))

    def test_all_window_modes_and_touching_edges_use_exact_physical_bounds(self):
        one = window()
        self.assertEqual(classify_window_mode([{**one, "window_mode": 1}]), "单窗口全屏")
        self.assertEqual(classify_window_mode([one]), "单窗口缩放")
        for bundle, label in (("app.test", "单应用多窗口"), ("app.other", "多应用多窗口")):
            self.assertEqual(classify_window_mode([one, window(2, bundle, x=4)]), label + "并列")
            self.assertEqual(classify_window_mode([one, window(2, bundle, x=3)]), label + "交叉")
        self.assertEqual(classify_window_mode([one, window(2, window_type=1001)]), "单窗口缩放")
