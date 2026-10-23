"""@brief 登记已验收采集事实，交付文件的选择与命名不参与计数和去重。"""

import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from har_capture.capture_facts import build_capture_facts
from har_capture.dataset_identity import (
    configuration_sources, manifest_configuration, run_identity, validate_identity, validate_run_identity,
    verify_contract, verify_sample_contract,
)
from har_capture.hdc import fail, utc_now
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json
from har_capture.signatures import build_signatures, digest

LOGGER = logging.getLogger(__name__)
CHECKS = ("coverage", "coordinates", "privacy", "consistency")
SCHEMA = """
CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS samples (
 sample_id TEXT PRIMARY KEY, task_name TEXT NOT NULL, state_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('prepared','committed','failed')),
 source TEXT NOT NULL, manifest TEXT NOT NULL, error TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS unique_state ON samples(state_hash) WHERE status IN ('prepared','committed');
"""


def require_capture_id(value: object) -> str:
    """@brief 保留采集标识的路径安全约束，不将任意路径当样本名称。 @param value 原标识。 @return 标识。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        fail("Capture ID is invalid")
    return value


def verify_dataset_identity(connection: sqlite3.Connection, identity: dict, sources: dict) -> None:
    """@brief 按原摘要核对数据库采集身份，不把新交付声明写入旧记录。 @param connection 连接。
    @param identity 当前配置。 @param sources 原配置来源。
    """
    stored = connection.execute("SELECT digest,value FROM identity WHERE singleton=1").fetchone()
    if stored is None:
        fail("Dataset has no registered collection identity")
    original = json.loads(stored["value"])
    validate_run_identity(original)
    if stored["digest"] != digest(original):
        fail("Stored dataset identity digest does not match its original value")
    verify_contract(stored["digest"], identity, sources)


def connect_dataset(root: Path, identity: dict) -> sqlite3.Connection:
    """@brief 建立采集连接，已有身份只核验不替换。 @param root 内部存储。
    @param identity 本次配置。 @return 数据库连接。
    """
    validate_identity(identity)
    root.mkdir(parents=True, exist_ok=True)
    (root / "samples").mkdir(exist_ok=True)
    connection = sqlite3.connect(root / "dataset.sqlite3", isolation_level=None, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript(SCHEMA)
        header = run_identity(identity)
        connection.execute("INSERT OR IGNORE INTO identity VALUES (1,?,?)", (digest(header), json.dumps(header)))
        verify_dataset_identity(connection, identity, configuration_sources(identity))
        return connection
    except BaseException:
        LOGGER.exception("Dataset connection failed")
        connection.close()
        raise


def verify_files(directory: Path, files: dict) -> None:
    """@brief 按实际清单核验任意数量和层级，不推断缺失角色。 @param directory 文件根。
    @param files 明确的相对路径、大小和摘要。
    """
    if not isinstance(files, dict) or not files:
        fail("Artifact manifest must contain files")
    expected, folded = set(), set()
    for info in files.values():
        name = info["file"]
        relative = Path(name) if isinstance(name, str) else None
        if relative is None or not name or relative.drive or relative.is_absolute() or ".." in relative.parts:
            fail("Artifact manifest contains an unsafe relative path")
        key = relative.as_posix()
        if key.casefold() in folded:
            fail("Artifact manifest contains duplicate paths")
        folded.add(key.casefold())
        expected.add(key)
        target = directory / relative
        if target.is_symlink() or not target.is_file() or not target.resolve().is_relative_to(directory.resolve()):
            fail("Artifact manifest source is missing or outside its directory")
        actual = file_info(target)
        if any(actual[field] != info[field] for field in ("bytes", "sha256")):
            fail("Artifact digest mismatch: " + key)
    actual = {item.relative_to(directory).as_posix() for item in directory.rglob("*") if item.is_file()}
    if actual != expected or any(item.is_symlink() for item in directory.rglob("*")):
        fail("Artifact files differ from their declared manifest")


def capture_signatures(facts: dict, bundle: str) -> dict:
    """@brief 始终从完整内部窗口和双树计算签名。 @param facts 完整事实。
    @param bundle 目标应用。 @return 页面和状态签名。
    """
    ids = {window["window_id"] for window in facts["windows"]["app_windows"]}
    accessibility = {"windows": [item for item in facts["accessibility"]["windows"] if item["window_id"] in ids]}
    return build_signatures(facts["windows"], accessibility, facts["arkui"], bundle)


def capture_facts(manifest: dict, configuration: dict) -> dict:
    """@brief 从明确的原证据位置重核验完整事实。 @param manifest 原登记。
    @param configuration 原采集要求。 @return 规范事实。
    """
    evidence = Path(manifest["evidence_directory"])
    if not evidence.is_absolute() or evidence.is_symlink() or not evidence.is_dir():
        fail("Registered capture evidence directory is unavailable")
    metadata = json.loads((evidence / "metadata.json").read_bytes())
    options = {key: configuration["project"]["dataset"][key] for key in ("canvas", "stability_regions")}
    return build_capture_facts(evidence, metadata, options)


def verify_manifest(directory: Path, manifest: dict, identity: dict, sources: dict) -> dict:
    """@brief 核验原清单和全部采集事实，少选交付文件不能绕过检查。 @param directory 登记文件根。
    @param manifest 原登记。 @param identity 当前身份。 @param sources 来源。 @return 完整事实。
    """
    require_capture_id(manifest["sample_id"])
    verify_contract(manifest["identity_sha256"], identity, sources)
    original = manifest_configuration(manifest, identity, sources)
    if manifest.get("publication_version") != 1 or manifest.get("signature_version") != 1:
        fail("Published capture rule version differs from the current implementation")
    if any(manifest.get("checks", {}).get(key) != "pass" for key in CHECKS):
        fail("Published capture has failed verification gates")
    verify_files(directory, manifest["files"])
    facts = capture_facts(manifest, original)
    if facts["capture_id"] != manifest["sample_id"]:
        fail("Published sample ID does not match its capture facts")
    visual = manifest["visual_acceptance"]
    if visual.get("pass") is not True or visual["screenshot_sha256"] != facts["screenshot"]["sha256"]:
        fail("Published visual acceptance does not match the final capture screenshot")
    verify_sample_contract(facts, manifest, original)
    signatures = capture_signatures(facts, manifest["target_bundle_name"])
    if any(signatures[key] != manifest[key] for key in ("page_hash", "state_hash", "window_pages")):
        fail("Published capture signature no longer matches its facts")
    return facts


def prepare_manifest(directory: Path, identity: dict, acceptance: dict) -> dict:
    """@brief 将已通过图片验收的规范采集登记为样本。 @param directory 内部准备目录。
    @param identity 当前身份。 @param acceptance 图片验收与事实清单。 @return 登记清单。
    """
    if acceptance.get("pass") is not True:
        fail("A passing saved-image acceptance is required before registration")
    prepared = acceptance["capture"]
    require_capture_id(prepared["capture_id"])
    if prepared.get("status") != "complete" or set(prepared["files"]) != {"capture"}:
        fail("Capture preparation must provide its complete canonical record")
    verify_files(directory, prepared["files"])
    stored = json.loads((directory / prepared["files"]["capture"]["file"]).read_bytes())
    manifest = {"publication_version": 1, "sample_id": prepared["capture_id"],
                "evidence_directory": str(directory.resolve().parent), "task_name": acceptance["task_name"],
                "target_bundle_name": acceptance["target_bundle_name"], "files": prepared["files"],
                "identity_sha256": digest(run_identity(identity)), "checks": dict.fromkeys(CHECKS, "pass"),
                "accepted_at": utc_now(), "visual_acceptance": {"pass": True,
                    "screenshot_sha256": acceptance["accepted_screenshot_sha256"]},
                "model_usage": acceptance["model_usage"]}
    facts = capture_facts(manifest, identity["configuration"])
    if (stored != facts or prepared["screenshot"] != facts["screenshot"]
            or prepared["capture_id"] != facts["capture_id"] or prepared["canvas"] != facts["canvas"]
            or prepared["checks"] != facts["windows"]["checks"]
            or Path(prepared["path"]).resolve() != directory.resolve()):
        fail("Prepared capture facts differ from their original evidence")
    if acceptance["accepted_screenshot_sha256"] != facts["screenshot"]["sha256"]:
        fail("Visual acceptance differs from the actual final screenshot")
    manifest.update(capture_signatures(facts, manifest["target_bundle_name"]))
    task = next((item for item in identity["configuration"]["project"]["tasks"]
                 if item["name"] == manifest["task_name"]), None)
    if task is None:
        fail("Capture task is not present in the project")
    manifest["publication_sample"] = {"app": task["app"], "scene": task["scene"]}
    verify_sample_contract(facts, manifest, identity["configuration"])
    return manifest


def reserve_sample(connection: sqlite3.Connection, identity: dict, source: Path, manifest: dict) -> None:
    """@brief 在事务内核对唯一状态和配额，分配稳定页面状态标识。 @param connection 数据库。
    @param identity 当前任务。 @param source 准备来源。 @param manifest 新登记事实。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        task = next(item for item in identity["tasks"] if item["name"] == manifest["task_name"])
        count = connection.execute("SELECT COUNT(*) FROM samples WHERE task_name=? AND status!='failed'",
                                   (task["name"],)).fetchone()[0]
        if count >= task["target_count"]:
            fail("Task quota is already complete")
        if connection.execute("SELECT 1 FROM samples WHERE state_hash=? AND status!='failed'",
                              (manifest["state_hash"],)).fetchone():
            fail("Duplicate capture state is already registered")
        previous = [json.loads(row[0]) for row in connection.execute("SELECT manifest FROM samples")]
        page_id = manifest["page_hash"][:16]
        if any(item["page_id"] == page_id and item["page_hash"] != manifest["page_hash"] for item in previous):
            fail("Stable page identifier collision")
        states = [int(item["state_id"]) for item in previous if item["page_hash"] == manifest["page_hash"]]
        manifest.update(page_id=page_id, state_id=f"{max(states, default=0) + 1:06d}")
        manifest["publication_sample"].update(page_id=page_id, state_id=manifest["state_id"])
        connection.execute("INSERT INTO samples VALUES (?,?,?,'prepared',?,?,NULL)",
                           (manifest["sample_id"], task["name"], manifest["state_hash"], str(source),
                            json.dumps(manifest, ensure_ascii=False, sort_keys=True)))
        connection.commit()
    except BaseException:
        LOGGER.exception("Capture reservation failed")
        connection.rollback()
        raise


def complete_prepared(connection: sqlite3.Connection, root: Path, row: sqlite3.Row, identity: dict) -> None:
    """@brief 按实际清单恢复原子移动，不改变文件名或假定数量。 @param connection 连接。
    @param root 内部根。 @param row 准备记录。 @param identity 当前身份。
    """
    manifest = json.loads(row["manifest"])
    source, final = Path(row["source"]), root / "samples" / require_capture_id(row["sample_id"])
    if source.exists() == final.exists():
        fail("Interrupted registration requires exactly one source or destination")
    actual = source if source.exists() else final
    verify_manifest(actual, manifest, identity, configuration_sources(identity))
    if actual == source:
        source.rename(final)
    connection.execute("UPDATE samples SET status='committed' WHERE sample_id=?", (row["sample_id"],))


def verify_receipt(sample: dict) -> None:
    """@brief 只读比对明确最终收据，缺失时不猜完成状态。 @param sample 含实际登记路径的样本。"""
    path = Path(sample["evidence_directory"]) / "publication.json"
    if path.is_symlink() or not path.is_file() or json.loads(path.read_bytes()) != sample:
        fail("Published capture receipt differs from its registered record")


def recover_dataset(connection: sqlite3.Connection, root: Path, identity: dict) -> None:
    """@brief 在写锁内恢复准备记录，再核验已经登记的采集。 @param connection 数据库。
    @param root 内部根。 @param identity 当前身份。
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        for row in connection.execute("SELECT * FROM samples WHERE status='prepared'").fetchall():
            complete_prepared(connection, root, row, identity)
        sources = configuration_sources(identity)
        for row in connection.execute("SELECT manifest FROM samples WHERE status='committed'").fetchall():
            manifest = json.loads(row[0])
            final = root / "samples" / require_capture_id(manifest["sample_id"])
            verify_manifest(final, manifest, identity, sources)
            sample = {**manifest, "path": str(final)}
            receipt = Path(manifest["evidence_directory"]) / "publication.json"
            if not receipt.exists():
                atomic_json(receipt, sample)
            verify_receipt(sample)
        known = {row[0] for row in connection.execute("SELECT sample_id FROM samples")}
        if any(path.name not in known for path in (root / "samples").iterdir()):
            fail("Dataset contains an unregistered capture directory")
        connection.commit()
    except BaseException:
        LOGGER.exception("Capture registration recovery failed")
        connection.rollback()
        raise


def dataset_snapshot(connection: sqlite3.Connection, root: Path, identity: dict) -> dict:
    """@brief 从已登记采集读取配额，重复交付不改变计数。 @param connection 数据库。
    @param root 内部根。 @param identity 当前任务。 @return 采集进度。
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
            "remaining": {task["name"]: max(0, task["target_count"] - counts[task["name"]])
                          for task in identity["tasks"]}, "failures": failures}


def dataset_status(root: Path, identity: dict) -> dict:
    """@brief 恢复采集登记，不根据交付声明重新生成文件。 @param root 内部根。
    @param identity 当前身份。 @return 已核验采集进度。
    """
    connection = None
    try:
        root = root.resolve()
        validate_identity(identity)
        if not (root / "dataset.sqlite3").exists():
            if (root / "samples").exists() and any((root / "samples").iterdir()):
                fail("Dataset has capture files but no registration database")
            return {"samples": [], "counts": {task["name"]: 0 for task in identity["tasks"]},
                    "remaining": {task["name"]: task["target_count"] for task in identity["tasks"]}, "failures": []}
        connection = connect_dataset(root, identity)
        recover_dataset(connection, root, identity)
        return dataset_snapshot(connection, root, identity)
    except BaseException:
        LOGGER.exception("Dataset resume failed")
        raise
    finally:
        if connection is not None:
            connection.close()


def publish_sample(root: Path, identity: dict, source: Path, acceptance: dict) -> dict:
    """@brief 登记完整已验收采集，交付由独立入口生成。 @param root 内部根。
    @param identity 当前身份。 @param source 内部事实准备目录。 @param acceptance 图片验收。 @return 登记。
    """
    connection = None
    try:
        root, source = root.resolve(), source.resolve()
        connection = connect_dataset(root, identity)
        recover_dataset(connection, root, identity)
        manifest = prepare_manifest(source, identity, acceptance)
        if source.stat().st_dev != root.stat().st_dev or root == source or root.is_relative_to(source):
            fail("Capture registration requires a separate directory on the dataset filesystem")
        if (root / "samples" / manifest["sample_id"]).exists():
            fail("Capture registration destination already exists")
        reserve_sample(connection, identity, source, manifest)
        recover_dataset(connection, root, identity)
        return {**manifest, "path": str(root / "samples" / manifest["sample_id"])}
    except BaseException:
        LOGGER.exception("Capture registration failed")
        raise
    finally:
        if connection is not None:
            connection.close()


def read_accepted_sample(root: Path, identity: dict, sample_id: str) -> tuple[dict, dict, dict]:
    """@brief 以只读连接核验指定成功帧，禁止修复、重计数和模型验收。 @param root 内部根。
    @param identity 当前采集声明。 @param sample_id 明确样本。 @return 原登记、完整事实、原配置。
    """
    root = root.resolve()
    require_capture_id(sample_id)
    validate_identity(identity)
    sources = configuration_sources(identity)
    uri = (root / "dataset.sqlite3").as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            verify_dataset_identity(connection, identity, sources)
            row = connection.execute("SELECT manifest FROM samples WHERE sample_id=? AND status='committed'",
                                     (sample_id,)).fetchone()
            if row is None:
                fail("Offline export requires an already accepted capture")
            manifest = json.loads(row[0])
            directory = root / "samples" / manifest["sample_id"]
            facts = verify_manifest(directory, manifest, identity, sources)
            sample = {**manifest, "path": str(directory)}
            verify_receipt(sample)
            return sample, facts, manifest_configuration(manifest, identity, sources)
    except BaseException:
        LOGGER.exception("Accepted capture read failed")
        raise
