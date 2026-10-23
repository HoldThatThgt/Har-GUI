"""@brief 独立核验完整采集事实，不受外部导出文件选择影响。"""

import json
import logging
import math
from pathlib import Path

from har_capture.capture_checks import compare_capture_states, coverage_report, verify_pixel_receipt
from har_capture.capture_metadata import verify_captured_metadata
from har_capture.hdc import fail
from har_capture.metadata import file_info, image_info, load_layout, parse_windows
from har_capture.page_observation import verify_page_stability
from har_capture.privacy import verify_privacy_receipt
from har_capture.scene_evidence import load_scene_evidence
from har_capture.stability import observe_stability, verify_project_rules
from har_capture.system_accessibility import read_window_accessibility
from har_capture.arkui_selection import read_window_arkui

LOGGER = logging.getLogger(__name__)


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
            if not tree["session"]["is_system"]:
                references.append(tree["forest"])
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


def verify_capture_evidence(directory: Path, metadata: dict, skip_screenshot: bool = False) -> dict:
    """@brief 为初次准备核验原始摘要、窗口覆盖与首尾观测绑定。 @param directory 原始目录。
    @param metadata 采集元数据。 @param skip_screenshot 打码恢复前只延后图片摘要核验。
    @return 从已核验原始 WMS 解析的窗口事实。
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
    stability = observe_stability(metadata["stability"]["rules"])
    if stability != metadata["stability"] or stability != json.loads((directory / "stability.json").read_bytes()):
        fail("Capture observation declaration differs from its original evidence")
    state = compare_capture_states(before, after)
    pixels = json.loads((directory / "pixel-consistency.json").read_bytes())
    verify_pixel_receipt(pixels, metadata["display"])
    state["pixels"] = pixels
    if state != metadata["consistency"] or state != json.loads((directory / "consistency.json").read_bytes()):
        fail("Capture consistency receipt differs from its original evidence")
    return inventory


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
    crossed = any(rectangles_intersect(first["screen_bounds"], second["screen_bounds"])
                  for index, first in enumerate(main) for second in main[index + 1:])
    return ("单应用多窗口" if application_count == 1 else "多应用多窗口") + ("交叉" if crossed else "并列")


def verify_fact_inputs(evidence: Path, metadata: dict, options: dict) -> dict:
    """@brief 核验画布、采集稳定性、全窗覆盖及打码。 @param evidence 证据目录。
    @param metadata 本帧元数据。 @param options 画布及原始稳定性声明。 @return 画布。
    """
    if not isinstance(options, dict) or set(options) != {"canvas", "stability_regions"}:
        fail("Capture fact options require only canvas and stability_regions")
    if metadata.get("status") != "complete":
        fail("Cannot export an incomplete capture")
    verify_project_rules(metadata, options)
    if metadata.get("coverage", {}).get("status") != "verified":
        fail("Cannot export without verified coverage")
    if metadata.get("consistency", {}).get("status") != "observed":
        fail("Cannot export without raw capture observations")
    if verify_page_stability(evidence, metadata) != metadata.get("page_stability"):
        fail("Cannot export without the bound target page receipt")
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
    root = read_window_arkui(evidence, window)
    geometry = record.get("coordinate_evidence")
    if not geometry:
        fail(f"Missing official ArkUI geometry evidence for window {window['window_id']}")
    sources = {"arkui_geometry": json.loads((evidence / geometry["arkui_geometry"]["file"]).read_bytes()),
               "scene": scenes[window["window_id"]]}
    for kind in ("render_tree", "render_detached_tree"):
        sources[kind] = (evidence / geometry[kind]["file"]).read_text(encoding="utf-8")
    tree = normalize_arkui(root, window, canvas, sources)
    if tree.get("coordinate_validation", {}).get("status") != "verified":
        issues = tree.get("coordinate_validation", {}).get("issues", [])
        reasons = "; ".join(f"node {issue['node_id']}: {issue['code']}" for issue in issues)
        fail(f"ArkUI screen coordinates are not verified for window {window['window_id']}: {reasons}")
    return {"window_id": window["window_id"], "bundle_name": window["bundle_name"], "tree": tree}


def covered_windows(metadata: dict) -> list[dict]:
    """@brief 取得已经完整覆盖且分类明确的窗口。 @param metadata 已核验元数据。 @return 窗口列表。"""
    visible = set(metadata["coverage"]["covered_window_ids"])
    windows = [window for window in metadata["windows"] if window["window_id"] in visible]
    if {window["window_id"] for window in windows} != visible:
        fail("Visible window coverage references missing captured windows")
    if any(type(window.get("is_system")) is not bool for window in windows):
        fail("Every captured window requires an explicit system-session classification")
    return windows


def window_facts(metadata: dict, applications: list[dict], canvas: dict, inventory: dict) -> dict:
    """@brief 组装没有外部文件名的窗口事实。 @param metadata 原始元数据。
    @param applications 已覆盖普通应用窗口。 @param canvas 整屏画布。
    @param inventory 已核验原始 WMS 数值事实。 @return 窗口事实。
    """
    return {"capture_id": metadata["capture_id"], "canvas": canvas, "coordinate_system": "screen",
            "window_mode": classify_window_mode(applications),
            "window_mode_basis": "Visible application main windows; WMS mode 1 includes the maximized work area.",
            "app_windows": [{**{key: value for key, value in window.items() if key not in ("layout", "trees")},
                             **inventory[window["window_id"]]} for window in applications],
            "windows": [{**{key: value for key, value in window.items() if key not in ("layout", "trees")},
                         **inventory[window["window_id"]]} for window in metadata["window_inventory"]],
            "device": metadata["device"], "started_at": metadata["started_at"],
            "finished_at": metadata["finished_at"],
            "consistency": metadata["consistency"], "coverage": metadata["coverage"],
            "stability": metadata["stability"],
            "page_stability": metadata["page_stability"], "privacy": metadata["privacy"],
            "checks": dict.fromkeys(("coverage", "coordinates", "privacy", "consistency"), "pass")}


def build_capture_facts(evidence: Path, metadata: dict, options: dict) -> dict:
    """@brief 先核验完整原始证据及全部应用双树，再生成独立采集事实。 @param evidence 原始证据目录。
    @param metadata 该帧真实元数据。 @param options 画布及原始稳定性声明。 @return 完整采集事实。
    """
    try:
        evidence = Path(evidence).resolve()
        metadata_path = evidence / "metadata.json"
        if metadata_path.is_symlink() or not metadata_path.is_file():
            fail("Capture metadata is missing or is a symbolic link")
        if json.loads(metadata_path.read_bytes()) != metadata:
            fail("Capture facts must use the actual saved metadata")
        canvas = verify_fact_inputs(evidence, metadata, options)
        verify_captured_metadata(evidence, metadata)
        inventory = verify_capture_evidence(evidence, metadata)
        windows = covered_windows(metadata)
        applications = [window for window in windows if not window["is_system"]]
        scenes = load_scene_evidence(evidence, windows)
        common = {"capture_id": metadata["capture_id"], "canvas": canvas, "coordinate_system": "screen"}
        screenshot = (evidence / metadata["files"]["screenshot"]["file"]).resolve()
        return {"schema_version": 1, "capture_id": metadata["capture_id"], "canvas": canvas,
                "evidence_directory": str(evidence), "metadata": metadata,
                "screenshot": {**image_info(screenshot), "file": str(screenshot)},
                "windows": window_facts(metadata, applications, canvas, inventory),
                "accessibility": {**common, "windows": accessibility_windows(evidence, metadata)},
                "arkui": {**common, "windows": [normalized_arkui(evidence, window, canvas, scenes)
                                                for window in applications]}}
    except BaseException:
        LOGGER.exception("Capture fact verification failed")
        raise
