"""@brief 分别准备和登记采集事实，以及对已成功采集执行只读声明式导出。"""

import json
import logging
from pathlib import Path

from har_capture.capture_facts import build_capture_facts, verify_capture_evidence
from har_capture.dataset import CHECKS, dataset_status, publish_sample, read_accepted_sample, verify_files
from har_capture.export import write_export
from har_capture.hdc import fail
from har_capture.metadata import file_info
from har_capture.page_observation import verify_page_stability
from har_capture.privacy import atomic_json, redact_capture, validate_policy
from har_capture.signatures import digest
from har_capture.stability import verify_project_rules

LOGGER = logging.getLogger(__name__)


def save_capture_preparation(directory: Path, facts: dict, rules: str) -> dict:
    """@brief 保存规范事实和准备收据，不移动图片或生成项目交付。 @param directory 原始采集目录。
    @param facts 完整规范事实。 @param rules 准备要求摘要。 @return 准备清单。
    """
    stage = directory / "accepted"
    stage.mkdir(exist_ok=True)
    record = stage / "capture.json"
    if any(path.name not in {"capture.json", "capture.json.pending"} for path in stage.iterdir()):
        fail("Capture preparation contains unrecognized artifacts")
    if record.exists() and json.loads(record.read_bytes()) != facts:
        fail("Prepared capture facts changed after preparation")
    atomic_json(record, facts)
    manifest = {"status": "complete", "capture_id": facts["capture_id"], "path": str(stage),
                "files": {"capture": file_info(record)}, "screenshot": facts["screenshot"],
                "canvas": facts["canvas"], "checks": dict.fromkeys(CHECKS, "pass")}
    receipt = {"rule_version": 1, "status": "complete", "capture_id": facts["capture_id"],
               "rules_sha256": rules, "manifest": manifest}
    path = directory / "capture-preparation.json"
    if path.exists() and json.loads(path.read_bytes()) != receipt:
        fail("Capture preparation differs from its recorded facts")
    verify_files(stage, manifest["files"])
    atomic_json(path, receipt)
    return manifest


def prepare_capture(request: dict) -> dict:
    """@brief 完整核验并准备最终图片与规范事实，选择的交付文件不参与检查。 @param request 准备参数。
    @return 供图片验收使用的完整事实清单和元数据。
    """
    directory = Path(request["directory"]).resolve()
    validate_policy(request["privacy"])
    metadata = json.loads((directory / "metadata.json").read_bytes())
    if (directory / "publication.json").exists():
        fail("Capture is already registered; export its accepted sample instead")
    verify_project_rules(metadata, request["options"])
    rules = digest({"options": request["options"], "privacy": request["privacy"]})
    receipt = directory / "capture-preparation.json"
    if receipt.exists():
        saved = json.loads(receipt.read_bytes())
        if saved.get("rule_version") != 1 or saved["rules_sha256"] != rules:
            fail("Capture preparation requirements differ from their recorded rules")
    page = verify_page_stability(directory, metadata)
    verify_capture_evidence(directory, metadata, skip_screenshot=True)
    metadata = redact_capture(directory, request["privacy"])
    metadata["page_stability"] = page
    atomic_json(directory / "metadata.json", metadata)
    facts = build_capture_facts(directory, metadata, request["options"])
    return {"manifest": save_capture_preparation(directory, facts, rules), "metadata": metadata}


def publish_capture(request: dict) -> dict:
    """@brief 只登记已通过模型验收的采集，不调用项目导出或改变原采集文件。 @param request 登记参数。
    @return 该采集的持久登记收据。
    """
    evidence = Path(request["evidence_directory"]).resolve()
    directory = Path(request["directory"]).resolve()
    if directory != evidence / "accepted":
        fail("Capture registration requires its canonical preparation directory")
    metadata = json.loads((evidence / "metadata.json").read_bytes())
    acceptance = request["acceptance"]
    if metadata["capture_id"] != acceptance["capture"]["capture_id"]:
        fail("Capture registration identity differs from its evidence")
    status = dataset_status(Path(request["root"]), request["identity"])
    existing = [sample for sample in status["samples"] if sample["sample_id"] == metadata["capture_id"]]
    if existing:
        sample = existing[0]
        if (acceptance["task_name"] != sample["task_name"]
                or acceptance["target_bundle_name"] != sample["target_bundle_name"]
                or acceptance["accepted_screenshot_sha256"] != sample["visual_acceptance"]["screenshot_sha256"]):
            fail("Repeated registration differs from its durable acceptance")
        return sample
    return publish_sample(Path(request["root"]), request["identity"], directory, acceptance)


def export_sample(request: dict) -> dict:
    """@brief 从只读成功登记按当前项目交付声明导出，不重新验收或计数。 @param request 当前声明及目标。
    @return 原登记、本次实际交付计划和零新增采集数。
    """
    identity = request["identity"]
    configuration = identity["configuration"]
    if request["delivery"] != configuration["project"]["delivery"]:
        fail("Export declaration differs from the actual project")
    destination, receipt = Path(request["destination"]).resolve(), Path(request["receipt"]).resolve()
    if destination.parent != Path(configuration["outputPath"]).resolve() or destination.is_symlink():
        fail("Delivery directory must belong to the configured output root")
    internal = Path(configuration["datasetPath"]).resolve() / "runs"
    if not receipt.is_relative_to(internal) or receipt.is_symlink():
        fail("Export receipt must belong to internal run storage")
    sample, facts, original = read_accepted_sample(Path(request["root"]), identity, request["sample_id"])
    task = next(item for item in original["project"]["tasks"] if item["name"] == sample["task_name"])
    naming = {"app": task["app"], "scene": task["scene"], "task": sample["task_name"],
              "capture_id": sample["sample_id"], "page_id": sample["page_id"], "state_id": sample["state_id"]}
    result = write_export(facts, request["delivery"], naming, destination, receipt)
    return {"sample": sample, "export": result, "new_samples": 0}


def execute_local(request: dict) -> dict:
    """@brief 执行明确的本地事实或导出操作，不连接设备。 @param request 已校验参数。 @return 操作结果。"""
    try:
        operation = request["operation"]
        if operation == "dataset_status":
            return dataset_status(Path(request["root"]), request["identity"])
        if operation == "prepare_capture":
            return prepare_capture(request)
        if operation == "publish_capture":
            return publish_capture(request)
        if operation == "export_sample":
            return export_sample(request)
        fail("Unsupported local capture or export operation")
    except BaseException:
        LOGGER.exception("Local capture or export operation failed")
        raise
