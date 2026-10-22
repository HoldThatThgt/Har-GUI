"""@brief 使用 SQLite 登记已验收五件套，支持跨运行去重和可核验的中断恢复。"""

import json
import logging
import re
import sqlite3
import hashlib
import copy
from pathlib import Path

from har_capture.hdc import fail, utc_now
from har_capture.metadata import file_info, image_info
from har_capture.signatures import build_signatures, digest
from har_capture.export import sample_prefix
from har_capture.privacy import atomic_json

LOGGER = logging.getLogger(__name__)
CHECKS = ("coverage", "coordinates", "privacy", "consistency")
SUFFIXES = {"screenshot": "_shot.png", "tree": "_tree.json", "windows_tree": "_windows_tree.json",
            "windows_akui": "_windows_akui.json", "wms": "_wms.json"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS samples (
 sample_id TEXT PRIMARY KEY, task_name TEXT NOT NULL, state_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','committed','failed')),
 source TEXT NOT NULL, manifest TEXT NOT NULL, error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_state ON samples(state_hash) WHERE status IN ('prepared','committed');
"""


def validate_identity(identity: dict) -> None:
    """@brief 拒绝不完整或歧义的项目身份。 @param identity 配置摘要及任务配额。"""
    if set(identity) != {"project_sha256", "config_sha256", "tasks"}:
        fail("Dataset identity requires project_sha256, config_sha256, and tasks")
    for key in ("project_sha256", "config_sha256"):
        if not isinstance(identity[key], str) or not re.fullmatch("[0-9a-f]{64}", identity[key]):
            fail(f"Dataset identity contains an invalid {key}")
    tasks = identity["tasks"]
    if not isinstance(tasks, list) or not tasks:
        fail("Dataset identity requires at least one task")
    for task in tasks:
        if set(task) != {"name", "target_count"} or not isinstance(task["name"], str) or not task["name"]:
            fail("Dataset task identity is invalid")
        if type(task["target_count"]) is not int or task["target_count"] < 1:
            fail("Dataset task target_count must be a positive integer")
    if len({task["name"] for task in tasks}) != len(tasks):
        fail("Dataset identity contains duplicate task names")


def connect_dataset(root: Path, identity: dict) -> sqlite3.Connection:
    """@brief 创建或核对唯一配置身份，不迁移旧数据库。 @param root 数据集目录。
    @param identity 项目身份。 @return 已开启的 SQLite 连接。
    """
    validate_identity(identity)
    root.mkdir(parents=True, exist_ok=True)
    (root / "samples").mkdir(exist_ok=True)
    connection = sqlite3.connect(root / "dataset.sqlite3", isolation_level=None, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(SCHEMA)
        connection.execute("INSERT OR IGNORE INTO identity VALUES (1,?,?)", (digest(identity), json.dumps(identity)))
        stored = connection.execute("SELECT digest FROM identity WHERE singleton=1").fetchone()[0]
        if stored != digest(identity):
            fail("Dataset configuration identity differs; refusing to reset or mix quotas")
        return connection
    except BaseException:
        connection.close()
        raise


def verify_files(directory: Path, files: dict) -> None:
    """@brief 验证五件套唯一文件集合及全部字节摘要。 @param directory 样本目录。 @param files 文件索引。"""
    if set(files) != set(SUFFIXES):
        fail("Delivery must index exactly the five required artifacts")
    expected, prefixes = set(), set()
    for kind, info in files.items():
        name = info["file"]
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(SUFFIXES[kind]):
            fail(f"Invalid delivery artifact name for {kind}")
        expected.add(name)
        prefixes.add(name.removesuffix(SUFFIXES[kind]))
        path = directory / name
        if path.is_symlink() or not path.is_file():
            fail(f"Delivery artifact is missing or is a symbolic link: {name}")
        actual = file_info(path)
        if any(actual[key] != info[key] for key in ("file", "bytes", "sha256")):
            fail(f"Delivery artifact digest mismatch: {name}")
    if len(prefixes) != 1 or len(expected) != 5 or {path.name for path in directory.iterdir()} != expected:
        fail("Delivery must contain exactly one matching five-file set")
    image_info(directory / files["screenshot"]["file"])


def read_documents(directory: Path, files: dict) -> dict:
    """@brief 读取已核验的 JSON 配套文件并验证采集身份。 @param directory 样本目录。
    @param files 文件索引。 @return JSON 文档映射。
    """
    documents = {kind: json.loads((directory / files[kind]["file"]).read_bytes())
                 for kind in SUFFIXES if kind != "screenshot"}
    capture_ids = {document["capture_id"] for document in documents.values()}
    if len(capture_ids) != 1:
        fail("Delivery artifact capture IDs do not agree")
    capture_id = next(iter(capture_ids))
    if not isinstance(capture_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", capture_id):
        fail("Delivery capture ID is invalid")
    wms = documents["wms"]
    if any(wms.get("checks", {}).get(check) != "pass" for check in CHECKS):
        fail("Delivery coverage, coordinates, privacy, and consistency must all pass")
    if any(document["canvas"] != wms["canvas"] for document in documents.values()):
        fail("Delivery artifact canvases do not agree")
    picture = image_info(directory / files["screenshot"]["file"])
    if any(picture[key] != wms["canvas"][key] for key in ("width", "height")):
        fail("Delivery screenshot dimensions do not match the canvas")
    for index, window in enumerate(wms["app_windows"]):
        for kind, key in (("accessibility", "windows_tree"), ("arkui", "windows_akui")):
            reference = window["trees"][kind]
            if (reference["file"] != files[key]["file"] or reference["json_pointer"] != f"/windows/{index}/tree"
                    or documents[key]["windows"][index]["window_id"] != window["window_id"]):
                fail("Delivery window tree reference does not identify its paired artifact")
    return documents


def prepare_manifest(directory: Path, identity: dict, acceptance: dict) -> dict:
    """@brief 只对已通过全部验收的五件套生成发布清单。 @param directory 待发布目录。
    @param identity 配置身份。 @param acceptance 视觉结果与目标应用。 @return 发布清单。
    """
    if acceptance.get("pass") is not True:
        fail("A passing saved-image acceptance result is required before publication")
    task_name = acceptance.get("task_name")
    if task_name not in {task["name"] for task in identity["tasks"]}:
        fail("Publication task is not present in the project")
    exported = acceptance["export"]
    if exported.get("status") != "complete" or any(exported.get("checks", {}).get(key) != "pass" for key in CHECKS):
        fail("Export manifest has not passed all required checks")
    files = exported["files"]
    if acceptance.get("accepted_screenshot_sha256") != files["screenshot"]["sha256"]:
        fail("Visual acceptance must refer to the final delivery screenshot digest")
    verify_files(directory, files)
    documents = read_documents(directory, files)
    bundle = acceptance["target_bundle_name"]
    signatures = build_signatures(documents["wms"], documents["windows_tree"], documents["windows_akui"], bundle)
    return {"publication_version": 1, "sample_id": documents["wms"]["capture_id"],
            "evidence_directory": str(directory.resolve().parent),
            "task_name": task_name, "target_bundle_name": bundle,
            "identity_sha256": digest(identity), "files": files, "checks": {key: "pass" for key in CHECKS},
            "accepted_at": utc_now(), "visual_acceptance": {
                "pass": True, "screenshot_sha256": acceptance["accepted_screenshot_sha256"]},
            "model_usage": acceptance["model_usage"], **signatures}


def verify_manifest(directory: Path, manifest: dict, identity: dict) -> None:
    """@brief 恢复时重新核验交付文件、配置和签名，禁止只相信数据库计数。 @param directory 样本目录。
    @param manifest 登记清单。 @param identity 当前配置身份。
    """
    if manifest["identity_sha256"] != digest(identity):
        fail("Published sample belongs to a different configuration identity")
    if manifest.get("publication_version") != 1 or manifest.get("signature_version") != 1:
        fail("Published sample rule version differs from the current implementation")
    if manifest.get("visual_acceptance", {}).get("pass") is not True:
        fail("Published sample has no passing visual acceptance record")
    if manifest["visual_acceptance"].get("screenshot_sha256") != manifest["files"]["screenshot"]["sha256"]:
        fail("Published visual acceptance does not match the delivery screenshot")
    if manifest.get("task_name") not in {task["name"] for task in identity["tasks"]}:
        fail("Published sample task is not present in the project")
    if any(manifest.get("checks", {}).get(key) != "pass" for key in CHECKS):
        fail("Published sample has failed verification gates")
    verify_files(directory, manifest["files"])
    documents = read_documents(directory, manifest["files"])
    signatures = build_signatures(documents["wms"], documents["windows_tree"], documents["windows_akui"],
                                  manifest["target_bundle_name"])
    if documents["wms"]["capture_id"] != manifest["sample_id"]:
        fail("Published sample ID does not match its artifacts")
    if any(signatures[key] != manifest[key] for key in ("page_hash", "state_hash", "window_pages")):
        fail("Published sample signature no longer matches its artifacts")


def complete_prepared(connection: sqlite3.Connection, root: Path, row: sqlite3.Row, identity: dict) -> None:
    """@brief 完成可核验的准备记录；证据丢失或歧义时明确失败且不计数。 @param connection 数据库。
    @param root 数据集目录。 @param row 准备记录。 @param identity 当前项目身份。
    """
    manifest = json.loads(row["manifest"])
    source, final = Path(row["source"]), root / "samples" / row["sample_id"]
    try:
        if source.exists() == final.exists():
            fail("Interrupted publication must have exactly one source or destination directory")
        actual = source if source.exists() else final
        finalize_names(actual, manifest)
        verify_manifest(actual, manifest, identity)
        if actual == source:
            source.rename(final)
        connection.execute("UPDATE samples SET status='committed' WHERE sample_id=?", (row["sample_id"],))
    except Exception as error:
        LOGGER.error("Interrupted publication could not be recovered: %s", error)
        connection.execute("UPDATE samples SET status='failed',error=? WHERE sample_id=?",
                           (str(error), row["sample_id"]))
        raise


def recover_dataset(connection: sqlite3.Connection, root: Path, identity: dict) -> None:
    """@brief 串行恢复准备记录并逐份验证所有已发布文件。 @param connection 数据库。
    @param root 数据集目录。 @param identity 当前项目身份。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in connection.execute("SELECT * FROM samples WHERE status='prepared'").fetchall():
            complete_prepared(connection, root, row, identity)
        for row in connection.execute("SELECT * FROM samples WHERE status='committed'").fetchall():
            verify_manifest(root / "samples" / row["sample_id"], json.loads(row["manifest"]), identity)
        known = {row[0] for row in connection.execute("SELECT sample_id FROM samples")}
        if any(path.name not in known for path in (root / "samples").iterdir()):
            fail("Dataset contains an unregistered sample directory")
        connection.commit()
    except BaseException:
        # 已记录的失败必须保留，损坏样本绝不静默扣减配额后继续。
        connection.commit()
        raise


def dataset_snapshot(connection: sqlite3.Connection, root: Path, identity: dict) -> dict:
    """@brief 返回核验后的样本和剩余配额，不返回设备坐标缓存。 @param connection 数据库。
    @param root 数据集目录。 @param identity 项目身份。 @return 断点续采状态。
    """
    samples = [json.loads(row[0]) for row in connection.execute(
        "SELECT manifest FROM samples WHERE status='committed' ORDER BY sample_id")]
    counts = {task["name"]: sum(sample["task_name"] == task["name"] for sample in samples)
              for task in identity["tasks"]}
    for sample in samples:
        sample["path"] = str(root / "samples" / sample["sample_id"])
    failures = [dict(row) for row in connection.execute(
        "SELECT sample_id,task_name,error FROM samples WHERE status='failed' ORDER BY sample_id")]
    return {"samples": samples, "counts": counts,
            "remaining": {task["name"]: task["target_count"] - counts[task["name"]] for task in identity["tasks"]},
            "failures": failures}


def dataset_status(root: Path, identity: dict) -> dict:
    """@brief 重新核验交付并返回跨运行进度。 @param root 数据集根目录。
    @param identity 项目与模型配置摘要。 @return 已验证样本与配额。
    """
    connection = None
    try:
        root = Path(root).resolve()
        validate_identity(identity)
        if not (root / "dataset.sqlite3").exists():
            samples = root / "samples"
            if samples.exists() and any(samples.iterdir()):
                fail("Dataset has sample files but no registration database")
            return {"samples": [], "counts": {task["name"]: 0 for task in identity["tasks"]},
                    "remaining": {task["name"]: task["target_count"] for task in identity["tasks"]},
                    "failures": []}
        connection = connect_dataset(root, identity)
        recover_dataset(connection, root, identity)
        return dataset_snapshot(connection, root, identity)
    except BaseException:
        LOGGER.exception("Dataset resume failed")
        raise
    finally:
        if connection is not None:
            connection.close()


def reserve_sample(connection: sqlite3.Connection, identity: dict, source: Path, manifest: dict) -> None:
    """@brief 在写入准备记录前核对状态唯一性与配额。 @param connection 数据库。
    @param identity 项目身份。 @param source 完整五件套目录。 @param manifest 待发布清单。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        task = next(item for item in identity["tasks"] if item["name"] == manifest["task_name"])
        count = connection.execute("SELECT COUNT(*) FROM samples WHERE task_name=? AND status!='failed'",
                                   (task["name"],)).fetchone()[0]
        if count >= task["target_count"]:
            fail("Task quota is already complete")
        duplicate = connection.execute("SELECT sample_id,task_name FROM samples "
                                       "WHERE state_hash=? AND status!='failed'",
                                       (manifest["state_hash"],)).fetchone()
        if duplicate:
            fail(f"Duplicate state already belongs to task {duplicate['task_name']}: {duplicate['sample_id']}")
        assign_names(connection, source, manifest)
        connection.execute("INSERT INTO samples VALUES (?,?,?,'prepared',?,?,NULL)",
                           (manifest["sample_id"], task["name"], manifest["state_hash"], str(source),
                            json.dumps(manifest, ensure_ascii=False, sort_keys=True)))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def assign_names(connection: sqlite3.Connection, source: Path, manifest: dict) -> None:
    """@brief 在写锁内分配稳定页面名和状态序号，先持久计划再修改任何文件。 @param connection 数据库。
    @param source 未修改五件套目录。 @param manifest 将写入准备记录的清单。
    """
    previous = [json.loads(row[0]) for row in connection.execute("SELECT manifest FROM samples")]
    page_id = manifest["page_hash"][:16]
    if any(item["page_id"] == page_id and item["page_hash"] != manifest["page_hash"] for item in previous):
        fail("Stable page identifier collision")
    states = [int(item["state_id"]) for item in previous if item["page_hash"] == manifest["page_hash"]]
    state_id = f"{max(states, default=0) + 1:06d}"
    original = copy.deepcopy(manifest["files"])
    wms = json.loads((source / original["wms"]["file"]).read_bytes())
    sample = {**wms["sample"], "page_id": page_id, "state_id": state_id}
    prefix = sample_prefix(sample)
    files = {kind: {**info, "file": prefix + SUFFIXES[kind]} for kind, info in original.items()}
    rewrite_wms_names(wms, sample, files)
    data = (json.dumps(wms, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    files["wms"].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    manifest.update(page_id=page_id, state_id=state_id, source_files=original,
                    publication_sample=sample, files=files)


def rewrite_wms_names(wms: dict, sample: dict, files: dict) -> None:
    """@brief 同时更新样本名称及窗口的五件套内部引用。 @param wms 窗口文档。
    @param sample 持久命名计划。 @param files 最终文件索引。
    """
    wms["sample"] = sample
    for window in wms["app_windows"]:
        for kind, key in (("accessibility", "windows_tree"), ("arkui", "windows_akui")):
            window["trees"][kind]["file"] = files[key]["file"]


def rename_prepared_file(directory: Path, manifest: dict, kind: str) -> None:
    """@brief 核验旧名或新名对应同一已准备文件，再原子改名。 @param directory 当前目录。
    @param manifest 持久命名计划。 @param kind 配套文件类型。
    """
    before, after = manifest["source_files"][kind], manifest["files"][kind]
    old, new = directory / before["file"], directory / after["file"]
    if old != new and old.exists() == new.exists():
        fail("Interrupted artifact rename has an ambiguous source or destination")
    actual = old if old.exists() else new
    if actual.is_symlink() or not actual.is_file():
        fail("Interrupted artifact rename source is missing or is a symbolic link")
    actual_info = file_info(actual)
    if not any(all(actual_info[key] == expected[key] for key in ("bytes", "sha256"))
               for expected in (before, after)):
        fail("Interrupted artifact bytes differ from the durable naming plan")
    if actual != new:
        actual.rename(new)


def finalize_names(directory: Path, manifest: dict) -> None:
    """@brief 幂等执行逐文件命名计划，修复 WMS 并核验最终摘要。 @param directory 本次目录。
    @param manifest SQLite 已持久化的命名计划。
    """
    expected = {info["file"] for files in (manifest["source_files"], manifest["files"]) for info in files.values()}
    wms_path = directory / manifest["files"]["wms"]["file"]
    temporary = wms_path.with_suffix(wms_path.suffix + ".pending")
    expected.add(temporary.name)
    if any(path.name not in expected or not path.is_file() or path.is_symlink() for path in directory.iterdir()):
        fail("Interrupted naming contains unrecognized files")
    temporary.unlink(missing_ok=True)
    for kind in SUFFIXES:
        rename_prepared_file(directory, manifest, kind)
    if file_info(wms_path)["sha256"] != manifest["files"]["wms"]["sha256"]:
        wms = json.loads(wms_path.read_bytes())
        rewrite_wms_names(wms, manifest["publication_sample"], manifest["files"])
        atomic_json(wms_path, wms)
    verify_files(directory, manifest["files"])


def publish_sample(root: Path, identity: dict, staging_directory: Path, acceptance: dict) -> dict:
    """@brief 验收、去重、持久准备、目录原子移动后登记配额。 @param root 数据集根目录。
    @param identity 项目身份。 @param staging_directory 五件套临时目录。
    @param acceptance 视觉验收与导出清单。 @return 已发布样本清单及最终路径。
    """
    connection = None
    try:
        root, source = Path(root).resolve(), Path(staging_directory).resolve()
        connection = connect_dataset(root, identity)
        recover_dataset(connection, root, identity)
        manifest = prepare_manifest(source, identity, acceptance)
        if source.stat().st_dev != root.stat().st_dev or root == source or root.is_relative_to(source):
            fail("Publication requires a separate sample directory on the dataset filesystem")
        final = root / "samples" / manifest["sample_id"]
        if final.exists():
            fail("Publication destination already exists")
        reserve_sample(connection, identity, source, manifest)
        # prepared 已持久化；恢复逻辑持有写锁，避免同一准备记录被并发重复移动。
        recover_dataset(connection, root, identity)
        return {**manifest, "path": str(final)}
    except BaseException:
        LOGGER.exception("Dataset publication failed")
        raise
    finally:
        if connection is not None:
            connection.close()
