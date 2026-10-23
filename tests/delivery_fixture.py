"""完整假设备来源、真实项目原文与真实 SQLite 的声明式交付夹具。"""

import copy
import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from har_capture import delivery_operations as operations
from har_capture.collector import capture
from har_capture.dataset_identity import configuration_identity
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json
from har_capture.signatures import digest
from tests.support import fake_hdc, save_page_stability, synthetic_closing_geometry, synthetic_screen_geometry

TARGET = {"window_id": 92, "bundle_name": "com.example.settings"}


def write_project(configuration):
    """用项目已有 TOML 库写真实原文，摘要来自实际字节。"""
    script = ("import { stringify } from 'smol-toml'; let value=''; "
              "for await (const chunk of process.stdin) value+=chunk; "
              "process.stdout.write(stringify(JSON.parse(value)));")
    result = subprocess.run(["node", "--input-type=module", "-e", script],
                            input=json.dumps(configuration["project"]).encode(), capture_output=True,
                            cwd=Path(__file__).resolve().parent.parent, check=True)
    path = Path(configuration["projectPath"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(result.stdout)
    configuration["digests"]["project"] = hashlib.sha256(result.stdout).hexdigest()
    return result.stdout


def save_configuration(identity, directory):
    """保存原项目及其原快照，不从新规则重建历史。"""
    configuration = identity["configuration"]
    raw = write_project(configuration)
    identity.update(configuration_identity(configuration))
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "project.toml").write_bytes(raw)
    atomic_json(directory / "configuration.json", configuration)
    return directory


def delivery(artifact_count=5, directory="."):
    """返回五种已存在来源，可明确选择第六份元数据。"""
    artifacts = [("accessibility", "tree.json"), ("application_accessibility", "windows_tree.json"),
                 ("application_arkui", "windows_akui.json"), ("screenshot", "shot.png"),
                 ("windows", "wms.json"), ("capture_metadata", "metadata.json")]
    return {"directory": directory, "artifacts": [
        {"source": source, "path": "{app}_{page_id}_{scene}_{state_id}_" + suffix}
        for source, suffix in artifacts[:artifact_count]]}


def snapshot_files(paths):
    """记录指定已存在文件集合的字节摘要及修改时间。"""
    files = set()
    for path in paths:
        path = Path(path)
        files.update(item for item in path.rglob("*") if item.is_file()) if path.is_dir() else files.add(path)
    return {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in files}


class CaptureFixture:
    """几何模块使用明确替身，其他来源与验证均走真实完整采集协议。"""

    def __init__(self, directory, quota=3):
        self.directory = Path(directory)
        self.root = self.directory / "internal"
        self.run_name = "run-a"
        self.policy = {"enabled": False, "reason": "Synthetic complete capture"}
        self.options = {"canvas": {"width": 3120, "height": 2080}, "stability_regions": []}
        configuration = {"project": {"schema_version": 1,
            "run": {"name": "capture-test", "output": "output", "max_actions": 20,
                    "max_duration_seconds": 300}, "agent": {"llm": "vision"},
            "dataset": {"root": "internal", **self.options}, "tools": {"enabled": ["ReadWindows", "Capture"]},
            "tasks": [{"name": "forms", "target_count": quota, "app": "Example", "scene": "Forms",
                       "bundle_names": [TARGET["bundle_name"]], "privacy": copy.deepcopy(self.policy),
                       "instruction": "Find a real page", "acceptance": "Body is visible"}],
            "delivery": delivery()},
            "profile": {"provider": "openai_compatible", "base_url": "https://model.example/v1",
                        "model": "vision", "model_family": "vlm-ui-tars", "api_key_env": "MODEL_KEY",
                        "supports_vision": True},
            "projectPath": str(self.directory / "project.toml"),
            "configPath": str(self.directory / "config.toml"),
            "outputPath": str(self.directory / "output"), "datasetPath": str(self.root),
            "digests": {"project": "0" * 64, "config": "0" * 64}}
        model = b'[model]\nname = "synthetic-vision"\n'
        Path(configuration["configPath"]).write_bytes(model)
        configuration["digests"]["config"] = hashlib.sha256(model).hexdigest()
        self.identity = {**configuration_identity(configuration), "configuration": configuration, "project_sources": []}
        self.save_configuration()

    def save_configuration(self):
        return save_configuration(self.identity, self.root / "runs" / self.run_name)

    @contextmanager
    def patch_geometry(self):
        """仅替代本夹具不含真实变换的几何证明，保留原始摘要及所有其他门槛。"""
        with ExitStack() as stack:
            stack.enter_context(patch("har_capture.window_geometry.attach_captured_screen_geometry",
                                      side_effect=synthetic_screen_geometry))
            stack.enter_context(patch("har_capture.window_geometry.read_window_geometry",
                                      side_effect=synthetic_closing_geometry))
            stack.enter_context(patch("har_capture.capture_facts.load_scene_evidence", return_value={}))
            stack.enter_context(patch("har_capture.capture_facts.normalized_arkui", side_effect=self.normalized))
            yield

    @staticmethod
    def normalized(evidence, window, canvas, scenes):
        tree = json.loads((evidence / window["trees"]["arkui"]["file"]).read_bytes())
        tree["coordinate_validation"] = {"status": "verified", "method": "ExplicitSyntheticGeometryFixture"}
        return {"window_id": window["window_id"], "bundle_name": window["bundle_name"], "tree": tree}

    def capture(self, capture_id=None, payloads=None, command_outputs=None):
        run = self.save_configuration()
        result = capture(fake_hdc(payloads, command_outputs=command_outputs), run / "captures", target=TARGET)
        evidence = result["path"]
        if capture_id is not None:
            renamed = evidence.parent / capture_id
            evidence.rename(renamed)
            evidence = renamed
            for filename in ("metadata.json", "capture-evidence.json"):
                metadata = json.loads((evidence / filename).read_bytes())
                metadata["capture_id"] = capture_id
                atomic_json(evidence / filename, metadata)
        metadata = json.loads((evidence / "metadata.json").read_bytes())
        save_page_stability(evidence, metadata)
        atomic_json(evidence / "candidate.json", {"capture_id": metadata["capture_id"], "task_name": "forms",
            "target_bundle_name": TARGET["bundle_name"], "page_name": "Real form", "path": str(evidence),
            "window_changes": []})
        return evidence

    def prepare(self, evidence):
        return operations.prepare_capture({"directory": str(evidence), "privacy": self.policy,
                                           "options": self.options})

    def acceptance(self, prepared):
        return {"pass": True, "task_name": "forms", "target_bundle_name": TARGET["bundle_name"],
                "accepted_screenshot_sha256": prepared["manifest"]["screenshot"]["sha256"],
                "model_usage": {"calls": 0}, "capture": prepared["manifest"]}

    def accept(self, evidence, prepared=None):
        prepared = self.prepare(evidence) if prepared is None else prepared
        return operations.publish_capture({"root": str(self.root), "identity": self.identity,
            "directory": prepared["manifest"]["path"], "evidence_directory": str(evidence),
            "acceptance": self.acceptance(prepared)})

    def export(self, sample, result_name="001-running", declaration=None):
        if declaration is not None:
            self.identity["configuration"]["project"]["delivery"] = copy.deepcopy(declaration)
            self.run_name = "export-" + result_name
            self.save_configuration()
        destination = Path(self.identity["configuration"]["outputPath"]) / result_name
        destination.mkdir(parents=True, exist_ok=True)
        if not (destination / "manifest.json").exists():
            atomic_json(destination / "manifest.json", {"status": "running", "samples": []})
        return operations.export_sample({"root": str(self.root), "identity": self.identity,
            "sample_id": sample["sample_id"], "delivery": self.identity["configuration"]["project"]["delivery"],
            "destination": str(destination), "receipt": str(self.root / "runs" / self.run_name / "export.json")})

    def historical_five_files(self, sample):
        """构造明确完整的已发布五文件来源，不靠缺字段推断历史格式。"""
        from har_capture.dataset import read_accepted_sample
        from har_capture.export import windows_payload
        _, facts, _ = read_accepted_sample(self.root, self.identity, sample["sample_id"])
        location = Path(sample["path"])
        for path in location.iterdir():
            path.unlink()
        prefix = f"Example_{sample['page_id']}_Forms_{sample['state_id']}"
        mapping = {"tree": "_tree.json", "windows_tree": "_windows_tree.json",
                   "windows_akui": "_windows_akui.json", "screenshot": "_shot.png", "wms": "_wms.json"}
        paths = {"application_accessibility": prefix + mapping["windows_tree"],
                 "application_arkui": prefix + mapping["windows_akui"], "windows": prefix + mapping["wms"]}
        naming = {**sample["publication_sample"], "capture_id": sample["sample_id"], "task": sample["task_name"]}
        app_ids = {window["window_id"] for window in facts["windows"]["app_windows"]}
        payloads = {"tree": facts["accessibility"],
                    "windows_tree": {**facts["accessibility"], "windows": [window for window in facts["accessibility"]["windows"] if window["window_id"] in app_ids]},
                    "windows_akui": facts["arkui"], "wms": windows_payload(facts, paths, naming)}
        files = {}
        for kind, suffix in mapping.items():
            target = location / (prefix + suffix)
            shutil.copyfile(facts["screenshot"]["file"], target) if kind == "screenshot" else atomic_json(target, payloads[kind])
            files[kind] = file_info(target)
        original = Path(facts["screenshot"]["file"])
        original.unlink()
        sample = {**sample, "files": files, "source_files": copy.deepcopy(files)}
        stored = {key: value for key, value in sample.items() if key != "path"}
        with closing(sqlite3.connect(self.root / "dataset.sqlite3")) as connection:
            connection.execute("UPDATE samples SET manifest=? WHERE sample_id=?",
                               (json.dumps(stored), sample["sample_id"]))
            connection.commit()
        evidence = Path(sample["evidence_directory"])
        (evidence / "capture-preparation.json").unlink()
        metadata = json.loads((evidence / "metadata.json").read_bytes())
        manifest = {"status": "complete", "capture_id": sample["sample_id"], "path": sample["path"],
                    "files": files, "checks": sample["checks"], "canvas": facts["canvas"], "publication": sample}
        metadata["delivery"] = manifest
        metadata["files"]["screenshot"] = {**files["screenshot"], "file": str(location / files["screenshot"]["file"])}
        atomic_json(evidence / "metadata.json", metadata)
        atomic_json(evidence / "publication.json", sample)
        atomic_json(evidence / "delivery-manifest.json", manifest)
        atomic_json(evidence / "delivery-preparation.json", {"rule_version": 1, "status": "complete",
            "capture_id": sample["sample_id"], "rules_sha256": digest({"options": {**self.options,
                **sample["publication_sample"]}, "privacy": self.policy}),
            "screenshot_sha256": files["screenshot"]["sha256"],
            "manifest_sha256": digest({key: manifest[key] for key in ("status", "capture_id", "files", "checks", "canvas")})})
        return sample
