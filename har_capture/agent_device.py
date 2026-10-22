"""@brief 为界面代理提供显式白名单内的设备查询、应用启动与原始证据采集。"""

import json
import re
from pathlib import Path

from har_capture.collector import capture
from har_capture.hdc import Hdc, fail, utc_now
from har_capture.metadata import parse_windows
from har_capture.sessions import SESSION_BLOCK, parse_sessions, pipe_values
from har_capture.window_control import window_size_control
from har_capture.window_title_bar import window_title_bar

OPERATIONS = {
    "read_windows": {"display_id"},
    "list_applications": {"query", "offset", "limit"},
    "launch_application": {"bundle_name", "ability_name"},
    "capture": {"output_dir", "display_id"},
    "window_size_control": {"window_id"},
    "window_title_bar": {"window_id"},
}
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")
ABILITY_HEADER = "callingBundleName bundleName moduleName abilityName"


def validate_request(request: object) -> str:
    """@brief 校验操作白名单与字段。 @param request 请求对象。 @return 操作名。"""
    if not isinstance(request, dict):
        fail("Device request must be a JSON object")
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        fail("Unsupported device operation")
    unknown = set(request) - OPERATIONS[operation] - {"operation"}
    if unknown:
        fail(f"Unexpected parameters for {operation}: {', '.join(sorted(unknown))}")
    return operation


def integer_parameter(request: dict, name: str, default: int, maximum: int | None = None) -> int:
    """@brief 校验非负整数参数。 @param request 请求。 @param name 字段名。
    @param default 默认值。 @param maximum 可选上界。 @return 参数值。
    """
    value = request.get(name, default)
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        fail(f"Invalid integer parameter: {name}")
    return value


def identifier(value: object, name: str) -> str:
    """@brief 限定传给设备命令的标识符字符。 @param value 原值。 @param name 字段名。 @return 标识符。"""
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        fail(f"Invalid identifier: {name}")
    return value


def parse_applications(text: str) -> list[dict]:
    """@brief 解析包管理器按用户列出的安装包。 @param text 原始输出。 @return 应用列表。"""
    applications, seen, user_id = [], set(), None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        header = re.fullmatch(r"ID: (\d+):", line)
        if header:
            user_id = int(header[1])
            continue
        if user_id is None:
            fail("Bundle inventory is missing its user header")
        bundle = identifier(line, "bundle_name")
        key = (user_id, bundle)
        if key in seen:
            fail(f"Duplicate installed bundle: {bundle}")
        seen.add(key)
        applications.append({"bundle_name": bundle, "user_id": user_id})
    if user_id is None:
        fail("Bundle manager did not return an application inventory")
    return sorted(applications, key=lambda item: (item["bundle_name"], item["user_id"]))


def bundle_details(hdc: Hdc, bundle: str) -> dict:
    """@brief 查询并验证指定安装包元数据。 @param hdc 连接。 @param bundle 包名。 @return 包元数据。"""
    identifier(bundle, "bundle_name")
    text = hdc.run(["shell", "bm", "dump", "-n", bundle], "bundle_details")
    header, separator, body = text.strip().partition("\n")
    if not separator or header.strip() != bundle + ":":
        fail(f"Bundle manager did not return metadata for {bundle}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        fail(f"Invalid bundle metadata JSON: {error.msg}")
    if not isinstance(data, dict) or data.get("name") != bundle:
        fail(f"Bundle metadata identity does not match {bundle}")
    application = data.get("applicationInfo")
    if not isinstance(application, dict) or application.get("bundleName") != bundle:
        fail(f"Application metadata identity does not match {bundle}")
    return data


def section_row(lines: list[str], name: str) -> str:
    """@brief 读取带独立数据行的唯一会话段。 @param lines 文本行。 @param name 段名。 @return 数据行。"""
    positions = [index for index, line in enumerate(lines) if line == name + ":"]
    if len(positions) != 1 or positions[0] + 1 >= len(lines):
        fail(f"Session details require one {name} section")
    return lines[positions[0] + 1]


def session_identities(text: str) -> dict[int, dict]:
    """@brief 读取包名、调用者与焦点，保留系统场景原名。 @param text 会话原文。 @return 窗口身份索引。"""
    blocks = list(SESSION_BLOCK.finditer(text))
    if not blocks:
        fail("Session details contain no identity blocks")
    header = [line.strip() for line in text[:blocks[0].start()].splitlines()]
    if section_row(header, "Ability") != ABILITY_HEADER:
        fail("Unsupported Ability header in session details")
    result = {}
    for index, block in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(text)
        lines = [line.strip() for line in text[block.end():end].splitlines()]
        ability = pipe_values(section_row(lines, "Ability"), 4, "Ability")
        session = pipe_values(section_row(lines, "Session"), 9, "Session")
        focus = [line.split(":", 1)[1].strip() for line in lines if line.startswith("Focus:")]
        if len(focus) != 1:
            fail("Session details require one Focus section")
        focused = pipe_values(focus[0], 4, "Focus")[0]
        if focused not in ("0", "1") or not session[1].isdecimal():
            fail("Invalid focus or calling UID in session details")
        result[int(block["id"])] = {"session_bundle_name": ability[1], "calling_uid": int(session[1]),
                                     "focused": focused == "1"}
    return result


def system_bundle(hdc: Hdc, session: dict, identity: dict) -> str:
    """@brief 用进程与安装包 UID 证明系统窗口归属。 @param hdc 连接。
    @param session 会话。 @param identity 调用者信息。 @return 验证后的包名。
    """
    process_id = session["process_id"]
    output = hdc.run(["shell", "cat", f"/proc/{process_id}/cmdline"], "system_process_identity")
    process = output.rstrip("\x00\r\n")
    bundle = identifier(process, "system_process_bundle")
    application = bundle_details(hdc, bundle)["applicationInfo"]
    if application.get("process") != process or application.get("uid") != identity["calling_uid"]:
        fail(f"System process ownership does not match window {session['window_id']}")
    return bundle


def read_windows(hdc: Hdc, request: dict) -> dict:
    """@brief 读取有前台层级的窗口及可验证的包归属。 @param hdc 连接。 @param request 请求。 @return 窗口清单。"""
    display_id = integer_parameter(request, "display_id", 0)
    started = utc_now()
    inventory = parse_windows(hdc.run(["shell", "hidumper", "-s", "WindowManagerService", "-a", "-a"], "windows"))
    text = hdc.run(["shell", "hidumper", "-s", "WindowManagerService", "-a", "-c"], "session_details")
    sessions, identities = parse_sessions(text), session_identities(text)
    windows, system_bundles = [], {}
    for window in inventory.values():
        if window["display_id"] != display_id or window["z_order"] < 0:
            continue
        window_id = window["window_id"]
        if window_id not in sessions or window_id not in identities:
            fail(f"Window disappeared from session details: {window_id}")
        session, identity = sessions[window_id], identities[window_id]
        if any(window[key] != session[key] for key in ("process_id", "display_id")):
            fail(f"Window identity changed between queries: {window_id}")
        if session["is_system"]:
            key = (session["process_id"], identity["calling_uid"])
            if key not in system_bundles:
                system_bundles[key] = system_bundle(hdc, session, identity)
            bundle, source = system_bundles[key], "proc_cmdline_and_bundle_manager"
        else:
            bundle = identifier(identity["session_bundle_name"], "session_bundle_name")
            source = "WindowManagerService.Session.Ability.bundleName"
        windows.append({**window, **session, **identity, "bundle_name": bundle, "bundle_name_source": source})
    return {"started_at": started, "finished_at": utc_now(), "display_id": display_id,
            "windows": sorted(windows, key=lambda window: window["z_order"], reverse=True)}


def list_applications(hdc: Hdc, request: dict) -> dict:
    """@brief 筛选并分页返回真实安装包。 @param hdc 连接。 @param request 请求。 @return 应用页。"""
    offset = integer_parameter(request, "offset", 0)
    limit = integer_parameter(request, "limit", 50, 100)
    query = request.get("query", "")
    if not isinstance(query, str) or limit == 0:
        fail("Application query must be a string and limit must be positive")
    applications = parse_applications(hdc.run(["shell", "bm", "dump", "-a"], "list_applications"))
    selected = [item for item in applications if query.casefold() in item["bundle_name"].casefold()]
    page = selected[offset:offset + limit]
    next_offset = offset + len(page) if offset + len(page) < len(selected) else None
    return {"applications": page, "total": len(selected), "offset": offset,
            "limit": limit, "next_offset": next_offset}


def launcher_skill(ability: dict) -> bool:
    """@brief 识别应用声明的桌面启动技能。 @param ability 能力元数据。 @return 是否含桌面入口。"""
    skills = ability.get("skills")
    if not isinstance(skills, list):
        fail("Ability metadata is missing its skills list")
    return any(isinstance(skill, dict) and "action.system.home" in skill.get("actions", [])
               and "entity.system.home" in skill.get("entities", []) for skill in skills)


def launch_target(data: dict, ability_name: str | None) -> dict:
    """@brief 唯一选择真实存在的可见界面入口。 @param data 包元数据。
    @param ability_name 可选显式能力名。 @return 启动目标。
    """
    if data["applicationInfo"].get("enabled") is not True:
        fail("Application is disabled")
    modules = data.get("hapModuleInfos")
    if not isinstance(modules, list):
        fail("Bundle metadata is missing its module list")
    targets = []
    for module in modules:
        if not isinstance(module, dict) or not isinstance(module.get("abilityInfos"), list):
            fail("Module metadata is missing its ability list")
        for ability in module["abilityInfos"]:
            if not isinstance(ability, dict):
                fail("Invalid ability metadata")
            if ability.get("enabled") is not True or ability.get("visible") is not True or ability.get("type") != 1:
                continue
            selected = ability.get("name") == ability_name if ability_name is not None else launcher_skill(ability)
            if not selected:
                continue
            if ability.get("bundleName") != data["name"] or ability.get("moduleName") != module.get("moduleName"):
                fail("Ability metadata does not match its bundle and module")
            targets.append({"bundle_name": data["name"],
                            "module_name": identifier(ability.get("moduleName"), "module_name"),
                            "ability_name": identifier(ability.get("name"), "ability_name")})
    if len(targets) != 1:
        fail(f"Expected one launchable ability; found {len(targets)}. Specify an unambiguous ability_name.")
    return targets[0]


def launch_application(hdc: Hdc, request: dict) -> dict:
    """@brief 解析真实能力后启动应用，并验证系统返回。 @param hdc 连接。 @param request 请求。 @return 启动目标。"""
    bundle = identifier(request.get("bundle_name"), "bundle_name")
    ability = identifier(request["ability_name"], "ability_name") if "ability_name" in request else None
    target = launch_target(bundle_details(hdc, bundle), ability)
    output = hdc.run(["shell", "aa", "start", "-b", bundle, "-m", target["module_name"],
                      "-a", target["ability_name"]], "launch_application")
    if "start ability successfully." not in [line.strip() for line in output.splitlines()]:
        fail("Ability manager did not confirm application launch")
    return {**target, "launched": True}


def capture_frame(hdc: Hdc, request: dict) -> dict:
    """@brief 复用原始采集并返回完整窗口索引。 @param hdc 连接。 @param request 请求。 @return 产物位置及摘要。"""
    output = request.get("output_dir")
    if not isinstance(output, str) or not output.strip():
        fail("Capture requires a nonempty output_dir")
    display = integer_parameter(request, "display_id", 0)
    destination = capture(hdc, Path(output), display)
    metadata_path = destination / "metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    keys = ("capture_id", "started_at", "finished_at", "display", "windows")
    return {"path": str(destination), "metadata_path": str(metadata_path), **{key: metadata[key] for key in keys}}


def execute_operation(hdc: Hdc, request: object) -> dict:
    """@brief 派发单次白名单操作，禁止任意命令。 @param hdc 连接。 @param request 请求。 @return 完整操作结果。"""
    operation = validate_request(request)
    handlers = {"read_windows": read_windows, "list_applications": list_applications,
                "launch_application": launch_application, "capture": capture_frame,
                "window_size_control": window_size_control, "window_title_bar": window_title_bar}
    if operation != "capture":
        hdc.connect()
    return {"operation": operation, "result": handlers[operation](hdc, request)}
