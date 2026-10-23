"""@brief 按项目声明导出已核验采集事实，不移动或改写原始证据。"""

import copy
import hashlib
import json
import logging
import os
import posixpath
import re
import shutil
import string
from pathlib import Path, PurePosixPath

from har_capture.hdc import fail
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json
from har_capture.signatures import digest

LOGGER = logging.getLogger(__name__)
FIELDS = {"app", "scene", "task", "capture_id", "page_id", "state_id"}
SOURCES = {"screenshot", "accessibility", "application_accessibility", "application_arkui", "windows",
           "capture_metadata"}


def render_template(template: str, naming: dict) -> str:
    """@brief 只替换固定命名字段，拒绝表达式及格式运算。 @param template 路径模板。
    @param naming 已登记名称。 @return 替换后的路径文本。
    """
    if not isinstance(template, str) or not template:
        fail("Export path template must be a nonempty string")
    formatter = string.Formatter()
    for _, field, specification, conversion in formatter.parse(template):
        if field is None:
            continue
        if field not in FIELDS or specification or conversion:
            fail("Export templates accept only the declared naming placeholders")
        value = naming.get(field)
        if (not isinstance(value, str) or not value or value != value.strip(" .")
                or re.search(r'[\\/:*?"<>|\x00-\x1f]', value)):
            fail(f"Invalid export naming field: {field}")
    return formatter.vformat(template, (), naming)


def relative_path(template: str, naming: dict, directory: bool = False) -> str:
    """@brief 验证可移植的显式相对路径，不对越界路径做规范化修复。 @param template 路径模板。
    @param naming 已登记名称。 @param directory 是否允许根目录点号。 @return 安全相对路径。
    """
    value = render_template(template, naming)
    if directory and value == ".":
        return value
    parts = value.split("/")
    if (re.search(r'[{}\\:*?"<>|\x00-\x1f]', value) or PurePosixPath(value).is_absolute()
            or any(not part or part in (".", "..") or part != part.strip(" .")
                   or re.match(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", part, re.I) for part in parts)):
        fail("Export paths must be safe relative paths without empty or parent components")
    return value


def declared_paths(declaration: dict, naming: dict) -> dict:
    """@brief 在写出前验证全部来源声明和路径冲突。 @param declaration 项目交付声明。
    @param naming 已登记名称。 @return 来源到相对输出路径的映射。
    """
    if not isinstance(declaration, dict) or set(declaration) != {"directory", "artifacts"}:
        fail("Delivery declaration requires directory and artifacts")
    artifacts = declaration["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        fail("Delivery artifacts must be a nonempty array")
    directory = relative_path(declaration["directory"], naming, True)
    paths = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"source", "path"}:
            fail("Each export artifact requires only source and path")
        source = artifact["source"]
        if not isinstance(source, str) or (source not in SOURCES and not source.startswith("raw:")):
            fail("Export artifact source is not supported")
        if source in paths:
            fail("An export source may be declared only once")
        path = (PurePosixPath(directory) / relative_path(artifact["path"], naming)).as_posix()
        if path.casefold() in {"manifest.json", "manifest.json.pending"}:
            fail("Export artifacts cannot replace the run manifest")
        paths[source] = path
    folded = [PurePosixPath(value.casefold()) for value in paths.values()]
    if len(set(folded)) != len(folded) or any(
            first in second.parents for first in folded for second in folded if first != second):
        fail("Export artifact paths overlap or identify the same file")
    return paths


def source_file(facts: dict, source: str) -> Path:
    """@brief 只读取事实明确记录且摘要吻合的普通来源文件。 @param facts 采集事实。
    @param source 截图或原始索引来源。 @return 实际来源路径。
    """
    if source == "screenshot":
        record = facts["screenshot"]
        path = Path(record["file"])
    else:
        key = source.removeprefix("raw:")
        if not key or key not in facts["metadata"]["files"]:
            fail(f"Export raw source is not indexed: {key}")
        record = facts["metadata"]["files"][key]
        path = Path(facts["evidence_directory"]) / record["file"]
        if key != "screenshot" and not path.resolve().is_relative_to(Path(facts["evidence_directory"]).resolve()):
            fail("Export raw source escapes its evidence directory")
    if path.is_symlink() or not path.is_file():
        fail(f"Export source is missing or is a symbolic link: {source}")
    actual = file_info(path)
    if any(actual[key] != record[key] for key in ("bytes", "sha256")):
        fail(f"Export source digest differs from its recorded facts: {source}")
    return path


def application_accessibility(facts: dict) -> dict:
    """@brief 从完整无障碍事实选择普通应用窗口。 @param facts 完整采集事实。 @return 应用无障碍文档。"""
    ids = {window["window_id"] for window in facts["windows"]["app_windows"]}
    return {**facts["accessibility"], "windows": [window for window in facts["accessibility"]["windows"]
                                               if window["window_id"] in ids]}


def windows_payload(facts: dict, paths: dict, naming: dict) -> dict:
    """@brief 只为本次实际选中的应用树生成相对文件引用。 @param facts 已核验事实。
    @param paths 全部实际输出路径。 @param naming 已登记名称。 @return 窗口交付文档。
    """
    windows = copy.deepcopy(facts["windows"])
    windows["sample"] = {key: naming[key] for key in ("app", "page_id", "scene", "state_id")}
    original = {window["window_id"]: window for window in facts["metadata"]["windows"]}
    sources = (("accessibility", "application_accessibility", application_accessibility(facts)),
               ("arkui", "application_arkui", facts["arkui"]))
    for kind, source, document in sources:
        if source not in paths:
            continue
        indexes = {window["window_id"]: index for index, window in enumerate(document["windows"])}
        filename = posixpath.relpath(paths[source], posixpath.dirname(paths["windows"]) or ".")
        for window in windows["app_windows"]:
            window_id = window["window_id"]
            window.setdefault("trees", {})[kind] = {"file": filename,
                "json_pointer": f"/windows/{indexes[window_id]}/tree",
                "node_count": original[window_id]["trees"][kind]["node_count"]}
    return windows


def artifact_content(facts: dict, source: str, paths: dict, naming: dict) -> bytes | Path:
    """@brief 将已存在来源解析为文件或确定的 JSON 字节。 @param facts 已核验事实。
    @param source 声明来源。 @param paths 实际路径。 @param naming 已登记名称。 @return 来源内容。
    """
    if source == "screenshot" or source.startswith("raw:"):
        return source_file(facts, source)
    payloads = {"accessibility": facts["accessibility"], "application_arkui": facts["arkui"],
                "capture_metadata": facts["metadata"]}
    if source == "application_accessibility":
        payload = application_accessibility(facts)
    elif source == "windows":
        payload = windows_payload(facts, paths, naming)
    else:
        payload = payloads[source]
    return (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def export_plan(facts: dict, declaration: dict, naming: dict) -> tuple[dict, dict]:
    """@brief 解析全部来源后生成不含临时输出绝对路径的持久计划。 @param facts 已核验事实。
    @param declaration 项目声明。 @param naming 已登记名称。 @return 收据计划及内存来源。
    """
    if facts.get("schema_version") != 1 or naming.get("capture_id") != facts["capture_id"]:
        fail("Export naming and facts must identify the same supported capture")
    paths = declared_paths(declaration, naming)
    contents = {source: artifact_content(facts, source, paths, naming) for source in paths}
    files = {}
    for source, content in contents.items():
        info = (file_info(content) if isinstance(content, Path)
                else {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        files[source] = {"file": paths[source], "bytes": info["bytes"], "sha256": info["sha256"]}
    return {"rule_version": 1, "capture_id": facts["capture_id"], "source_sha256": digest(facts),
            "declaration": copy.deepcopy(declaration), "naming": copy.deepcopy(naming),
            "files": files, "status": "started"}, contents


def controlled_path(destination: Path, relative: str) -> Path:
    """@brief 拒绝输出路径中的符号链接及目录逃逸。 @param destination 结果目录。
    @param relative 已核验相对路径。 @return 实际输出位置。
    """
    path = destination / relative
    if not path.resolve().is_relative_to(destination):
        fail("Export output escapes its destination")
    for part in (path, *path.parents):
        if part == destination:
            break
        if part.is_symlink():
            fail("Export output paths cannot contain symbolic links")
    return path


def verify_output(path: Path, record: dict) -> None:
    """@brief 已有文件只核验字节，不覆盖冲突内容。 @param path 实际路径。 @param record 持久文件计划。"""
    if not path.is_file() or path.is_symlink():
        fail("Existing export output is not an ordinary file")
    actual = file_info(path)
    if any(actual[key] != record[key] for key in ("bytes", "sha256")):
        fail(f"Existing export output conflicts with its saved plan: {record['file']}")


def sample_record(plan: dict) -> dict:
    """@brief 提取运行清单保存的实际样本计划。 @param plan 内部导出计划。 @return 样本清单项。"""
    return {key: plan[key] for key in ("capture_id", "source_sha256", "naming", "files")}


def read_run_manifest(destination: Path, plan: dict) -> dict:
    """@brief 核验运行清单并拒绝不同样本之间的路径冲突。 @param destination 结果目录。
    @param plan 本次文件计划。 @return 当前运行清单。
    """
    path = destination / "manifest.json"
    if path.is_symlink() or not path.is_file():
        fail("Export requires the existing ordinary run manifest")
    manifest = json.loads(path.read_bytes())
    if manifest.get("status") != "running" or not isinstance(manifest.get("samples"), list):
        fail("Export run manifest must be running and contain a samples array")
    current = {PurePosixPath(record["file"].casefold()) for record in plan["files"].values()}
    matches = 0
    for sample in manifest["samples"]:
        if sample["capture_id"] == plan["capture_id"]:
            matches += 1
            if matches != 1 or sample != sample_record(plan):
                fail("Run manifest already contains a different export for this capture")
            continue
        previous = {PurePosixPath(record["file"].casefold()) for record in sample["files"].values()}
        if any(first == second or first in second.parents or second in first.parents
               for first in current for second in previous):
            fail("Export artifact paths conflict with an already exported sample")
    return manifest


def pending_path(destination: Path, relative: str) -> Path:
    """@brief 用最终相对路径摘要定位专用临时文件，避免与声明文件重名。 @param destination 结果目录。
    @param relative 最终相对路径。 @return 本计划临时文件。
    """
    name = hashlib.sha256(relative.encode("utf-8")).hexdigest()
    return controlled_path(destination, ".pending/" + name)


def planned_manifest(manifest: dict, plan: dict) -> dict:
    """@brief 从当前运行清单构造本帧唯一的完整追加结果。 @param manifest 当前清单。
    @param plan 已持久计划。 @return 追加后清单。
    """
    result = copy.deepcopy(manifest)
    if not any(item["capture_id"] == plan["capture_id"] for item in result["samples"]):
        result["samples"].append(sample_record(plan))
    return result


def verify_pending(destination: Path, manifest: dict, plan: dict, registered: bool) -> set[str]:
    """@brief 临时残留必须属于已核验持久计划，清单残留必须是准确写入前缀。 @param destination 结果目录。
    @param manifest 当前清单。 @param plan 本帧计划。 @param registered 是否已有一致收据。 @return 临时文件集合。
    """
    expected = {pending_path(destination, record["file"]).relative_to(destination).as_posix()
                for record in plan["files"].values()}
    directory = controlled_path(destination, ".pending")
    actual = set()
    if directory.exists():
        if not directory.is_dir() or not registered:
            fail("Export temporary artifacts require their matching durable receipt")
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                fail("Export temporary artifacts must be ordinary plan files")
            actual.add(path.relative_to(destination).as_posix())
        if actual - expected:
            fail("Export destination contains unknown temporary artifacts")
    pending = controlled_path(destination, "manifest.json.pending")
    if pending.exists():
        if not registered or not pending.is_file():
            fail("Interrupted run manifest requires its matching durable receipt")
        data = (json.dumps(planned_manifest(manifest, plan), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        if not data.startswith(pending.read_bytes()):
            fail("Interrupted run manifest differs from its durable export plan")
        actual.add("manifest.json.pending")
    return actual


def verify_output_inventory(destination: Path, manifest: dict, plan: dict, registered: bool) -> None:
    """@brief 文件集合必须属于已完成样本或本次完整声明。 @param destination 结果目录。
    @param manifest 当前运行清单。 @param plan 本次交付计划。 @param registered 是否存在一致持久收据。
    """
    expected = {record["file"] for record in plan["files"].values()} | {"manifest.json"}
    expected.update(verify_pending(destination, manifest, plan, registered))
    for sample in manifest["samples"]:
        for record in sample["files"].values():
            expected.add(record["file"])
            verify_output(controlled_path(destination, record["file"]), record)
    actual = set()
    for path in destination.rglob("*"):
        if path.is_symlink():
            fail("Export destination cannot contain symbolic links")
        if path.is_file():
            actual.add(path.relative_to(destination).as_posix())
    if actual - expected:
        fail("Export destination contains files outside its manifest and declared plan")


def existing_receipt(receipt: Path, plan: dict) -> bool:
    """@brief 只读核验已有收据，不能用新收据认领未知临时残留。 @param receipt 内部收据。
    @param plan 本次完整计划。 @return 是否存在准确的一致收据。
    """
    if receipt.exists() or receipt.is_symlink():
        if receipt.is_symlink() or not receipt.is_file():
            fail("Export receipt must be an ordinary file")
        existing = json.loads(receipt.read_bytes())
        if existing.get("status") not in ("started", "complete") or existing != {**plan, "status": existing["status"]}:
            fail("Export rules, naming, sources, or files differ from the existing receipt")
        return True
    return False


def prepare_receipt(receipt: Path, plan: dict) -> None:
    """@brief 规则和来源预检完成后持久化准确导出计划。 @param receipt 内部收据路径。
    @param plan 本次完整文件计划。
    """
    if existing_receipt(receipt, plan):
        return
    receipt.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(receipt, plan)


def publish_artifact(temporary: Path, final: Path, content: bytes | Path, record: dict) -> None:
    """@brief 在专用临时文件完整写入并同步校验，再原子发布。 @param temporary 已获授权的临时文件。
    @param final 最终文件。 @param content 原始来源。 @param record 持久字节计划。
    """
    with temporary.open("wb") as target:
        if isinstance(content, Path):
            with content.open("rb") as stream:
                shutil.copyfileobj(stream, target)
        else:
            target.write(content)
        target.flush()
        os.fsync(target.fileno())
    verify_output(temporary, record)
    final.parent.mkdir(parents=True, exist_ok=True)
    if final.exists():
        verify_output(final, record)
        temporary.unlink()
    else:
        temporary.rename(final)


def write_artifacts(destination: Path, plan: dict, contents: dict) -> None:
    """@brief 只重写已获持久计划授权的临时文件，最终文件保持不可覆盖。 @param destination 结果目录。
    @param plan 已持久化文件计划。 @param contents 真实来源。
    """
    directory = controlled_path(destination, ".pending")
    directory.mkdir(exist_ok=True)
    for source, record in plan["files"].items():
        path = controlled_path(destination, record["file"])
        temporary = pending_path(destination, record["file"])
        if path.exists():
            verify_output(path, record)
            temporary.unlink(missing_ok=True)
            continue
        publish_artifact(temporary, path, contents[source], record)
        verify_output(path, record)
    directory.rmdir()


def append_sample(destination: Path, plan: dict) -> None:
    """@brief 原子追加本帧实际交付清单，相同帧只允许相同内容。 @param destination 结果目录。
    @param plan 本次实际交付计划。
    """
    manifest = read_run_manifest(destination, plan)
    sample = sample_record(plan)
    existing = [item for item in manifest["samples"] if item["capture_id"] == plan["capture_id"]]
    if existing and (len(existing) != 1 or existing[0] != sample):
        fail("Run manifest already contains a different export for this capture")
    if not existing:
        atomic_json(destination / "manifest.json", planned_manifest(manifest, plan))


def clear_pending_manifest(destination: Path, plan: dict) -> None:
    """@brief 最终清单已确认后仅移除本计划对应的清单临时残留。 @param destination 结果目录。
    @param plan 已完成的持久计划。
    """
    manifest = read_run_manifest(destination, plan)
    pending = verify_pending(destination, manifest, plan, True)
    if "manifest.json.pending" in pending:
        (destination / "manifest.json.pending").unlink()


def write_export(facts: dict, declaration: dict, naming: dict, destination: Path, receipt: Path) -> dict:
    """@brief 按声明复制事实产物并保存独立导出凭证，不改变采集计数。 @param facts 完整采集事实。
    @param declaration 交付来源及路径。 @param naming 已登记名称。 @param destination 本轮交付目录。
    @param receipt 内部持久收据。 @return 完成本帧的交付计划。
    """
    try:
        destination, receipt = Path(destination).resolve(), Path(receipt).absolute()
        evidence = Path(facts["evidence_directory"]).resolve()
        if (destination.is_relative_to(evidence) or evidence.is_relative_to(destination)
                or receipt.resolve().is_relative_to(evidence) or receipt.resolve().is_relative_to(destination)):
            fail("Export output and receipt must be separate from original evidence")
        plan, contents = export_plan(facts, declaration, naming)
        manifest = read_run_manifest(destination, plan)
        registered = existing_receipt(receipt, plan)
        verify_output_inventory(destination, manifest, plan, registered)
        for record in plan["files"].values():
            path = controlled_path(destination, record["file"])
            if path.exists():
                verify_output(path, record)
        prepare_receipt(receipt, plan)
        write_artifacts(destination, plan, contents)
        append_sample(destination, plan)
        clear_pending_manifest(destination, plan)
        result = {**plan, "status": "complete"}
        atomic_json(receipt, result)
        return result
    except BaseException:
        LOGGER.exception("Declarative export failed")
        raise
