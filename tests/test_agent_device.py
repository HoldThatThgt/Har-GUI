"""验证代理设备入口的真实格式解析、操作白名单与启动前校验。"""

import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import agent_device
from har_capture.agent_device import (
    bundle_details,
    capture_frame,
    execute_operation,
    launch_application,
    launch_target,
    list_applications,
    parse_applications,
    read_windows,
)
from har_capture.hdc import CaptureError, Hdc
from tests.support import FIXTURES


def bundle_fixture(bundle="com.example.notes"):
    """构造具有真实桌面技能的匿名能力描述，能力名称故意不是 EntryAbility。"""
    ability = {"bundleName": bundle, "moduleName": "pc", "name": "WorkspaceAbility", "type": 1,
               "enabled": True, "visible": True,
               "skills": [{"actions": ["action.system.home"], "entities": ["entity.system.home"]}]}
    return {"name": bundle, "applicationInfo": {"bundleName": bundle, "process": bundle,
                                                 "uid": 10000, "enabled": True},
            "hapModuleInfos": [{"moduleName": "pc", "abilityInfos": [ability]}]}


def session_fixture():
    """构造与匿名窗口列表对应的系统及应用会话。"""
    header = ("---SessionDetailedInfo---\nSession:\n"
              "callingPid callingUid isSystem reuse lockedState time type isSystemCalling topmost\n"
              "Ability:\ncallingBundleName bundleName moduleName abilityName\n"
              "Life: state isActive isSystemActive isVisible uiNodeId showRecent bufferAvailable "
              "foregroundInteractiveStatus isAttach isPersistentRecover\n"
              "Display: snapshotScale vpr screenId brightness displayId\n")
    for window_id, pid, system, bundle in ((9, 200, 1, "ExampleDock"), (80, 201, 0, "com.example.reader"),
                                           (92, 202, 0, "com.example.settings")):
        header += (f"---Window|{window_id}---\nSession:\n{pid}|10000|{system}|0|0||1|0|0|\n"
                   f"Ability:\nunknown|{bundle}|pc|WorkspaceAbility|\n"
                   f"Life: 3|1|0|1|{window_id * 10}|0|1|1|1|0|\nDisplay: 0.5|2|0|-1|0|\n"
                   f"Focus: {int(window_id == 92)}|0|1|1|\n")
    return header


def device_fixture(session=None, system_data=None):
    """按操作名提供只读设备响应。"""
    hdc = Mock(spec=Hdc)
    data = system_data or bundle_fixture("com.example.sceneboard")
    outputs = {"windows": (FIXTURES / "windows.txt").read_text(encoding="utf-8"),
               "session_details": session or session_fixture(),
               "system_process_identity": "com.example.sceneboard\x00\n",
               "bundle_details": "com.example.sceneboard:\n" + json.dumps(data)}
    hdc.run.side_effect = lambda arguments, name: outputs[name]
    return hdc


class AgentDeviceTests(unittest.TestCase):
    """覆盖需要真实证据支持的应用与窗口信息。"""

    def test_read_windows_joins_real_geometry_and_proven_system_bundle(self):
        hdc = device_fixture()
        result = read_windows(hdc, {})
        dock, settings, reader = result["windows"]
        self.assertEqual([dock["window_id"], settings["window_id"], reader["window_id"]], [9, 92, 80])
        self.assertEqual(dock["bundle_name"], "com.example.sceneboard")
        self.assertEqual(dock["session_bundle_name"], "ExampleDock")
        self.assertEqual(dock["bundle_name_source"], "proc_cmdline_and_bundle_manager")
        self.assertEqual(dock["bounding_box"], {"x": 0, "y": 1955, "width": 3120, "height": 125})
        self.assertEqual(dock["z_order"], 902)
        self.assertTrue(dock["is_system"])
        self.assertTrue(settings["focused"])
        self.assertEqual(settings["bundle_name"], "com.example.settings")
        self.assertLessEqual(result["started_at"], result["finished_at"])

    def test_read_windows_rejects_missing_session_and_changed_owner(self):
        for text in (session_fixture().replace("---Window|92---", "---Window|93---"),
                     session_fixture().replace("202|10000|0|", "999|10000|0|")):
            with self.subTest(text=text[-200:]), self.assertRaises(CaptureError):
                read_windows(device_fixture(session=text), {})

    def test_read_windows_rejects_unproven_system_process_ownership(self):
        for field, value in (("uid", 99999), ("process", "com.example.other")):
            data = bundle_fixture("com.example.sceneboard")
            data["applicationInfo"][field] = value
            with self.subTest(field=field), self.assertRaises(CaptureError):
                read_windows(device_fixture(system_data=data), {})

    def test_read_windows_rejects_changed_ability_header(self):
        text = session_fixture().replace("callingBundleName bundleName", "bundleName callingBundleName", 1)
        with self.assertRaises(CaptureError):
            read_windows(device_fixture(session=text), {})

    def test_bundle_inventory_keeps_users_and_supports_query_pagination(self):
        hdc = Mock(spec=Hdc)
        hdc.run.return_value = "ID: 100:\n\tcom.example.notes\n\tcom.example.browser\nID: 101:\ncom.example.notes\n"
        page = list_applications(hdc, {"query": "NOTES", "limit": 1})
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["applications"], [{"bundle_name": "com.example.notes", "user_id": 100}])
        self.assertEqual(page["next_offset"], 1)
        final = list_applications(hdc, {"query": "notes", "limit": 1, "offset": 1})
        self.assertEqual(final["applications"][0]["user_id"], 101)
        self.assertIsNone(final["next_offset"])

    def test_bundle_inventory_rejects_errors_duplicates_and_shell_text(self):
        for text in ("error: permission denied", "com.example.notes", "ID: 100:\na; touch /data/foo",
                     "ID: 100:\ncom.example.notes\ncom.example.notes\n"):
            with self.subTest(text=text), self.assertRaises(CaptureError):
                parse_applications(text)

    def test_pagination_rejects_invalid_numbers(self):
        for request in ({"offset": True}, {"offset": -1}, {"limit": 0}, {"limit": 101}, {"query": []}):
            with self.subTest(request=request), self.assertRaises(CaptureError):
                list_applications(Mock(spec=Hdc), request)

    def test_bundle_details_requires_matching_prefix_and_application_identity(self):
        valid = bundle_fixture()
        wrong = copy.deepcopy(valid)
        wrong["applicationInfo"]["bundleName"] = "com.example.other"
        for output in ("com.example.other:\n" + json.dumps(valid), "com.example.notes:\ninvalid",
                       "com.example.notes:\n" + json.dumps(wrong)):
            hdc = Mock(spec=Hdc)
            hdc.run.return_value = output
            with self.subTest(output=output[:70]), self.assertRaises(CaptureError):
                bundle_details(hdc, "com.example.notes")

    def test_launch_uses_discovered_ability_and_module(self):
        hdc = Mock(spec=Hdc)
        hdc.run.side_effect = ["com.example.notes:\n" + json.dumps(bundle_fixture()),
                               "start ability successfully.\n"]
        result = launch_application(hdc, {"bundle_name": "com.example.notes"})
        self.assertEqual(result, {"bundle_name": "com.example.notes", "module_name": "pc",
                                  "ability_name": "WorkspaceAbility", "launched": True})
        self.assertEqual(hdc.run.call_args.args[0], ["shell", "aa", "start", "-b", "com.example.notes",
                                                   "-m", "pc", "-a", "WorkspaceAbility"])

    def test_ambiguous_launcher_requires_explicit_existing_ability(self):
        data = bundle_fixture()
        second = copy.deepcopy(data["hapModuleInfos"][0]["abilityInfos"][0])
        second["name"] = "SecondaryAbility"
        data["hapModuleInfos"][0]["abilityInfos"].append(second)
        with self.assertRaises(CaptureError):
            launch_target(data, None)
        self.assertEqual(launch_target(data, "SecondaryAbility")["ability_name"], "SecondaryAbility")
        with self.assertRaises(CaptureError):
            launch_target(data, "MissingAbility")

    def test_launch_rejects_missing_home_skill_disabled_or_hidden_ability(self):
        for field, value in (("skills", []), ("enabled", False), ("visible", False), ("type", 2)):
            data = bundle_fixture()
            data["hapModuleInfos"][0]["abilityInfos"][0][field] = value
            with self.subTest(field=field), self.assertRaises(CaptureError):
                launch_target(data, None)

    def test_launch_requires_ability_manager_confirmation(self):
        hdc = Mock(spec=Hdc)
        hdc.run.side_effect = ["com.example.notes:\n" + json.dumps(bundle_fixture()), "error: launch denied\n"]
        with self.assertRaises(CaptureError):
            launch_application(hdc, {"bundle_name": "com.example.notes"})

    def test_dispatch_rejects_unknown_operations_and_injected_parameters_before_connecting(self):
        for request in ({"operation": "shell", "command": "echo unsafe"}, [],
                        {"operation": "read_windows", "command": "echo unsafe"}):
            hdc = Mock(spec=Hdc)
            with self.subTest(request=request), self.assertRaises(CaptureError):
                execute_operation(hdc, request)
            hdc.connect.assert_not_called()
            hdc.run.assert_not_called()

    def test_launch_identifier_injection_never_reaches_hdc(self):
        hdc = Mock(spec=Hdc)
        with self.assertRaises(CaptureError):
            launch_application(hdc, {"bundle_name": "com.example.notes; touch /data/foo"})
        hdc.run.assert_not_called()

    def test_capture_reuses_collector_and_returns_full_window_indices(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            metadata = {"capture_id": "sample", "started_at": "start", "finished_at": "end",
                        "display": {"id": 0}, "windows": [{"window_id": 92, "trees": {"arkui": {"file": "tree"}}}]}
            (path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
            hdc = Mock(spec=Hdc)
            with patch("har_capture.agent_device.capture", return_value=path) as collector:
                response = capture_frame(hdc, {"output_dir": temporary})
            collector.assert_called_once_with(hdc, path, 0)
            self.assertEqual(response["windows"], metadata["windows"])
            self.assertEqual(response["metadata_path"], str(path / "metadata.json"))

    def test_cli_writes_one_json_result_and_a_nonzero_failure(self):
        for request, expected_code in (({"operation": "list_applications"}, 0), ({"operation": "shell"}, 1)):
            hdc = Mock(spec=Hdc)
            hdc.run.return_value = "ID: 100:\ncom.example.notes\n"
            output = io.StringIO()
            with patch.object(agent_device, "arguments", return_value=Mock(hdc=Path("hdc"), device="serial")), \
                    patch.object(agent_device, "Hdc", return_value=hdc), \
                    patch("sys.stdin", io.StringIO(json.dumps(request))), patch("sys.stdout", output):
                self.assertEqual(agent_device.main(), expected_code)
            response = json.loads(output.getvalue())
            self.assertEqual(response["operation"], request["operation"])
            self.assertIn("error" if expected_code else "result", response)


if __name__ == "__main__":
    unittest.main()
