"""@brief 区分数据集采集合同与各次运行来源，保留既有登记摘要和清单。"""

import copy
import hashlib
import json
import re
import tomllib
from pathlib import Path

from har_capture.hdc import fail
from har_capture.privacy import verify_privacy_receipt
from har_capture.signatures import digest

HEADER_KEYS = {"project_sha256", "config_sha256", "tasks"}
CONFIGURATION_KEYS = {"project", "profile", "projectPath", "configPath", "outputPath", "datasetPath", "digests"}


def validate_run_identity(value: dict) -> None:
    """@brief 核验原有三字段运行来源。 @param value 已登记或待登记运行摘要。"""
    if not isinstance(value, dict) or set(value) != HEADER_KEYS:
        fail("Run identity requires project_sha256, config_sha256, and tasks")
    for key in ("project_sha256", "config_sha256"):
        if not isinstance(value[key], str) or not re.fullmatch("[0-9a-f]{64}", value[key]):
            fail(f"Run identity contains an invalid {key}")
    tasks = value["tasks"]
    if not isinstance(tasks, list) or not tasks:
        fail("Run identity requires at least one task")
    for task in tasks:
        if not isinstance(task, dict) or set(task) != {"name", "target_count"}:
            fail("Dataset task identity is invalid")
        if not isinstance(task["name"], str) or not task["name"].strip():
            fail("Dataset task name must not be empty")
        if type(task["target_count"]) is not int or task["target_count"] < 1:
            fail("Dataset task target_count must be a positive integer")
    if len({task["name"] for task in tasks}) != len(tasks):
        fail("Dataset identity contains duplicate task names")


def validate_configuration(configuration: dict) -> None:
    """@brief 验证已装载运行快照的必要结构。 @param configuration 完整配置快照。"""
    if not isinstance(configuration, dict) or set(configuration) != CONFIGURATION_KEYS:
        fail("Dataset identity requires a complete loaded configuration snapshot")
    project = configuration["project"]
    required = {"schema_version", "run", "agent", "dataset", "tools", "tasks"}
    if (not isinstance(project, dict) or not required.issubset(project)
            or set(project) - required - {"delivery"}):
        fail("Saved project configuration structure is invalid")
    if not isinstance(configuration["profile"], dict) or not configuration["profile"]:
        fail("Saved model profile is missing")
    for key in ("projectPath", "configPath", "outputPath", "datasetPath"):
        if not isinstance(configuration[key], str) or not configuration[key].strip():
            fail(f"Saved configuration has an invalid {key}")
    if not isinstance(configuration["digests"], dict) or set(configuration["digests"]) != {"project", "config"}:
        fail("Saved configuration digests are invalid")
    try:
        header = configuration_identity(configuration)
        canvas = project["dataset"]["canvas"]
        valid_canvas = set(canvas) == {"width", "height"} and all(
            type(value) is int and value > 0 for value in canvas.values())
        if not valid_canvas or not isinstance(project["run"], dict):
            fail("Saved configuration canvas or run declaration is invalid")
        for task in project["tasks"]:
            if any(key not in task for key in ("app", "scene", "privacy", "instruction", "acceptance")):
                fail("Saved task configuration is incomplete")
            if type(task["privacy"]["enabled"]) is not bool or not task["privacy"]["reason"].strip():
                fail("Saved task privacy declaration is invalid")
        validate_run_identity(header)
    except (KeyError, TypeError, AttributeError) as error:
        fail(f"Saved configuration structure is invalid: {type(error).__name__}")


def configuration_identity(configuration: dict) -> dict:
    """@brief 从保存快照提取既有三字段来源。 @param configuration 已装载快照。 @return 运行来源。"""
    return {"project_sha256": configuration["digests"]["project"],
            "config_sha256": configuration["digests"]["config"],
            "tasks": [{"name": task["name"], "target_count": task["target_count"]}
                      for task in configuration["project"]["tasks"]]}


def run_identity(identity: dict) -> dict:
    """@brief 提取登记所用三字段，不把快照混入原有摘要。 @param identity 本次完整身份。 @return 运行来源。"""
    return {key: copy.deepcopy(identity[key]) for key in ("project_sha256", "config_sha256", "tasks")}


def validate_identity(identity: dict) -> None:
    """@brief 要求完整快照与运行来源相互一致。 @param identity 本次 RPC 身份与配置快照。"""
    if not isinstance(identity, dict) or set(identity) != HEADER_KEYS | {"configuration", "project_sources"}:
        fail("Dataset identity requires run hashes, tasks, configuration, and project_sources")
    validate_run_identity(run_identity(identity))
    validate_configuration(identity["configuration"])
    if run_identity(identity) != configuration_identity(identity["configuration"]):
        fail("Dataset run identity does not match its loaded configuration")
    sources = identity["project_sources"]
    if not isinstance(sources, list) or any(not isinstance(path, str) or not path.strip() for path in sources):
        fail("Project sources must be an explicit array of local file or directory paths")
    for value in sources:
        path = Path(value)
        if not path.is_absolute() or path.is_symlink() or not (path.is_file() or path.is_dir()):
            fail("Project source must be an existing absolute ordinary file or directory")
    current = identity["configuration"]
    raw = read_project_bytes(Path(current["projectPath"]))
    if hashlib.sha256(raw).hexdigest() != current["digests"]["project"]:
        fail("Current project digest differs from its actual source")
    verify_project_snapshot(current, raw)


def normalized_strings(value: object) -> object:
    """@brief 对比两种 TOML 解析器时统一字符串换行，不修改原始字节。 @param value 解析值。
    @return 递归统一 CRLF 的值。
    """
    if isinstance(value, str):
        return value.replace("\r\n", "\n")
    if isinstance(value, list):
        return [normalized_strings(item) for item in value]
    if isinstance(value, dict):
        return {key: normalized_strings(item) for key, item in value.items()}
    return value


def read_project_bytes(path: Path) -> bytes:
    """@brief 仅读取明确配置的普通本地原文。 @param path 实际来源路径。 @return 未规范化的原始字节。"""
    if path.is_symlink() or not path.is_file():
        fail("Raw project source is missing or is a symbolic link")
    try:
        return path.read_bytes()
    except OSError as error:
        fail(f"Cannot read raw project source: {type(error).__name__}")


def verify_project_snapshot(configuration: dict, raw: bytes) -> None:
    """@brief 以精确原文摘要及解析内容核对运行快照。 @param configuration 保存的快照。
    @param raw 对应原始 TOML 字节。
    """
    if hashlib.sha256(raw).hexdigest() != configuration["digests"]["project"]:
        fail("Raw project source digest differs from its saved project digest")
    try:
        project = tomllib.loads(raw.decode("utf-8"))
        project["dataset"].setdefault("stability_regions", [])
    except (UnicodeError, ValueError, KeyError, TypeError) as error:
        fail(f"Raw project source is invalid: {type(error).__name__}")
    if normalized_strings(project) != normalized_strings(configuration["project"]):
        fail("Saved project snapshot differs from its raw TOML source")


def configuration_paths(identity: dict) -> set[Path]:
    """@brief 只索引声明的输出归档、内部运行和显式项目来源。 @param identity 当前身份。
    @return 实际配置快照路径，不跟随记录中的其他绝对路径。
    """
    current = identity["configuration"]
    paths = set(Path(current["outputPath"]).glob("*/configuration.json"))
    paths.update((Path(current["datasetPath"]) / "runs").glob("*/configuration.json"))
    for value in identity["project_sources"]:
        path = Path(value)
        if path.is_dir():
            paths.update(path.rglob("configuration.json"))
        elif (path.parent / "configuration.json").is_file():
            paths.add(path.parent / "configuration.json")
    return paths


def raw_project_sources(identity: dict) -> dict:
    """@brief 仅索引当前路径、新运行归档和显式声明的原文。 @param identity 本次身份。
    @return 按实际字节 SHA256 索引的原文。
    """
    configuration = identity["configuration"]
    paths = {Path(configuration["projectPath"])}
    paths.update(path.parent / "project.toml" for path in configuration_paths(identity)
                 if (path.parent / "project.toml").exists())
    for value in identity["project_sources"]:
        path = Path(value)
        if path.is_dir():
            paths.update(path.rglob("*.toml"))
        else:
            paths.add(path)
    projects = {}
    for path in sorted(paths):
        raw = read_project_bytes(path)
        projects[hashlib.sha256(raw).hexdigest()] = raw
    return projects


def verify_saved_project(record: dict) -> None:
    """@brief 仅为被引用的历史快照证明真实项目原文。 @param record 来源、实际原文索引及配置。"""
    configuration = record["configuration"]
    raw = record["projects"].get(configuration["digests"]["project"])
    if raw is None:
        fail("Raw project source is missing for the saved project digest")
    verify_project_snapshot(configuration, raw)


def stable_contract(configuration: dict) -> dict:
    """@brief 明确投影采集语义，交付声明及存储位置不决定采集身份。 @param configuration 已验证快照。
    @return 严格采集合同。
    """
    project = configuration["project"]
    tasks = [{key: copy.deepcopy(value) for key, value in task.items()
              if key not in {"instruction", "acceptance", "target_count"}} for task in project["tasks"]]
    for task in tasks:
        del task["privacy"]["reason"]
    collection = {"schema_version": project["schema_version"], "run_name": project["run"]["name"],
                  "agent": project["agent"], "tools": project["tools"], "tasks": tasks,
                  "canvas": project["dataset"]["canvas"],
                  "stability_regions": project["dataset"]["stability_regions"]}
    return {"project": collection, "config_sha256": configuration["digests"]["config"],
            "profile": copy.deepcopy(configuration["profile"])}


def configuration_sources(identity: dict) -> dict:
    """@brief 从本次输出目录读取运行来源，不访问历史绝对路径。 @param identity 本次身份。
    @return 运行摘要到原有配置快照及实际来源路径的映射。
    """
    validate_identity(identity)
    current = identity["configuration"]
    records = [{"configuration": current, "path": None}]
    for path in sorted(configuration_paths(identity)):
        if path.is_symlink() or not path.is_file():
            fail("Saved run configuration must be an ordinary file")
        try:
            configuration = json.loads(path.read_bytes())
        except (OSError, ValueError) as error:
            fail(f"Cannot read saved run configuration: {path.name} ({type(error).__name__})")
        validate_configuration(configuration)
        records.append({"configuration": configuration, "path": str(path.resolve())})
    sources, projects = {}, raw_project_sources(identity)
    for record in records:
        configuration = record["configuration"]
        key = digest(configuration_identity(configuration))
        record["projects"] = projects
        sources.setdefault(key, []).append(record)
    return sources


def resolve_configuration(identity_sha256: str, sources: dict) -> dict:
    """@brief 按原清单摘要解析唯一内容的运行配置。 @param identity_sha256 原摘要。
    @param sources 已核验来源集合。 @return 原始解析快照。
    """
    records = sources.get(identity_sha256, [])
    if not records:
        fail("Saved run configuration is missing for the registered identity")
    for record in records:
        verify_saved_project(record)
    for record in records[1:]:
        if any(normalized_strings(record["configuration"][field])
               != normalized_strings(records[0]["configuration"][field]) for field in ("project", "profile")):
            fail("Saved run configurations disagree for the same run identity")
    return sorted(records, key=lambda record: record["path"] is None)[0]["configuration"]


def verify_contract(identity_sha256: str, identity: dict, sources: dict) -> dict:
    """@brief 使用原运行快照核对当前采集合同。 @param identity_sha256 原来源摘要。
    @param identity 本次身份。 @param sources 快照集合。 @return 已匹配历史配置。
    """
    original = resolve_configuration(identity_sha256, sources)
    if stable_contract(original) != stable_contract(identity["configuration"]):
        fail("Dataset configuration identity differs in its collection contract")
    return original


def read_capture_identity(path: Path) -> dict:
    """@brief 读取必须存在的普通采集身份文件。 @param path 当前副本内的身份文件。 @return 原记录。"""
    if path.is_symlink() or not path.is_file():
        fail("Cross-run publication requires its original capture and finalization identities")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, ValueError) as error:
        fail(f"Cannot read capture identity: {type(error).__name__}")
    if not isinstance(value, dict):
        fail("Capture identity must be an object")
    return value


def finalization_configuration(manifest: dict, sample: dict, identity: dict, sources: dict) -> dict:
    """@brief 由明确的独立验收目录定位发布快照，同时核对原绝对路径。 @param manifest 登记清单。
    @param sample 原样本身份。 @param identity 本次身份。 @param sources 已核验快照。 @return 发布运行配置。
    """
    directory = sample.get("finalization_directory")
    if not isinstance(directory, str) or not directory:
        fail("Published capture origin belongs to a different run identity without a recorded finalization")
    actual = (Path(directory) / "configuration.json").resolve()
    records = [record for record in sources.get(manifest["identity_sha256"], [])
               if record["path"] == str(actual)]
    if len(records) != 1:
        fail("Capture finalization run differs from its registered identity")
    configuration = records[0]["configuration"]
    verify_saved_project(records[0])
    return configuration


def verify_capture_identity(manifest: dict, candidate: dict) -> None:
    """@brief 绑定采集身份、任务、应用与原位置。 @param manifest 原登记清单。 @param candidate 原身份。"""
    fields = {"capture_id": manifest["sample_id"], "task_name": manifest["task_name"],
              "target_bundle_name": manifest["target_bundle_name"]}
    if any(candidate.get(key) != value for key, value in fields.items()):
        fail("Published finalization identity differs from its original capture")
    if (not isinstance(candidate.get("path"), str)
            or Path(candidate["path"]).resolve() != Path(manifest["evidence_directory"]).resolve()):
        fail("Published finalization path differs from its original capture")


def finalized_capture_configuration(manifest: dict, identity: dict, sources: dict, captured: dict) -> dict:
    """@brief 明确关联不同运行的采集与独立验收，不把相同合同当作任意重绑定许可。 @param manifest 原清单。
    @param identity 本次身份。 @param sources 已核验来源。 @param captured 采集运行。 @return 发布运行配置。
    """
    origin = Path(manifest["evidence_directory"])
    if origin.name != manifest["sample_id"] or origin.parent.name != "captures":
        fail("Captured evidence origin differs from its saved run path")
    directory = origin
    path = directory / "sample.json"
    if not path.is_file():
        fail("Published capture origin belongs to a different run identity without a recorded finalization")
    sample = read_capture_identity(path)
    verify_capture_identity(manifest, sample)
    candidate_path = directory / "candidate.json"
    if candidate_path.exists() or candidate_path.is_symlink():
        candidate = read_capture_identity(candidate_path)
        verify_capture_identity(manifest, candidate)
        if any(sample.get(key) != value for key, value in candidate.items()):
            fail("Finalized sample differs from its original candidate identity")
    elif (sample.get("origin") != "direct_capture" or sample.get("page_name") != manifest["sample_id"]
          or sample.get("window_changes") != []):
        fail("Cross-run publication has no proven original candidate or direct capture identity")
    publication = finalization_configuration(manifest, sample, identity, sources)
    if stable_contract(captured) != stable_contract(publication):
        fail("Capture and finalization runs differ in their collection contract")
    return publication


def manifest_configuration(manifest: dict, identity: dict, sources: dict) -> dict:
    """@brief 选择所属运行快照并拒绝原摘要冲突。 @param manifest 原清单。
    @param identity 本次身份。 @param sources 已核验快照集合。 @return 历史配置及其原路径锚点。
    """
    original = resolve_configuration(manifest["identity_sha256"], sources)
    origin = Path(manifest["evidence_directory"])
    if origin.parent.name != "captures":
        return original
    if origin.name != manifest["sample_id"]:
        fail("Published capture origin does not match its sample identity")
    run_path = origin.parent.parent / "configuration.json"
    for key, records in sources.items():
        for record in records:
            if record["path"] == str(run_path.resolve()):
                verify_saved_project(record)
                if key != manifest["identity_sha256"]:
                    return finalized_capture_configuration(manifest, identity, sources, record["configuration"])
                return record["configuration"]
    fail("Original capture run configuration is missing")


def verify_sample_contract(facts: dict, manifest: dict, configuration: dict) -> None:
    """@brief 以完整采集事实核对画布、任务、隐私和观察要求，与交付选择无关。
    @param facts 已核验事实。 @param manifest 原登记清单。 @param configuration 原运行快照。
    """
    project, wms = configuration["project"], facts["windows"]
    matches = [task for task in project["tasks"] if task["name"] == manifest["task_name"]]
    if len(matches) != 1:
        fail("Published sample task is not present in its saved run")
    task = matches[0]
    if wms["canvas"] != project["dataset"]["canvas"]:
        fail("Published sample canvas differs from its saved project")
    if any(manifest["publication_sample"].get(key) != task[key] for key in ("app", "scene")):
        fail("Published sample app or scene differs from its saved task")
    bundle = manifest["target_bundle_name"]
    if "bundle_names" in task and bundle not in task["bundle_names"]:
        fail("Published sample target bundle differs from its saved task")
    if bundle not in {window["bundle_name"] for window in wms["app_windows"]}:
        fail("Published sample target bundle is absent from its application windows")
    receipt, policy = wms["privacy"], task["privacy"]
    verify_privacy_receipt(receipt)
    policy_sha256 = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
    if receipt.get("policy_sha256") != policy_sha256 or receipt["enabled"] != policy["enabled"]:
        fail("Published privacy receipt differs from its saved task policy")
    if receipt["after_sha256"] != facts["screenshot"]["sha256"]:
        fail("Published privacy receipt does not match its paired screenshot")
    stability = wms["stability"]
    if stability.get("status") != "verified" or stability.get("rules") != project["dataset"]["stability_regions"]:
        fail("Published stability rules differ from its saved project")
