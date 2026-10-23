"""验证设备先完成两种独立转储，再接收文件，失败仍保留清理登记。"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from har_capture.geometry_capture import collect_window_geometry
from har_capture.hdc import CaptureError, cleanup_remote
from har_capture.tree_capture import collect_arkui
from tests.support import fake_hdc, native_arkui, native_system_arkui, raw_arkui


class CaptureSourceTimingTests(unittest.TestCase):
    """只用 HDC 替身验证采集时序与原有验收约束。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.stage = Path(self.temporary.name)
        (self.stage / "arkui").mkdir()
        self.remote = {"layout": "/data/local/tmp/source-timing.json"}
        self.window = {"window_id": 92, "process_id": 202, "bundle_name": "com.example.settings"}

    def collect(self, hdc):
        return collect_arkui(hdc, self.stage, self.remote, self.window)

    def assert_no_accepted_tree(self):
        self.assertFalse((self.stage / "arkui/window-92.json").exists())
        self.assertFalse((self.stage / "arkui/window-92-forest.json").exists())

    def assert_registered_cleanup(self, hdc, keys):
        self.assertEqual(set(self.remote), {"layout", *keys})
        self.assertTrue(cleanup_remote(hdc, self.remote)["success"])
        arguments, name = hdc.run.call_args.args
        self.assertEqual(name, "cleanup")
        self.assertEqual(arguments[3:-3], list(self.remote.values()))

    def test_both_device_dumps_finish_before_any_receive_with_separate_command_records(self):
        hdc = fake_hdc()
        receive = hdc.receive.side_effect

        def checked_receive(remote, local, name):
            self.assertEqual([record["name"] for record in hdc.records[:2]],
                             ["dump_arkui_92", "geometry_92"])
            self.assertEqual(set(self.remote), {"layout", "arkui_92", "geometry_92"})
            receive(remote, local, name)

        hdc.receive.side_effect = checked_receive
        self.collect(hdc)
        self.assertEqual([record["name"] for record in hdc.records],
                         ["dump_arkui_92", "geometry_92", "receive_arkui_response_92", "receive_geometry_92"])
        self.assertIn("-w 92 -default -all", hdc.records[0]["command"][1])
        self.assertIn("-w 92 -allInfoWithParamConfig", hdc.records[1]["command"][1])
        for record in hdc.records:
            self.assertTrue({"started_at", "finished_at", "duration_seconds"} <= record.keys())

    def test_original_responses_digests_and_unselected_forest_survive_new_order(self):
        raw = raw_arkui() + b"|-> Detached,9000,1 childSize:0\r\n      ID: 9000\r\n"
        native = native_arkui()
        evidence = self.collect(fake_hdc({"arkui_response_92": raw, "geometry_92": native}))
        for reference, expected in ((evidence["response"], raw), (evidence["raw"], raw),
                                    (evidence["coordinate_evidence"]["response"], native)):
            self.assertEqual((self.stage / reference["file"]).read_bytes(), expected)
            self.assertEqual(reference["sha256"], hashlib.sha256(expected).hexdigest())
        forest = json.loads((self.stage / evidence["forest"]["file"]).read_bytes())
        self.assertEqual([node["attributes"]["ID"] for node in forest], ["0", "9000"])
        self.assertEqual(evidence["forest_node_count"], 3)
        self.assertEqual(evidence["node_count"], 2)

    def test_each_response_still_requires_its_own_window_and_process_identity(self):
        cases = [("arkui_response_92", raw_arkui(window_id=80)),
                 ("arkui_response_92", raw_arkui(process_id=201)),
                 ("geometry_92", native_arkui(window_id=80)),
                 ("geometry_92", native_arkui(process_id=201))]
        for key, payload in cases:
            with self.subTest(source=key, payload=payload), tempfile.TemporaryDirectory() as temporary:
                self.stage = Path(temporary)
                (self.stage / "arkui").mkdir()
                hdc = fake_hdc({key: payload})
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "response does not match"):
                    self.collect(hdc)
                self.assertEqual([record["name"] for record in hdc.records[:2]],
                                 ["dump_arkui_92", "geometry_92"])
                self.assert_no_accepted_tree()
                self.assert_registered_cleanup(hdc, {"arkui_92", "geometry_92"})

    def test_native_application_identity_is_independent_of_valid_response_header(self):
        payloads = [native_arkui(bundle="com.example.other"),
                    native_arkui().replace(b'"WindowID": 92', b'"WindowID": 80')]
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                self.stage = Path(temporary)
                (self.stage / "arkui").mkdir()
                with self.assertLogs(level="ERROR"), self.assertRaisesRegex(CaptureError, "application identity"):
                    self.collect(fake_hdc({"geometry_92": payload}))
                self.assert_no_accepted_tree()
                self.assertFalse((self.stage / "arkui/window-92-geometry.json").exists())

    def test_dump_failure_stops_before_receive_and_keeps_every_started_path_for_cleanup(self):
        for name in ("dump_arkui_92", "geometry_92"):
            for missing_marker in (False, True):
                with self.subTest(name=name, missing_marker=missing_marker):
                    self.remote = {"layout": "/data/local/tmp/source-timing.json"}
                    hdc = (fake_hdc(command_outputs={name: "No response"}) if missing_marker
                           else fake_hdc(failure=name))
                    with self.assertRaises(CaptureError):
                        self.collect(hdc)
                    hdc.receive.assert_not_called()
                    self.assert_no_accepted_tree()
                    keys = {"arkui_92"} if name == "dump_arkui_92" else {"arkui_92", "geometry_92"}
                    self.assert_registered_cleanup(hdc, keys)

    def test_transfer_failure_preserves_both_registered_paths_without_acceptance(self):
        for name in ("receive_arkui_response_92", "receive_geometry_92"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                self.stage = Path(temporary)
                (self.stage / "arkui").mkdir()
                hdc = fake_hdc(failure=name)
                with self.assertRaises(CaptureError):
                    self.collect(hdc)
                self.assert_no_accepted_tree()
                self.assert_registered_cleanup(hdc, {"arkui_92", "geometry_92"})

    def test_system_geometry_retains_its_existing_single_dump_then_receive_strategy(self):
        hdc = fake_hdc({"geometry_92": native_system_arkui()})
        self.window["is_system"] = True
        evidence = collect_window_geometry(hdc, self.stage, self.remote, self.window)
        self.assertEqual([record["name"] for record in hdc.records], ["geometry_92", "receive_geometry_92"])
        self.assertEqual(evidence["source_window_id"], 92)
        self.assertEqual(evidence["process_id"], 202)
        geometry = json.loads((self.stage / evidence["arkui_geometry"]["file"]).read_bytes())
        self.assertEqual(geometry["appInfo"]["WindowID"], 1)
        self.assert_registered_cleanup(hdc, {"geometry_92"})


if __name__ == "__main__":
    unittest.main()
