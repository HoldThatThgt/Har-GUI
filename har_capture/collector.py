"""@brief 成对采集原始证据，校验后发布独立的采集目录。"""

import json
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from har_capture.hdc import CaptureError, Hdc, fail, utc_now
from har_capture.capture_checks import (compare_capture_states, compare_screen_pixels, coverage_report,
                                        session_visibility)
from har_capture.metadata import associate_windows, file_info, image_info, load_layout, parse_windows
from har_capture.tree_capture import collect_trees
from har_capture.geometry_capture import collect_geometry_evidence
from har_capture.system_accessibility import supplement_missing_windows
from har_capture.stability import resolve_stability, validate_rules

LOGGER = logging.getLogger(__name__)


def write_json(path: Path, data: object) -> None:
    """@brief 写入派生元数据，不用于原始控件树。 @param path 输出路径。 @param data JSON 数据。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def capture_remote(hdc: Hdc, remote: dict, display_id: int) -> str:
    """@brief 在唯一正式截图前读取窗口与页面状态。 @param hdc 连接。
    @param remote 设备文件路径。 @param display_id 显示器 ID。 @return 原始窗口列表。
    """
    display = str(display_id)
    windows = hdc.run(["shell", "hidumper", "-s", "WindowManagerService", "-a", "-a"], "windows")
    tree = hdc.run(["shell", "uitest", "dumpLayout", "-d", display, "-m", "false", "-p", remote["layout"]],
                   "layout")
    if f"DumpLayout saved to:{remote['layout']}" not in tree:
        fail("UITest did not confirm the layout dump")
    screenshot = hdc.run(["shell", "uitest", "screenCap", "-d", display, "-p", remote["screenshot"]], "screenshot")
    if f"ScreenCap saved to {remote['screenshot']}" not in screenshot:
        fail("UITest did not confirm the screenshot")
    return windows


def closing_observation(hdc: Hdc, stage: Path, remote: dict, options: dict) -> dict:
    """@brief 在采集末尾读取状态，末帧图片仅暂存并立即删除。 @param hdc 连接。
    @param stage 本地证据。 @param remote 临时路径。 @param options 画布与动态区域声明。 @return 一致性证据。
    """
    windows = hdc.run(["shell", "hidumper", "-s", "WindowManagerService", "-a", "-a"], "closing_windows")
    (stage / "closing-windows.txt").write_text(windows, encoding="utf-8")
    remote["closing_layout"] = remote["layout"] + "-closing.json"
    remote["closing_screen"] = remote["screenshot"] + "-closing.png"
    for kind, command, confirmation in (("layout", "dumpLayout", "DumpLayout saved to:"),
                                         ("screen", "screenCap", "ScreenCap saved to ")):
        arguments = ["shell", "uitest", command, "-d", str(options["display"]["id"])]
        if kind == "layout":
            arguments.extend(["-m", "false"])
        destination = remote["closing_" + kind]
        result = hdc.run([*arguments, "-p", destination], "closing_" + kind)
        if confirmation + destination not in result:
            fail(f"UITest did not confirm the closing {kind} observation")
    hdc.receive(remote["closing_layout"], stage / "closing-layout.json", "receive_closing_layout")
    before = {"windows": parse_windows((stage / "windows.txt").read_text(encoding="utf-8")),
              "layout": load_layout(stage / "layout.json")}
    after = {"windows": parse_windows(windows), "layout": load_layout(stage / "closing-layout.json")}
    proof = {"display": options["display"],
             "sessions": session_visibility((stage / "session-details.txt").read_text(encoding="utf-8"))}
    stability = resolve_stability({"before": before, "after": after}, proof, options["stability_regions"])
    write_json(stage / "stability.json", stability)
    with tempfile.TemporaryDirectory(prefix="har-capture-check-") as temporary:
        path = Path(temporary) / "observation.png"
        hdc.receive(remote["closing_screen"], path, "receive_closing_screen")
        pixels = compare_screen_pixels((stage / "screenshot.png").read_bytes(), path.read_bytes(), stability["regions"])
    write_json(stage / "pixel-consistency.json", pixels)
    state = compare_capture_states(before, after, stability)
    state["pixels"] = pixels
    write_json(stage / "consistency.json", state)
    if pixels["status"] != "verified":
        fail("Screen content changed during collection")
    return state


def collect_files(hdc: Hdc, stage: Path, remote: dict, display_id: int) -> dict:
    """@brief 保存当前采集的原始文件。 @param hdc 连接。 @param stage 本次临时目录。
    @param remote 设备路径。 @param display_id 显示器 ID。 @return 每个窗口的组件树与无障碍树记录。
    """
    windows = capture_remote(hdc, remote, display_id)
    (stage / "windows.txt").write_bytes(windows.encode("utf-8"))
    names = {"screenshot": "screenshot.png", "layout": "layout.json"}
    for name, filename in names.items():
        hdc.receive(remote[name], stage / filename, "receive_" + name)
    inventory = parse_windows(windows)
    visible = associate_windows(load_layout(stage / "layout.json"), inventory, display_id)
    return collect_trees(hdc, stage, remote, visible, display_id)


def inspect_files(stage: Path, display_id: int) -> dict:
    """@brief 验证文件并生成窗口关联信息。 @param stage 采集目录。
    @param display_id 显示器 ID。 @return 文件摘要、窗口与覆盖信息。
    """
    layout = load_layout(stage / "layout.json")
    inventory = parse_windows((stage / "windows.txt").read_text(encoding="utf-8"))
    windows = associate_windows(layout, inventory, display_id)
    screenshot = image_info(stage / "screenshot.png")
    captured_ids = {window["window_id"] for window in windows}
    missing = [window_id for window_id, window in inventory.items()
               if window["display_id"] == display_id and window["z_order"] >= 0 and window_id not in captured_ids]
    return {
        "display": {"id": display_id, "width": screenshot["width"], "height": screenshot["height"]},
        "files": {"screenshot": screenshot, "layout": file_info(stage / "layout.json"),
                  "windows": file_info(stage / "windows.txt")},
        "windows": windows,
        "applications": group_applications(windows),
        "foreground_window_ids_without_tree": sorted(missing),
        "consistency": {"atomic": False},
    }


def group_applications(windows: list[dict]) -> list[dict]:
    """@brief 汇总应用所属窗口，不合并窗口边界。 @param windows 关联窗口。 @return 应用列表。"""
    applications = {}
    for window in windows:
        if window.get("is_system") is True:
            continue
        applications.setdefault(window["bundle_name"], []).append(window["window_id"])
    return [{"bundle_name": bundle, "window_ids": ids} for bundle, ids in sorted(applications.items())]


def cleanup_remote(hdc: Hdc, remote: dict) -> dict:
    """@brief 清理本次生成的设备临时文件。 @param hdc 连接。 @param remote 精确路径。 @return 清理结果。"""
    try:
        output = hdc.run(["shell", "rm", "-f", *remote.values(), "&&", "echo", "HAR_CAPTURE_CLEANUP_OK"], "cleanup")
        if "HAR_CAPTURE_CLEANUP_OK" not in output.splitlines():
            fail("Device did not confirm temporary file cleanup")
    except (CaptureError, KeyboardInterrupt) as error:
        LOGGER.error("Artifacts are valid, but device cleanup failed: %s", error)
        return {"success": False, "error": str(error), "remote_files": remote}
    return {"success": True}


def attach_uitest_trees(metadata: dict, trees: dict) -> None:
    """@brief 附加 UITest 已返回的窗口双树，其余窗口保持待原生通道补齐。 @param metadata 本次元数据。
    @param trees UITest 已确认归属的双树。
    """
    metadata["windows"] = [window for window in metadata["windows"] if window["window_id"] in trees]
    for window in metadata["windows"]:
        window["trees"] = trees[window["window_id"]]
        arkui = window["trees"]["arkui"]
        window["ui_node_id"] = arkui["session"]["ui_node_id"]
        metadata["files"][f"arkui_{window['window_id']}"] = {key: arkui[key] for key in ("file", "bytes", "sha256")}
        metadata["files"][f"arkui_raw_{window['window_id']}"] = arkui["raw"]
        metadata["files"][f"arkui_response_{window['window_id']}"] = arkui["response"]
        window["is_system"] = arkui["session"]["is_system"]


def complete_capture(hdc: Hdc, stage: Path, remote: dict, display_id: int, stability_regions: list) -> dict:
    """@brief 执行一次完整采集及验证。 @param hdc 连接。 @param stage 临时目录。
    @param remote 设备路径。 @param display_id 显示器 ID。 @param stability_regions 项目声明。 @return 元数据。
    """
    hdc.connect()
    device = hdc.device_info()
    trees = collect_files(hdc, stage, remote, display_id)
    metadata = inspect_files(stage, display_id)
    layout_windows = metadata["windows"]
    metadata["files"]["accessibility"] = file_info(stage / "accessibility.json")
    metadata["files"]["session_details"] = file_info(stage / "session-details.txt")
    attach_uitest_trees(metadata, trees)
    inventory = parse_windows((stage / "windows.txt").read_text(encoding="utf-8"))
    sessions = (stage / "session-details.txt").read_text(encoding="utf-8")
    metadata["window_inventory"] = enrich_inventory(inventory, layout_windows, sessions)
    write_json(stage / "coverage.json", coverage_report(metadata, inventory, sessions))
    supplement_missing_windows(hdc, stage, remote, metadata)
    metadata["coverage"] = coverage_report(metadata, inventory, sessions)
    metadata["applications"] = group_applications(metadata["windows"])
    captured = {window["window_id"] for window in metadata["windows"]}
    metadata["foreground_window_ids_without_tree"] = sorted(window_id for window_id, window in inventory.items()
        if window["display_id"] == display_id and window["z_order"] >= 0 and window_id not in captured)
    write_json(stage / "coverage.json", metadata["coverage"])
    write_json(stage / "capture-evidence.json", metadata)
    geometry = collect_geometry_evidence(hdc, stage, remote, metadata["windows"])
    for window in metadata["windows"]:
        window["trees"]["arkui"]["coordinate_evidence"] = geometry[window["window_id"]]
        for kind in ("arkui_geometry", "response", "render_tree", "render_detached_tree"):
            metadata["files"][f"geometry_{window['window_id']}_{kind}"] = geometry[window["window_id"]][kind]
    metadata["consistency"] = closing_observation(hdc, stage, remote, {
        "display": metadata["display"], "stability_regions": stability_regions})
    metadata["stability"] = json.loads((stage / "stability.json").read_bytes())
    for kind, filename in (("coverage", "coverage.json"), ("consistency", "consistency.json"),
                           ("closing_layout", "closing-layout.json"), ("closing_windows", "closing-windows.txt"),
                           ("pixel_consistency", "pixel-consistency.json"), ("stability", "stability.json")):
        metadata["files"][kind] = file_info(stage / filename)
    write_json(stage / "capture-evidence.json", metadata)
    if metadata["coverage"]["status"] != "verified":
        ids = [item["window_id"] for item in metadata["coverage"]["unresolved_windows"]]
        fail(f"Visible windows are missing verified dual trees: {ids}")
    metadata.update(device=device, device_cleanup=cleanup_remote(hdc, remote))
    return metadata


def enrich_inventory(inventory: dict, windows: list[dict], text: str) -> list[dict]:
    """@brief 保留全部窗口行与会话可见性，不丢失后台或无树窗口。 @param inventory WMS 表。
    @param windows 已关联窗口。 @param text 原始会话。 @return 完整窗口列表。
    """
    sessions = session_visibility(text)
    captured = {window["window_id"]: window for window in windows}
    return [{**window, "session": sessions.get(window_id),
             "bundle_name": captured.get(window_id, {}).get("bundle_name"),
             "is_system": sessions.get(window_id, {}).get("is_system")}
            for window_id, window in inventory.items()]


def save_failure(hdc: Hdc, stage: Path, error: BaseException, remote: dict) -> Path:
    """@brief 保存失败证据，禁止发布成功元数据。 @param hdc 连接。 @param stage 临时目录。
    @param error 原始异常。 @param remote 设备路径。 @return 失败目录。
    """
    LOGGER.error("Capture failed: %s", error)
    cleanup = cleanup_remote(hdc, remote)
    write_json(stage / "commands.json", hdc.records)
    write_json(stage / "error.json", {"status": "failed", "failed_at": utc_now(),
                                      "error": str(error), "remote_files": remote, "device_cleanup": cleanup})
    if (stage / "metadata.json").exists():
        (stage / "metadata.json").unlink()
    failed = stage.with_name(stage.name.removesuffix(".partial") + ".failed")
    stage.rename(failed)
    LOGGER.error("Incomplete artifacts were saved to %s", failed)
    return failed


def capture(hdc: Hdc, output: Path, display_id: int = 0, stability_regions: list | None = None) -> Path:
    """@brief 采集并发布独立目录，绝不覆盖旧记录。 @param hdc 连接。 @param output 输出根目录。
    @param display_id 显示器 ID。 @param stability_regions 显式动态资源选择器。 @return 成功目录路径。
    """
    hdc.records.clear()
    stability_regions = validate_rules([] if stability_regions is None else stability_regions)
    capture_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid.uuid4().hex[:8]
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stage = output / (capture_id + ".partial")
    stage.mkdir()
    started = utc_now()
    prefix = "/data/local/tmp/har-capture-" + capture_id
    remote = {"screenshot": prefix + ".png", "layout": prefix + ".json"}
    try:
        metadata = complete_capture(hdc, stage, remote, display_id, stability_regions)
        metadata.update(capture_id=capture_id, status="complete", started_at=started, finished_at=utc_now(),
                        clock="host_utc", z_order_source="WindowManagerService.ZOrd",
                        z_order_direction="higher_is_front_on_same_display",
                        bounding_box_source="WindowManagerService.GetSessionRect", commands_file="commands.json")
        metadata["capture_times"] = {
            record["name"]: {key: record[key] for key in ("started_at", "finished_at", "duration_seconds")}
            for record in hdc.records
            if record["name"] in ("screenshot", "layout", "windows", "accessibility", "session_details")
            or record["name"].startswith("dump_arkui_")
        }
        write_json(stage / "commands.json", hdc.records)
        write_json(stage / "metadata.json", metadata)
        final = output / capture_id
        stage.rename(final)
        return final
    except (Exception, KeyboardInterrupt) as error:
        save_failure(hdc, stage, error, remote)
        raise
