"""@brief 验证会话详情中的字段顺序与明确的系统会话标志。"""

import unittest

from har_capture.hdc import CaptureError
from har_capture.sessions import parse_sessions

HEADER = """---SessionDetailedInfo---
Session:
callingPid callingUid isSystem reuse lockedState time type isSystemCalling topmost
Life: state isActive isSystemActive isVisible uiNodeId showRecent bufferAvailable foregroundInteractiveStatus isAttach isPersistentRecover
Display: snapshotScale vpr screenId brightness displayId
"""


def session_block(window_id=9, is_system="1", window_type="2116"):
    """@brief 构造匿名会话块。"""
    return (
        f"---Anonymous|{window_id}---\n"
        "Session:\n"
        f"123|456|{is_system}|0|0||{window_type}|0|0|\n"
        "0|0|0|0|0|1|0|127|1|\n"
        "Life: 0|0|1|1|789|0|0|1|0|0|\n"
        "Display: 0.5|2|0|-1|4|\n"
    )


class SessionParsingTests(unittest.TestCase):
    """@brief 用匿名样本验证成功结果与错误拒绝。"""

    def test_explicit_fields_and_duplicate_names(self):
        sessions = parse_sessions(HEADER + session_block() + session_block(10, "0", "1"))
        self.assertEqual(sessions[9], {
            "window_id": 9, "process_id": 123, "is_system": True, "ui_node_id": 789, "display_id": 4,
        })
        self.assertFalse(sessions[10]["is_system"])

    def test_window_type_does_not_determine_system_flag(self):
        sessions = parse_sessions(HEADER + session_block(9, "0", "2116") + session_block(10, "1", "1"))
        self.assertFalse(sessions[9]["is_system"])
        self.assertTrue(sessions[10]["is_system"])

    def test_signed_ui_node_sentinel_and_crlf(self):
        text = (HEADER + session_block()).replace("|789|", "|-1|").replace("\n", "\r\n")
        self.assertEqual(parse_sessions(text)[9]["ui_node_id"], -1)

    def test_missing_and_duplicate_sections_are_rejected(self):
        body = session_block()
        cases = [
            body.replace("Session:\n", ""),
            body.replace("Life: 0|0|1|1|789|0|0|1|0|0|\n", ""),
            body.replace("Display: 0.5|2|0|-1|4|\n", ""),
            body + "Display: 0.5|2|0|-1|4|\n",
            body + "Session:\n123|456|1|0|0||2116|0|0|\n",
        ]
        for body in cases:
            with self.subTest(body=body), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                parse_sessions(HEADER + body)

    def test_incompatible_headers_are_rejected(self):
        cases = [
            HEADER.replace("SessionDetailedInfo", "SessionSummary"),
            HEADER.replace("callingPid callingUid", "callingUid callingPid"),
            HEADER.replace("isVisible uiNodeId", "uiNodeId isVisible"),
            HEADER.replace("brightness displayId", "displayId brightness"),
            HEADER.replace("Display:", "Other:"),
        ]
        for header in cases:
            with self.subTest(header=header), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                parse_sessions(header + session_block())

    def test_invalid_records_are_rejected(self):
        body = session_block()
        cases = [
            "",
            HEADER,
            HEADER + body + body,
            HEADER + session_block(is_system="true"),
            HEADER + body.replace("123|456|", "abc|456|"),
            HEADER + body.replace("|789|", "|1.2|"),
            HEADER + body.replace("Display: 0.5|2|0|-1|4|", "Display: 0.5|2|0|-1|-4|"),
            HEADER + body.replace("Display: 0.5|2|0|-1|4|", "Display: 0.5|2|0|-1|4"),
            HEADER + body.replace("Display: 0.5|2|0|-1|4|", "Display: 0.5|2|0|-1|4|5|"),
            HEADER + body + "---Other|broken---\n",
        ]
        for text in cases:
            with self.subTest(text=text), self.assertLogs(level="ERROR"), self.assertRaises(CaptureError):
                parse_sessions(text)


if __name__ == "__main__":
    unittest.main()
