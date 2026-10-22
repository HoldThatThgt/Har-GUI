"""@brief 将离线后处理和持久化登记接入受限操作桥，并恢复中断后的证据指针。"""

import hashlib
import json
import math
from pathlib import Path

from har_capture.capture_checks import (compare_capture_states, coverage_report, session_visibility,
                                        verify_pixel_receipt)
from har_capture.dataset import SUFFIXES, dataset_status, publish_sample, read_documents, verify_files
from har_capture.export import sample_prefix, write_delivery
from har_capture.hdc import fail
from har_capture.metadata import file_info, image_info, load_layout, parse_windows
from har_capture.privacy import atomic_json, redact_capture, validate_policy, verify_privacy_receipt
from har_capture.signatures import digest
from har_capture.system_accessibility import read_window_accessibility
from har_capture.stability import resolve_stability, verify_project_rules


def verify_raw_files(directory: Path, metadata: dict, skip_screenshot: bool) -> None:
    """@brief 核验原始文件索引，只有发布截图允许引用正式样本目录。 @param directory 证据目录。
    @param metadata 采集信息。 @param skip_screenshot 让打码恢复先核对自己的图片收据。
    """
    required = {"layout": "layout.json", "windows": "windows.txt", "accessibility": "accessibility.json",
                "session_details": "session-details.txt", "coverage": "coverage.json",
                "consistency": "consistency.json", "closing_layout": "closing-layout.json",
                "closing_windows": "closing-windows.txt", "pixel_consistency": "pixel-consistency.json",
                "stability": "stability.json"}
    if any(metadata["files"].get(key, {}).get("file") != name for key, name in required.items()):
        fail("Capture is missing a required original evidence file index")
    for kind, record in metadata["files"].items():
        if kind == "screenshot" and skip_screenshot:
            continue
        path = directory / record["file"]
        if path.is_symlink() or not path.is_file():
            fail(f"Capture artifact is missing or is a symbolic link: {record['file']}")
        if not path.resolve().is_relative_to(directory):
            delivery = metadata.get("delivery", {})
            expected = Path(delivery.get("path", directory)) / delivery.get("files", {}).get(
                "screenshot", {}).get("file", "")
            if kind != "screenshot" or path.resolve() != expected.resolve():
                fail("Capture artifact path escapes its evidence directory")
        actual = file_info(path)
        if any(actual[key] != record[key] for key in ("bytes", "sha256")):
            fail(f"Capture artifact digest mismatch: {record['file']}")
    if skip_screenshot:
        return
    screenshot = (directory / metadata["files"]["screenshot"]["file"]).resolve()
    images = {path.resolve() for path in directory.rglob("*") if path.suffix.lower() in (
        ".png", ".jpg", ".jpeg", ".webp")}
    expected_images = {screenshot} if screenshot.is_relative_to(directory) else set()
    if images != expected_images:
        fail("Capture evidence must contain exactly its indexed screenshot")
    image = image_info(screenshot)
    if any(image[key] != metadata["display"][key] for key in ("width", "height")):
        fail("Capture screenshot dimensions differ from its display")


def verify_tree_references(window: dict, indexed: dict) -> None:
    """@brief 要求双树及其原文、几何证据均使用已核验索引。 @param window 采集窗口。
    @param indexed 原始文件索引。
    """
    for kind in ("accessibility", "arkui"):
        tree = window["trees"][kind]
        if not tree.get("node_count", 0) > 0:
            fail("Captured window tree is empty")
        references = [tree["artifact"]] if kind == "accessibility" else [tree, tree["raw"], tree["response"]]
        if kind == "arkui":
            geometry_keys = ("arkui_geometry", "response", "render_tree", "render_detached_tree")
            geometry = tree["coordinate_evidence"]
            references.extend(geometry[key] for key in geometry_keys)
            sources = geometry.get("scene_sources")
            if not isinstance(sources, list):
                fail("Captured window is missing its explicit SceneBoard source list")
            for source in sources:
                if not isinstance(source, dict) or not all(key in source for key in ("raw", "arkui_geometry")):
                    fail("Captured window has an invalid SceneBoard source reference")
                references.extend(source[key] for key in ("raw", "arkui_geometry"))
        for record in references:
            expected = indexed.get(record["file"])
            if expected is None or any(record[key] != expected[key] for key in ("bytes", "sha256")):
                fail("Captured window tree evidence differs from the verified file index")


def verify_capture_windows(directory: Path, metadata: dict, inventory: dict) -> None:
    """@brief 重新核对窗口事实、双树索引与全屏覆盖。 @param directory 原始目录。
    @param metadata 采集信息。 @param inventory 原始 WMS 记录。
    """
    windows = metadata["windows"]
    if not windows or len({window["window_id"] for window in windows}) != len(windows):
        fail("Capture requires distinct exposed window records")
    indexed = {record["file"]: record for record in metadata["files"].values()}
    for window in windows:
        original = inventory.get(window["window_id"])
        if (not original or any(window.get(key) != value for key, value in original.items())
                or not window.get("bundle_name") or not math.isfinite(window["z_order"])):
            fail("Captured window does not match the original WMS evidence")
        verify_tree_references(window, indexed)
        read_window_accessibility(directory, window)
    recorded = metadata["window_inventory"]
    if len(recorded) != len(inventory) or {window["window_id"] for window in recorded} != set(inventory):
        fail("Capture window inventory differs from the original WMS evidence")
    if any(any(window.get(key) != value for key, value in inventory[window["window_id"]].items())
           for window in recorded):
        fail("Capture window inventory facts differ from the original WMS evidence")
    sessions = (directory / "session-details.txt").read_text(encoding="utf-8")
    coverage = coverage_report(metadata, inventory, sessions)
    if coverage["status"] != "verified" or coverage != metadata["coverage"]:
        fail("Capture coverage does not match the original window and session evidence")
    if json.loads((directory / "coverage.json").read_bytes()) != coverage:
        fail("Capture coverage receipt differs from its metadata")
    excluded = {window["window_id"] for window in coverage["excluded_windows"]}
    if any(window_id not in excluded for window_id in metadata["foreground_window_ids_without_tree"]):
        fail("Capture has unaccounted foreground windows without trees")


def verify_capture_evidence(directory: Path, metadata: dict, skip_screenshot: bool = False) -> None:
    """@brief 为初次准备和恢复统一核验原始摘要、窗口覆盖与首尾一致性。 @param directory 原始目录。
    @param metadata 采集元数据。 @param skip_screenshot 打码恢复前只延后图片摘要核验。
    """
    if metadata.get("status") != "complete":
        fail("Cannot prepare an incomplete capture")
    verify_raw_files(directory, metadata, skip_screenshot)
    inventory = parse_windows((directory / "windows.txt").read_text(encoding="utf-8"))
    verify_capture_windows(directory, metadata, inventory)
    verify_screen_geometry(directory, metadata)
    before = {"windows": inventory, "layout": load_layout(directory / "layout.json")}
    after = {"windows": parse_windows((directory / "closing-windows.txt").read_text(encoding="utf-8")),
             "layout": load_layout(directory / "closing-layout.json")}
    proof = {"display": metadata["display"], "sessions": session_visibility(
        (directory / "session-details.txt").read_text(encoding="utf-8"))}
    stability = resolve_stability({"before": before, "after": after}, proof, metadata["stability"]["rules"])
    if stability != metadata["stability"] or stability != json.loads((directory / "stability.json").read_bytes()):
        fail("Capture stability receipt differs from its original system leaf evidence")
    state = compare_capture_states(before, after, stability)
    pixels = json.loads((directory / "pixel-consistency.json").read_bytes())
    verify_pixel_receipt(pixels, stability)
    state["pixels"] = pixels
    if state != metadata["consistency"] or state != json.loads((directory / "consistency.json").read_bytes()):
        fail("Capture consistency receipt differs from its original evidence")


def verify_screen_geometry(directory: Path, metadata: dict) -> None:
    """@brief 离线重算同批精确屏幕边界，拒绝缺失或被改写的动作验收事实。 @param directory 证据目录。
    @param metadata 已保存的采集信息。
    """
    from har_capture.window_geometry import attach_captured_screen_geometry
    observed = {**metadata, "windows": [dict(window) for window in metadata["windows"]]}
    attach_captured_screen_geometry(directory, observed)
    for expected, actual in zip(metadata["windows"], observed["windows"], strict=True):
        if any(expected.get(key) != actual[key] for key in ("screen_bounds", "screen_bounds_source")):
            fail("Captured screen geometry differs from its original SceneBoard and RenderService evidence")


def manifest_digest(manifest: dict) -> str:
    """@brief 摘要只包含不随原子移动改变的交付事实。 @param manifest 清单。 @return 规范摘要。"""
    return digest({key: manifest[key] for key in ("status", "capture_id", "files", "checks", "canvas")})


def verify_preparation(directory: Path, metadata: dict, request: dict, receipt: dict) -> dict:
    """@brief 重试时核对规则身份、五文件摘要及正文打码结果。 @param directory 证据目录。
    @param metadata 当前元数据。 @param request 当前规则。 @param receipt 持久准备凭证。 @return 已核验清单。
    """
    expected = digest({"options": request["options"], "privacy": request["privacy"]})
    if receipt.get("rule_version") != 1 or receipt["rules_sha256"] != expected:
        fail("Delivery preparation rules differ from the existing receipt")
    if receipt["capture_id"] != metadata["capture_id"]:
        fail("Delivery preparation capture identity differs")
    manifest = metadata["delivery"]
    location = Path(manifest["path"]).resolve()
    verify_files(location, manifest["files"])
    documents = read_documents(location, manifest["files"])
    wms = documents["wms"]
    naming = {key: request["options"][key] for key in ("app", "page_id", "scene", "state_id")}
    if manifest.get("publication"):
        naming = manifest["publication"]["publication_sample"]
    if wms["sample"] != naming or wms["canvas"] != request["options"]["canvas"]:
        fail("Prepared delivery does not match its naming or canvas rules")
    if wms["stability"] != metadata["stability"]:
        fail("Prepared delivery stability evidence differs from its verified original capture")
    policy_hash = hashlib.sha256(json.dumps(request["privacy"], sort_keys=True).encode()).hexdigest()
    verify_privacy_receipt(wms["privacy"])
    if (wms["privacy"]["policy_sha256"] != policy_hash or wms["privacy"] != metadata["privacy"]
            or wms["privacy"]["enabled"] != request["privacy"]["enabled"]):
        fail("Prepared delivery redaction policy differs")
    screenshot_hash = manifest["files"]["screenshot"]["sha256"]
    if screenshot_hash != receipt["screenshot_sha256"] or screenshot_hash != wms["privacy"]["after_sha256"]:
        fail("Prepared delivery screenshot differs from the redaction receipt")
    if receipt["status"] == "complete" and receipt["manifest_sha256"] != manifest_digest(manifest):
        fail("Prepared delivery manifest changed after preparation")
    return manifest


def restore_uncommitted_delivery(directory: Path, metadata: dict, request: dict) -> None:
    """@brief 只回滚未写入完成元数据的本次导出文件，以便重新执行完整验证。 @param directory 证据目录。
    @param metadata 尚未完成导出的元数据。 @param request 相同导出规则。
    """
    delivery = directory / "delivery"
    if not delivery.exists():
        return
    if delivery.is_symlink() or not delivery.is_dir():
        fail("Interrupted delivery path is not a regular directory")
    prefix = sample_prefix(request["options"])
    expected = {prefix + suffix for suffix in SUFFIXES.values()}
    if any(path.is_symlink() or not path.is_file() or path.name not in expected for path in delivery.iterdir()):
        fail("Interrupted delivery contains unrecognized files")
    shot = delivery / (prefix + SUFFIXES["screenshot"])
    original = (directory / metadata["files"]["screenshot"]["file"]).resolve()
    if original.parent != directory or original.name != "screenshot.png":
        fail("Uncommitted screenshot is outside the controlled evidence directory")
    if shot.exists():
        if original.exists() or file_info(shot)["sha256"] != metadata["files"]["screenshot"]["sha256"]:
            fail("Interrupted delivery screenshot cannot be restored unambiguously")
        shot.rename(original)
    for path in delivery.iterdir():
        path.unlink()
    delivery.rmdir()


def save_preparation(directory: Path, metadata: dict, request: dict, receipt: dict) -> dict:
    """@brief 完成重试校验并保存可重复使用的准备凭证。 @param directory 证据目录。
    @param metadata 已完成元数据。 @param request 同一规则。 @param receipt 准备凭证。 @return 结果。
    """
    verify_capture_evidence(directory, metadata)
    manifest = verify_preparation(directory, metadata, request, receipt)
    receipt.update(status="complete", manifest_sha256=manifest_digest(manifest))
    atomic_json(directory / "delivery-preparation.json", receipt)
    atomic_json(directory / "delivery-manifest.json", manifest)
    return {"manifest": manifest, "metadata": metadata}


def prepare_delivery(request: dict) -> dict:
    """@brief 独立打码后准备五件套；相同规则重试必须重新核验所有交付文件。 @param request 参数。
    @return 清单及元数据。
    """
    directory = Path(request["directory"]).resolve()
    validate_policy(request["privacy"])
    metadata = json.loads((directory / "metadata.json").read_bytes())
    verify_project_rules(metadata, request["options"])
    receipt_path = directory / "delivery-preparation.json"
    rules_hash = digest({"options": request["options"], "privacy": request["privacy"]})
    receipt = json.loads(receipt_path.read_bytes()) if receipt_path.exists() else None
    if receipt and (receipt.get("rule_version") != 1 or receipt["rules_sha256"] != rules_hash
                    or receipt["capture_id"] != metadata["capture_id"]):
        fail("Delivery preparation rules or capture identity differ from the existing receipt")
    if metadata.get("delivery"):
        if receipt is None:
            fail("Prepared delivery has no rule identity receipt")
        return save_preparation(directory, metadata, request, receipt)
    if receipt:
        restore_uncommitted_delivery(directory, metadata, request)
    elif (directory / "delivery").exists():
        fail("Unregistered delivery directory prevents preparation")
    verify_capture_evidence(directory, metadata, skip_screenshot=True)
    metadata = redact_capture(directory, request["privacy"])
    verify_raw_files(directory, metadata, skip_screenshot=False)
    receipt = {"rule_version": 1, "rules_sha256": rules_hash, "capture_id": metadata["capture_id"],
               "status": "started", "screenshot_sha256": metadata["files"]["screenshot"]["sha256"]}
    atomic_json(receipt_path, receipt)
    write_delivery(directory, metadata, request["options"])
    return save_preparation(directory, metadata, request, receipt)


def synchronize_publication(result: dict) -> None:
    """@brief 根据持久收据修复原始证据指针；可重复执行，不修改五件套。 @param result 已核验发布收据。"""
    evidence = Path(result["evidence_directory"]).resolve()
    metadata_path = evidence / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    if metadata["capture_id"] != result["sample_id"]:
        fail("Published evidence capture identity does not match")
    delivery = metadata["delivery"]
    if delivery["files"] not in (result["files"], result["source_files"]):
        fail("Published evidence file index does not match its durable receipt")
    if delivery["capture_id"] != result["sample_id"]:
        fail("Published evidence capture identity does not match its durable receipt")
    expected = evidence / "delivery"
    if Path(delivery["path"]).resolve() not in (expected, Path(result["path"]).resolve()):
        fail("Published evidence delivery pointer is outside the recorded directories")
    delivery.update(path=result["path"], files=result["files"], publication=result)
    screenshot = Path(result["path"]) / result["files"]["screenshot"]["file"]
    metadata["files"]["screenshot"] = {**result["files"]["screenshot"], "file": str(screenshot)}
    atomic_json(metadata_path, metadata)
    atomic_json(evidence / "delivery-manifest.json", delivery)
    atomic_json(evidence / "publication.json", result)
    preparation_path = evidence / "delivery-preparation.json"
    receipt = json.loads(preparation_path.read_bytes())
    receipt.update(status="complete", manifest_sha256=manifest_digest(delivery))
    atomic_json(preparation_path, receipt)


def resume_dataset(request: dict) -> dict:
    """@brief 验证已发布配额并修复在登记后中断的证据同步。 @param request 项目身份与根目录。 @return 进度。"""
    status = dataset_status(Path(request["root"]), request["identity"])
    for sample in status["samples"]:
        synchronize_publication(sample)
    return status


def publish_delivery(request: dict) -> dict:
    """@brief 持久化登记后同步原始证据的最终文件指针。 @param request 发布参数。 @return 正式样本收据。"""
    evidence = Path(request["evidence_directory"]).resolve()
    metadata = json.loads((evidence / "metadata.json").read_bytes())
    directory = Path(request["directory"]).resolve()
    if directory != (evidence / "delivery").resolve():
        fail("Publication directory must be the evidence delivery directory")
    if metadata["capture_id"] != request["acceptance"]["export"]["capture_id"]:
        fail("Publication evidence capture identity does not match")
    status = resume_dataset(request)
    existing = [sample for sample in status["samples"] if sample["sample_id"] == metadata["capture_id"]]
    if existing:
        result = existing[0]
        acceptance = request["acceptance"]
        if (acceptance["task_name"] != result["task_name"]
                or acceptance["target_bundle_name"] != result["target_bundle_name"]
                or acceptance["accepted_screenshot_sha256"] != result["visual_acceptance"]["screenshot_sha256"]):
            fail("Repeated publication request differs from its durable acceptance")
        return result
    result = publish_sample(Path(request["root"]), request["identity"], directory, request["acceptance"])
    synchronize_publication(result)
    return result


def execute_local(request: dict) -> dict:
    """@brief 执行明确注册的本地操作，不连接设备。 @param request 已校验请求。 @return 操作结果。"""
    operation = request["operation"]
    if operation == "dataset_status":
        return resume_dataset(request)
    if operation == "prepare_delivery":
        return prepare_delivery(request)
    if operation == "publish_sample":
        return publish_delivery(request)
    fail("Unsupported local delivery operation")
