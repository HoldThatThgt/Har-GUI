"""@brief 从当前完整 SceneBoard 和真实渲染父链核验窗口屏幕边界。"""

import re
import tempfile
import uuid
from pathlib import Path

from har_capture.coordinates import (client_render, detached_render_index, native_rect, render_ancestors,
                                     render_bounds, render_index, scene_display, scene_records,
                                     scene_window_identity, verify_scene_ancestors, verify_window_envelope)
from har_capture.geometry_capture import collect_render_evidence, collect_window_geometry
from har_capture.hdc import Hdc, cleanup_remote, fail
from har_capture.scene_evidence import artifact_bytes, load_scene_evidence, load_source, select_window_scene
from har_capture.tree_capture import collect_system_arkui

def screen_canvas(ancestors: list[dict]) -> dict:
    """@brief 从真实屏幕渲染祖先取得未缩放的整数画布。 @param ancestors 当前会话祖先链。
    @return 屏幕宽高。
    """
    screens = [node for node in ancestors if node["kind"] == "SCREEN_NODE"]
    if len(screens) != 1:
        raise ValueError("scene_screen_missing_or_ambiguous")
    x, y, width, height = render_bounds(screens[0])
    if x != 0 or y != 0 or min(width, height) <= 0 or not all(value.is_integer() for value in (width, height)):
        raise ValueError("invalid_scene_screen_canvas")
    return {"width": int(width), "height": int(height)}


def screen_bounds_source(scene: dict, identifiers: tuple) -> dict:
    """@brief 保留可持久化的原始来源摘要和节点身份，不引用临时路径。 @param scene 完整场景来源。
    @param identifiers 会话、渲染、显示器和屏幕身份。 @return 精确边界的来源记录。
    """
    hashes = {key: scene["source"][name]["sha256"] for key, name in (
        ("raw_sha256", "raw"), ("native_sha256", "arkui_geometry"))}
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in hashes.values()):
        raise ValueError("invalid_scene_source_digest")
    return {"method": "SceneBoard.ArkUI.PaintRectAncestorComposition", **dict(zip(
        ("session_node_id", "session_render_node_id", "display_render_node_id", "screen_render_node_id"),
        identifiers, strict=True)), **hashes}


def window_screen_geometry(window: dict, scene: dict, render: dict, detached: dict) -> dict:
    """@brief 只用真实场景浮点父链核验窗口外框，不使用 WMS 近似比例投影。 @param window 当前 WMS 窗口。
    @param scene 已核验原始场景。 @param render 已挂载渲染索引。 @param detached 未挂载索引。
    @return 屏幕外框和原始来源。
    """
    try:
        if not isinstance(scene, dict) or not scene.get("path"):
            raise ValueError("missing_scene_coordinate_evidence")
        node = scene_window_identity(scene["path"][-1], window, scene)
        # 外框只由祖先及会话自身决定，后代绘制不参与这层坐标映射。
        path = [*scene["path"][:-1], {**node, "children": []}]
        records = scene_records({**scene, "path": path})
        session_id = client_render(node)["id"]
        ancestors = render_ancestors(session_id, render)
        if render[session_id]["kind"] != "SURFACE_NODE":
            raise ValueError("scene_render_window_mismatch")
        if re.findall(r"nodeName: \[([^\]]*)\]", render[session_id]["line"]) != [f"session{window['window_id']}"]:
            raise ValueError("scene_render_session_identity_mismatch")
        if any(item["id"] in detached for item in ancestors if item["kind"] != "RS_NODE"):
            raise ValueError("scene_render_attachment_state_changed")
        display_id, screen_id = scene_display(records, ancestors, window, screen_canvas(ancestors))
        verify_scene_ancestors(records, ancestors, render, detached)
        node_id = str(node["attributes"]["ID"])
        verify_window_envelope(window, node, native_rect(records["native"][node_id]["$rect"]))
        bounds = records["rectangles"][session_id]
        if min(bounds["width"], bounds["height"]) <= 0:
            raise ValueError("empty_scene_window_bounds")
        source = screen_bounds_source(scene, (node_id, session_id, display_id, screen_id))
        return {"screen_bounds": dict(bounds), "screen_bounds_source": source}
    except (KeyError, TypeError, ValueError) as error:
        fail(f"Cannot verify exact window screen geometry: {error}")


def read_render_indexes(directory: Path, records: dict) -> tuple[dict, dict]:
    """@brief 核验渲染原始摘要并构建同批两个索引。 @param directory 证据目录。
    @param records 两份渲染文件记录。 @return 已挂载和未挂载渲染索引。
    """
    cache = {}
    try:
        active = artifact_bytes(directory, records["render_tree"], cache).decode("utf-8")
        detached = artifact_bytes(directory, records["render_detached_tree"], cache).decode("utf-8")
        return render_index(active), detached_render_index(detached)
    except (KeyError, TypeError, ValueError) as error:
        fail(f"Cannot read exact window render evidence: {error}")


def add_render_source(geometry: dict, records: dict) -> dict:
    """@brief 将同批已核验的渲染原始摘要加入可持久化来源。 @param geometry 精确窗口几何。
    @param records 同批渲染文件记录。 @return 补齐来源的几何，不含临时路径。
    """
    geometry["screen_bounds_source"].update({kind + "_sha256": records[kind]["sha256"]
                                             for kind in ("render_tree", "render_detached_tree")})
    return geometry


def collect_scene_forests(hdc: Hdc, stage: Path, remote: dict, windows: list[dict]) -> list[dict]:
    """@brief 每个系统进程和显示器仅采集一份完整原文与原生几何。 @param hdc 设备连接。
    @param stage 临时目录。 @param remote 精确远程路径登记。 @param windows 当前窗口。 @return 已核验森林。
    """
    sources, cache = {}, {}
    for window in windows:
        if type(window["is_system"]) is not bool:
            fail("Window geometry requires explicit system-session classification")
        if not window["is_system"]:
            continue
        key = (window["process_id"], window["display_id"])
        if key in sources:
            continue
        raw = collect_system_arkui(hdc, stage, remote, window, window)
        native = collect_window_geometry(hdc, stage, remote, window)
        source = {"source_window_id": window["window_id"], "process_id": window["process_id"],
                  "display_id": window["display_id"], "raw": raw["response"],
                  "arkui_geometry": native["arkui_geometry"]}
        sources[key] = load_source(stage, source, cache)
    if windows and not sources:
        fail("Current windows have no verified SceneBoard geometry source")
    return list(sources.values())


def read_window_geometry(hdc: Hdc, windows: list[dict]) -> list[dict]:
    """@brief 为当前窗口批次增加严格核验的屏幕外框，并清理所有本次临时文件。 @param hdc 设备连接。
    @param windows 已绑定 WMS 身份的窗口。 @return 保留 WMS 原字段的窗口列表。
    """
    if not windows:
        return []
    if len({window["window_id"] for window in windows}) != len(windows):
        fail("Window geometry requires distinct window identities")
    remote = {"layout": "/data/local/tmp/har-window-geometry-" + uuid.uuid4().hex}
    with tempfile.TemporaryDirectory(prefix="har-window-geometry-") as temporary:
        stage, successful = Path(temporary).resolve(), False
        (stage / "arkui").mkdir()
        try:
            forests = collect_scene_forests(hdc, stage, remote, windows)
            records = collect_render_evidence(hdc, stage, remote)
            render, detached = read_render_indexes(stage, records)
            result = []
            for window in windows:
                candidates = [forest for forest in forests if forest["source"]["display_id"] == window["display_id"]]
                scene = select_window_scene(candidates, window)
                geometry = add_render_source(window_screen_geometry(window, scene, render, detached), records)
                result.append({**window, **geometry})
            successful = True
            return result
        except (KeyError, OSError, TypeError, ValueError) as error:
            fail(f"Cannot collect exact window screen geometry: {error}")
        finally:
            cleanup = cleanup_remote(hdc, remote)
            if not cleanup["success"] and successful:
                fail("Cannot return exact window geometry after remote cleanup failure")


def captured_render_records(metadata: dict) -> dict:
    """@brief 只接受所有窗口共同引用且已列入采集索引的渲染文件。 @param metadata 原始采集信息。
    @return 唯一同批渲染文件记录。
    """
    indexed = {record["file"]: record for record in metadata["files"].values()}
    records = {}
    for window in metadata["windows"]:
        evidence = window["trees"]["arkui"]["coordinate_evidence"]
        for kind in ("render_tree", "render_detached_tree"):
            record = evidence[kind]
            if indexed.get(record["file"]) != record:
                fail("Window render evidence differs from the capture file index")
            if kind in records and record != records[kind]:
                fail("Captured windows do not share the same render evidence")
            records[kind] = record
    return records


def attach_captured_screen_geometry(directory: Path, metadata: dict) -> None:
    """@brief 用同批已保存来源为采集窗口附加精确外框，不改写 WMS 原字段。 @param directory 原始证据目录。
    @param metadata 本次采集信息。
    """
    if not metadata["windows"]:
        return
    try:
        scenes = load_scene_evidence(directory, metadata["windows"])
        records = captured_render_records(metadata)
        render, detached = read_render_indexes(directory.resolve(), records)
        additions = [add_render_source(window_screen_geometry(window, scenes[window["window_id"]], render, detached),
                                       records)
                     for window in metadata["windows"]]
        for window, geometry in zip(metadata["windows"], additions, strict=True):
            window.update(geometry)
    except (KeyError, OSError, TypeError, ValueError) as error:
        fail(f"Cannot attach exact captured window screen geometry: {error}")
