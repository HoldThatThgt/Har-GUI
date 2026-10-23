"""@brief 只按原始来源核验发布记录，恢复四次原子写入的明确中断状态。"""

import copy
import json
from pathlib import Path

from har_capture.dataset_identity import finalization_configuration, manifest_configuration, relocate_evidence
from har_capture.hdc import fail


def read_json(path: Path) -> dict:
    """@brief 读取必须存在的普通发布证据文件。 @param path 原始文件。 @return JSON 对象。"""
    if path.is_symlink() or not path.is_file():
        fail(f"Published evidence is missing or is a symbolic link: {path.name}")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        fail(f"Cannot read published evidence {path.name}: {type(error).__name__}")
    if not isinstance(value, dict):
        fail(f"Published evidence is not an object: {path.name}")
    return value


def publication_context(sample: dict, identity: dict, sources: dict) -> dict:
    """@brief 关联原运行来源与当前实际目录，不改写持久路径。 @param sample 已登记样本。
    @param identity 本次身份。 @param sources 已核验快照。 @return 来源和实际读写位置。
    """
    historical = manifest_configuration(sample, identity, sources)
    return {"configuration": historical, "identity": identity, "sources": sources,
            "directory": relocate_evidence(sample, historical, identity["configuration"])}


def direct_candidate(sample: dict, context: dict) -> dict:
    """@brief 要求直接采集的原样本及其独立验收运行证明。 @param sample 原登记清单。
    @param context 原来源和实际位置。 @return 已保存的直接采集身份。
    """
    candidate = read_json(context["directory"] / "sample.json")
    if (candidate.get("origin") != "direct_capture" or candidate.get("page_name") != sample["sample_id"]
            or candidate.get("window_changes") != []):
        fail("Missing candidate requires a recorded direct capture sample identity")
    finalization_configuration(sample, candidate, context["identity"], context["sources"])
    return candidate


def original_preparation(sample: dict, context: dict) -> dict:
    """@brief 从原任务及候选身份重建准备参数，保留原隐私理由。 @param sample 已登记样本。
    @param context 原来源和实际位置。 @return 原准备请求。
    """
    historical, directory = context["configuration"], context["directory"]
    task = next(task for task in historical["project"]["tasks"] if task["name"] == sample["task_name"])
    path = directory / "candidate.json"
    if path.is_symlink():
        fail("Published candidate must not be a symbolic link")
    candidate = read_json(path) if path.exists() else direct_candidate(sample, context)
    if (candidate.get("capture_id") != sample["sample_id"] or candidate.get("task_name") != task["name"]
            or candidate.get("target_bundle_name") != sample["target_bundle_name"]
            or not isinstance(candidate.get("page_name"), str) or not candidate["page_name"].strip()
            or not isinstance(candidate.get("path"), str)
            or Path(candidate["path"]).resolve() != Path(sample["evidence_directory"]).resolve()):
        fail("Published candidate differs from its original task or capture")
    return {"directory": str(directory), "privacy": task["privacy"], "options": {
        "app": task["app"], "scene": task["scene"], "page_id": candidate["page_name"],
        "state_id": sample["sample_id"], "canvas": historical["project"]["dataset"]["canvas"],
        "stability_regions": historical["project"]["dataset"]["stability_regions"]}}


def delivery_versions(sample: dict, context: dict) -> tuple[dict, dict, dict]:
    """@brief 从持久命名计划恢复发布前后清单，路径始终保留原项目含义。 @param sample 原登记清单。
    @param context 原来源。 @return 原准备清单、最终清单和原发布收据。
    """
    historical = context["configuration"]
    original_path = Path(historical["datasetPath"]) / "samples" / sample["sample_id"]
    receipt = {**sample, "path": str(original_path)}
    prepared = {"status": "complete", "capture_id": sample["sample_id"],
                "path": str(Path(sample["evidence_directory"]) / "delivery"), "files": sample["source_files"],
                "checks": sample["checks"], "canvas": historical["project"]["dataset"]["canvas"]}
    final = {**prepared, "path": receipt["path"], "files": sample["files"], "publication": receipt}
    return prepared, final, receipt


def metadata_versions(metadata: dict, deliveries: tuple, sample: dict) -> tuple[dict, dict]:
    """@brief 仅构造同步阶段拥有的两个元数据版本。 @param metadata 当前元数据。
    @param deliveries 准备和最终清单。 @param sample 原登记样本。 @return 原准备及最终元数据。
    """
    if metadata.get("capture_id") != sample["sample_id"]:
        fail("Published evidence capture identity does not match")
    before, after = copy.deepcopy(metadata), copy.deepcopy(metadata)
    before["delivery"], after["delivery"] = deliveries
    before["files"]["screenshot"] = {**sample["source_files"]["screenshot"],
                                     "file": "delivery/" + sample["source_files"]["screenshot"]["file"]}
    after["files"]["screenshot"] = {**sample["files"]["screenshot"],
                                    "file": str(Path(deliveries[1]["path"]) / sample["files"]["screenshot"]["file"])}
    return before, after


def preparation_versions(receipt: dict, deliveries: tuple) -> tuple[dict, dict]:
    """@brief 仅允许原准备摘要到最终命名摘要的变更。 @param receipt 原准备凭证。
    @param deliveries 准备和最终清单。 @return 两个明确阶段的凭证。
    """
    from har_capture.delivery_operations import manifest_digest
    required = {"rule_version", "rules_sha256", "capture_id", "status", "screenshot_sha256", "manifest_sha256"}
    if set(receipt) != required or receipt["status"] != "complete":
        fail("Published preparation receipt is not a complete original receipt")
    return tuple({**receipt, "manifest_sha256": manifest_digest(delivery)} for delivery in deliveries)


def publication_records(sample: dict, context: dict) -> list[dict]:
    """@brief 读取四份文档并构造原子写入前后的精确计划。 @param sample 原登记清单。
    @param context 原来源和实际位置。 @return 按持久写入顺序排列的文档计划。
    """
    directory = context["directory"]
    prepared, final, publication = delivery_versions(sample, context)
    metadata = read_json(directory / "metadata.json")
    versions = metadata_versions(metadata, (prepared, final), sample)
    preparation = read_json(directory / "delivery-preparation.json")
    receipts = preparation_versions(preparation, (prepared, final))
    path = directory / "publication.json"
    if path.is_symlink():
        fail("Published receipt must not be a symbolic link")
    observed = [metadata, read_json(directory / "delivery-manifest.json"),
                read_json(path) if path.exists() else None, preparation]
    before = [versions[0], prepared, None, receipts[0]]
    after = [versions[1], final, publication, receipts[1]]
    names = ("metadata.json", "delivery-manifest.json", "publication.json", "delivery-preparation.json")
    return [{"path": directory / name, "observed": current, "before": first, "after": last}
            for name, current, first, last in zip(names, observed, before, after, strict=True)]


def verify_publication_bytes(sample: dict, context: dict, records: list[dict]) -> None:
    """@brief 写入前以实际位置核对所有原始字节、五件套及原准备规则。 @param sample 原登记样本。
    @param context 原来源和实际位置。 @param records 已核对同步顺序的四份文档。
    """
    from har_capture.delivery_operations import verify_preparation, verify_raw_files
    from har_capture.capture_metadata import verify_captured_metadata
    directory = context["directory"]
    metadata = copy.deepcopy(records[0]["after"])
    metadata["delivery"]["path"] = sample["path"]
    metadata["files"]["screenshot"]["file"] = str(Path(sample["path"]) / sample["files"]["screenshot"]["file"])
    verify_captured_metadata(directory, metadata)
    verify_raw_files(directory, metadata, False)
    request = original_preparation(sample, context)
    verify_preparation(directory, metadata, request, records[3]["after"])


def publication_plan(sample: dict, context: dict) -> list[dict]:
    """@brief 只接受既有四次写入的完整前缀，中断修复前必须核验全部来源。 @param sample 原登记样本。
    @param context 原来源和实际位置。 @return 尚未完成的精确写入计划；完整发布为空。
    """
    records = publication_records(sample, context)
    positions = [count for count in range(len(records) + 1) if all(
        record["observed"] == record["after" if index < count else "before"]
        for index, record in enumerate(records))]
    if not positions:
        fail("Published evidence does not match an interrupted atomic synchronization")
    verify_publication_bytes(sample, context, records)
    return [record for record in records[max(positions):] if record["observed"] != record["after"]]


def verify_published_evidence(sample: dict, context: dict) -> None:
    """@brief 完整已发布状态只读核验，绝不重建坐标或改写历史凭证。 @param sample 原登记样本。
    @param context 原来源和实际位置。
    """
    if publication_plan(sample, context):
        fail("Published evidence synchronization is incomplete")
