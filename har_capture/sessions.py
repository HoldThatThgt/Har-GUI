"""@brief 解析窗口管理器会话详情中的系统会话标志及窗口关联字段。"""

import re

from har_capture.hdc import fail

SESSION_BLOCK = re.compile(r"^-{3,}(?P<name>.+?)\|(?P<id>\d+)-{3,}\s*$", re.MULTILINE)
SESSION_HEADER = "callingPid callingUid isSystem reuse lockedState time type isSystemCalling topmost".split()
LIFE_HEADER = (
    "state isActive isSystemActive isVisible uiNodeId showRecent bufferAvailable "
    "foregroundInteractiveStatus isAttach isPersistentRecover"
).split()
DISPLAY_HEADER = "snapshotScale vpr screenId brightness displayId".split()


def section_value(lines: list[str], section: str) -> str:
    """@brief 读取唯一必需段。 @param lines 原始行。 @param section 段名。 @return 段数据。"""
    matches = [index for index, line in enumerate(lines) if line.startswith(section + ":")]
    if len(matches) != 1:
        fail(f"Session detail requires exactly one {section} section; found {len(matches)}")
    index = matches[0]
    if section == "Session":
        if lines[index] != "Session:" or index + 1 >= len(lines):
            fail("Malformed Session section in session details")
        return lines[index + 1]
    return lines[index].split(":", 1)[1].strip()


def validate_headers(text: str) -> None:
    """@brief 校验已验证的字段顺序。 @param text 首个会话块之前的格式说明。"""
    lines = [line.strip() for line in text.splitlines()]
    if not any(line.strip("-") == "SessionDetailedInfo" for line in lines):
        fail("Missing SessionDetailedInfo header")
    expected = {"Session": SESSION_HEADER, "Life": LIFE_HEADER, "Display": DISPLAY_HEADER}
    for section, columns in expected.items():
        if section_value(lines, section).split() != columns:
            fail(f"Unsupported {section} header in session details")


def pipe_values(text: str, count: int, section: str) -> list[str]:
    """@brief 严格拆分字段。 @param text 段数据。 @param count 字段数量。 @param section 段名。 @return 值。"""
    if not text.endswith("|"):
        fail(f"Missing trailing delimiter in {section} session details")
    values = text[:-1].split("|")
    if len(values) != count:
        fail(f"Expected {count} {section} fields; found {len(values)}")
    return values


def integer_value(value: str, field: str, signed: bool = False) -> int:
    """@brief 校验整数字段。 @param value 原值。 @param field 字段名。 @param signed 可否为负。 @return 整数。"""
    pattern = r"-?\d+" if signed else r"\d+"
    if not re.fullmatch(pattern, value):
        fail(f"Invalid {field} in session details: {value!r}")
    return int(value)


def parse_session(text: str, window_id: int) -> dict:
    """@brief 解析单个会话块。 @param text 会话内容。 @param window_id 窗口标识。 @return 关联信息。"""
    lines = [line.strip() for line in text.splitlines()]
    session = pipe_values(section_value(lines, "Session"), len(SESSION_HEADER), "Session")
    life = pipe_values(section_value(lines, "Life"), len(LIFE_HEADER), "Life")
    display = pipe_values(section_value(lines, "Display"), len(DISPLAY_HEADER), "Display")
    if session[2] not in ("0", "1"):
        fail(f"Invalid isSystem in session details for window {window_id}: {session[2]!r}")
    return {
        "window_id": window_id,
        "process_id": integer_value(session[0], "callingPid"),
        "is_system": session[2] == "1",
        "ui_node_id": integer_value(life[4], "uiNodeId", signed=True),
        "display_id": integer_value(display[4], "displayId"),
    }


def parse_sessions(text: str) -> dict[int, dict]:
    """@brief 解析完整会话详情。 @param text WMS 全局 -c 原文。 @return 按窗口标识索引的会话。"""
    matches = list(SESSION_BLOCK.finditer(text))
    if not matches:
        fail("Session details contain no session blocks")
    validate_headers(text[:matches[0].start()])
    sessions = {}
    for index, match in enumerate(matches):
        window_id = int(match["id"])
        if window_id in sessions:
            fail(f"Duplicate window ID in session details: {window_id}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end]
        if re.search(r"^-{3,}.*\|.*-{3,}\s*$", body, re.MULTILINE):
            fail(f"Malformed session block after window {window_id}")
        sessions[window_id] = parse_session(body, window_id)
    return sessions
