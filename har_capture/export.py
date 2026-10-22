"""@brief 将已核验的单帧证据导出为严格配对的五件套。"""

import json
import re
from pathlib import Path

from har_capture.hdc import fail
from har_capture.metadata import file_info, image_info
from har_capture.privacy import atomic_json, verify_privacy_receipt
from har_capture.system_accessibility import read_window_accessibility
from har_capture.stability import verify_project_rules
from har_capture.scene_evidence import load_scene_evidence


def write_json(path: Path, value: object) -> None:
    """@brief 保存 UTF-8 数据。 @param path 文件路径。 @param value JSON 数据。"""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sample_prefix(options: dict) -> str:
    """@brief 验证项目定义的样本文件名前缀。 @param options 应用、页面、场景、状态。 @return 前缀。"""
    parts = []
    for key in ("app", "page_id", "scene", "state_id"):
        part = options.get(key)
        if (not isinstance(part, str) or not part or part != part.strip(" .")
                or re.search(r'[\\/:*?"<>|\x00-\x1f]', part)):
            fail(f"Invalid sample naming field: {key}")
        parts.append(part)
    return "_".join(parts)


def rectangles_intersect(first: dict, second: dict) -> bool:
    """@brief 判断两个屏幕矩形是否有正面积交集。 @param first 首矩形。 @param second 次矩形。 @return 交集。"""
    return (min(first["x"] + first["width"], second["x"] + second["width"]) > max(first["x"], second["x"])
            and min(first["y"] + first["height"], second["y"] + second["height"]) > max(first["y"], second["y"]))


def classify_window_mode(windows: list[dict]) -> str:
    """@brief 按全屏可见应用主窗口判定六种窗口模式。 @param windows 全部可见应用窗口。 @return 模式名。"""
    main = [window for window in windows if window["window_type"] == 1]
    if not main:
        fail("Window mode requires at least one visible application main window")
    if len(main) == 1:
        return "单窗口全屏" if main[0]["window_mode"] == 1 else "单窗口缩放"
    application_count = len({window["bundle_name"] for window in main})
    crossed = any(rectangles_intersect(first["bounding_box"], second["bounding_box"])
                  for index, first in enumerate(main) for second in main[index + 1:])
    return ("单应用多窗口" if application_count == 1 else "多应用多窗口") + ("交叉" if crossed else "并列")


def verify_export_inputs(evidence: Path, metadata: dict, options: dict) -> dict:
    """@brief 核验画布、采集稳定性、全窗覆盖及打码。 @param evidence 证据目录。
    @param metadata 本帧元数据。 @param options 项目导出参数。 @return 画布。
    """
    if metadata.get("status") != "complete":
        fail("Cannot export an incomplete capture")
    verify_project_rules(metadata, options)
    for key in ("coverage", "consistency"):
        if metadata.get(key, {}).get("status") != "verified":
            fail(f"Cannot export without verified {key}")
    verify_privacy_receipt(metadata.get("privacy", {}))
    canvas = options.get("canvas")
    if (not isinstance(canvas, dict) or set(canvas) != {"width", "height"}
            or any(type(value) is not int or value <= 0 for value in canvas.values())):
        fail("Export requires an explicit positive canvas width and height")
    screenshot = image_info(evidence / metadata["files"]["screenshot"]["file"])
    if any(screenshot[key] != canvas[key] for key in canvas):
        fail("Screenshot does not match the project canvas")
    expected = metadata["files"]["screenshot"]
    if any(screenshot[key] != expected[key] for key in ("bytes", "sha256")):
        fail("Final screenshot digest does not match the processed metadata")
    return canvas


def accessibility_windows(evidence: Path, metadata: dict) -> list[dict]:
    """@brief 按原始窗口归属加载整屏无障碍树。 @param evidence 本地原始目录。
    @param metadata 元数据。 @return 含系统窗口的树列表。
    """
    visible = set(metadata["coverage"]["covered_window_ids"])
    return [{"window_id": window["window_id"], "bundle_name": window["bundle_name"],
             "tree": read_window_accessibility(evidence, window)} for window in metadata["windows"]
            if window["window_id"] in visible]


def normalized_arkui(evidence: Path, window: dict, canvas: dict, scenes: dict) -> dict:
    """@brief 使用官方几何证据恢复 ArkUI 坐标，拒绝未证明的坐标。 @param evidence 证据目录。
    @param window 窗口。 @param canvas 整屏画布。 @param scenes 同批完整场景来源。 @return 已验证的 ArkUI 树。
    """
    from har_capture.coordinates import normalize_arkui

    record = window["trees"]["arkui"]
    root = json.loads((evidence / record["file"]).read_bytes())
    geometry = record.get("coordinate_evidence")
    if not geometry:
        fail(f"Missing official ArkUI geometry evidence for window {window['window_id']}")
    sources = {"arkui_geometry": json.loads((evidence / geometry["arkui_geometry"]["file"]).read_bytes()),
               "scene": scenes[window["window_id"]]}
    for kind in ("render_tree", "render_detached_tree"):
        sources[kind] = (evidence / geometry[kind]["file"]).read_text(encoding="utf-8")
    tree = normalize_arkui(root, window, canvas, sources)
    if tree.get("coordinate_validation", {}).get("status") != "verified":
        write_json(evidence / f"coordinate-failure-{window['window_id']}.json", tree["coordinate_validation"])
        fail(f"ArkUI screen coordinates are not verified for window {window['window_id']}")
    return {"window_id": window["window_id"], "bundle_name": window["bundle_name"], "tree": tree}


def exported_window_records(windows: list[dict], prefix: str) -> list[dict]:
    """@brief 将应用窗口树引用指向五件套内的实际路径。 @param windows 应用窗口。
    @param prefix 项目命名。 @return 自包含的窗口记录。
    """
    result = []
    for index, window in enumerate(windows):
        record = {key: value for key, value in window.items() if key not in ("layout", "trees")}
        record["trees"] = {}
        for kind, filename in (("accessibility", "windows_tree"), ("arkui", "windows_akui")):
            record["trees"][kind] = {"file": f"{prefix}_{filename}.json", "json_pointer": f"/windows/{index}/tree",
                                     "node_count": window["trees"][kind]["node_count"]}
        result.append(record)
    return result


def delivery_payloads(evidence: Path, metadata: dict, options: dict) -> dict:
    """@brief 构造四个配对 JSON，应用树包含子窗口和对话框。 @param evidence 原始目录。
    @param metadata 采集信息。 @param options 项目字段。 @return 四个数据对象。
    """
    canvas = verify_export_inputs(evidence, metadata, options)
    visible = set(metadata["coverage"]["covered_window_ids"])
    windows = [window for window in metadata["windows"] if window["window_id"] in visible]
    if {window["window_id"] for window in windows} != visible:
        fail("Visible window coverage references missing captured windows")
    if any(type(window.get("is_system")) is not bool for window in windows):
        fail("Every exported window requires an explicit system-session classification")
    applications = [window for window in windows if not window["is_system"]]
    scenes = load_scene_evidence(evidence, windows)
    application_ids = {window["window_id"] for window in applications}
    all_accessibility = accessibility_windows(evidence, metadata)
    common = {"capture_id": metadata["capture_id"], "canvas": canvas, "coordinate_system": "screen"}
    wms = {**common, "window_mode": classify_window_mode(applications),
           "window_mode_basis": "Visible application main windows; WMS mode 1 includes the maximized work area.",
           "app_windows": exported_window_records(applications, sample_prefix(options)),
           "windows": [{key: value for key, value in window.items() if key not in ("layout", "trees")}
                       for window in metadata["window_inventory"]], "device": metadata["device"],
           "started_at": metadata["started_at"], "finished_at": metadata["finished_at"],
           "sample": {key: options[key] for key in ("app", "page_id", "scene", "state_id")},
           "consistency": metadata["consistency"], "coverage": metadata["coverage"], "stability": metadata["stability"],
           "privacy": metadata["privacy"], "checks": dict.fromkeys(
               ("coverage", "coordinates", "privacy", "consistency"), "pass")}
    return {"tree": {**common, "windows": all_accessibility},
            "windows_tree": {**common, "windows": [item for item in all_accessibility
                                                     if item["window_id"] in application_ids]},
            "windows_akui": {**common, "windows": [normalized_arkui(evidence, window, canvas, scenes)
                                                     for window in applications]}, "wms": wms}


def write_delivery(evidence: Path, metadata: dict, options: dict) -> dict:
    """@brief 写出严格五文件的待登记交付，不自行增加配额。 @param evidence 本地证据目录。
    @param metadata 已核验采集。 @param options 项目命名和画布。 @return 外部登记清单。
    """
    prefix = sample_prefix(options)
    payloads = delivery_payloads(evidence, metadata, options)
    directory = evidence / "delivery"
    directory.mkdir(exist_ok=False)
    files = {}
    for key, payload in payloads.items():
        destination = directory / f"{prefix}_{key}.json"
        write_json(destination, payload)
        files[key] = file_info(destination)
    source = evidence / metadata["files"]["screenshot"]["file"]
    destination = directory / f"{prefix}_shot.png"
    source.rename(destination)
    files["screenshot"] = file_info(destination)
    manifest = {"status": "complete", "capture_id": metadata["capture_id"], "path": str(directory),
                "files": files, "checks": payloads["wms"]["checks"], "canvas": options["canvas"]}
    metadata["files"]["screenshot"] = {**files["screenshot"], "file": "delivery/" + destination.name}
    metadata["delivery"] = manifest
    atomic_json(evidence / "metadata.json", metadata)
    atomic_json(evidence / "delivery-manifest.json", manifest)
    return manifest
