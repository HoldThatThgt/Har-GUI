"""五件套、主窗口分类和发布前真实性门槛测试。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from har_capture.export import classify_window_mode, delivery_payloads, sample_prefix, write_delivery
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from tests.support import png_bytes


def window(window_id=1, bundle="app.test", x=0, width=4, window_type=1, system=False):
    """构造可自解释的匿名窗口记录。"""
    return {"window_id": window_id, "bundle_name": bundle, "is_system": system, "window_type": window_type,
            "window_mode": 102, "z_order": window_id, "display_id": 0,
            "bounding_box": {"x": x, "y": 0, "width": width, "height": 6},
            "transform": {"offset_x": 0, "offset_y": 0, "scale_x": 1, "scale_y": 1},
            "trees": {"accessibility": {"node_count": 1}, "arkui": {"node_count": 1}}}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "screenshot.png").write_bytes(png_bytes())
        self.windows = [window(), window(2, "app.test", 1, 2, 1001), window(3, "system.dock", system=True)]
        trees = [{"attributes": {"hostWindowId": str(item["window_id"]), "bundleName": item["bundle_name"],
                                  "displayId": "0"},
                  "children": []} for item in self.windows]
        (self.directory / "accessibility.json").write_text(json.dumps(trees), encoding="utf-8")
        for index, item in enumerate(self.windows):
            item["trees"]["accessibility"].update(file="accessibility.json", json_pointer=f"/{index}",
                artifact=file_info(self.directory / "accessibility.json"))
        self.metadata = {"status": "complete", "capture_id": "frame", "windows": self.windows,
                         "window_inventory": copy.deepcopy(self.windows), "device": {"model": "anonymous"},
                         "started_at": "2026-10-22T10:00:00Z", "finished_at": "2026-10-22T10:00:01Z",
                         "coverage": {"status": "verified", "covered_window_ids": [1, 2, 3]},
                         "consistency": {"status": "verified", "atomic": False},
                         "stability": {"status": "verified", "rules": [], "regions": []},
                         "privacy": {"status": "disabled", "enabled": False, "regions": [],
                                     "before_sha256": file_info(self.directory / "screenshot.png")["sha256"],
                                     "after_sha256": file_info(self.directory / "screenshot.png")["sha256"]},
                         "files": {name: file_info(self.directory / filename) for name, filename in (
                             ("screenshot", "screenshot.png"), ("accessibility", "accessibility.json"))}}
        self.options = {"app": "示例", "page_id": "page", "scene": "文档页面", "state_id": "state",
                        "canvas": {"width": 8, "height": 6}}

    def normalized(self, evidence, current, canvas, scenes):
        return {"window_id": current["window_id"], "bundle_name": current["bundle_name"],
                "tree": {"attributes": {}, "children": [], "coordinate_validation": {"status": "verified"}}}

    def test_export_has_exactly_five_files_and_moves_the_only_image(self):
        with patch("har_capture.export.load_scene_evidence", return_value={}), \
                patch("har_capture.export.normalized_arkui", side_effect=self.normalized):
            result = write_delivery(self.directory, self.metadata, self.options)
        delivery = Path(result["path"])
        self.assertEqual(len(list(delivery.iterdir())), 5)
        self.assertEqual(set(result["files"]), {"tree", "windows_tree", "windows_akui", "screenshot", "wms"})
        self.assertEqual(len(list(self.directory.rglob("*.png"))), 1)
        self.assertFalse((self.directory / "screenshot.png").exists())
        for file in result["files"].values():
            self.assertEqual(file_info(delivery / file["file"]), file)
        saved = json.loads((self.directory / "metadata.json").read_bytes())
        self.assertTrue((self.directory / saved["files"]["screenshot"]["file"]).is_file())
        self.assertEqual(result["checks"], dict.fromkeys(("coverage", "coordinates", "privacy", "consistency"), "pass"))

    def test_system_tree_is_in_screen_tree_and_subwindow_is_in_application_trees(self):
        with patch("har_capture.export.load_scene_evidence", return_value={}) as scene_loader, \
                patch("har_capture.export.normalized_arkui", side_effect=self.normalized):
            payload = delivery_payloads(self.directory, self.metadata, self.options)
        scene_loader.assert_called_once_with(self.directory, self.windows)
        self.assertEqual([item["window_id"] for item in payload["tree"]["windows"]], [1, 2, 3])
        for key in ("windows_tree", "windows_akui"):
            self.assertEqual([item["window_id"] for item in payload[key]["windows"]], [1, 2])
        self.assertEqual(payload["wms"]["window_mode"], "单窗口缩放")
        for record in payload["wms"]["app_windows"]:
            self.assertIn("_windows_akui.json", record["trees"]["arkui"]["file"])
            self.assertNotIn("layout", record)

    def test_all_six_modes_and_edge_touching(self):
        fullscreen = window()
        fullscreen["window_mode"] = 1
        cases = [("单窗口全屏", [fullscreen]), ("单窗口缩放", [window()]),
                 ("单应用多窗口并列", [window(), window(2, x=4)]),
                 ("单应用多窗口交叉", [window(), window(2, x=3)]),
                 ("多应用多窗口并列", [window(), window(2, "app.other", x=4)]),
                 ("多应用多窗口交叉", [window(), window(2, "app.other", x=3)])]
        for expected, windows in cases:
            self.assertEqual(classify_window_mode(windows), expected)
        with self.assertRaises(CaptureError):
            classify_window_mode([window(window_type=1001)])

    def test_excluded_windows_remain_in_inventory_but_not_visible_application_or_screen_trees(self):
        self.metadata["coverage"]["covered_window_ids"] = [1, 3]
        self.metadata["coverage"]["excluded_windows"] = [{"window_id": 2,
                                                            "reason": "render_service_totally_occluded"}]
        with patch("har_capture.export.load_scene_evidence", return_value={}) as scene_loader, \
                patch("har_capture.export.normalized_arkui", side_effect=self.normalized):
            payload = delivery_payloads(self.directory, self.metadata, self.options)
        scene_loader.assert_called_once_with(self.directory, [self.windows[0], self.windows[2]])
        self.assertEqual([window["window_id"] for window in payload["wms"]["app_windows"]], [1])
        self.assertEqual([window["window_id"] for window in payload["tree"]["windows"]], [1, 3])
        self.assertEqual([window["window_id"] for window in payload["windows_tree"]["windows"]], [1])
        self.assertEqual([window["window_id"] for window in payload["windows_akui"]["windows"]], [1])
        self.assertEqual([window["window_id"] for window in payload["wms"]["windows"]], [1, 2, 3])

    def test_export_refuses_unverified_gates_and_bad_canvas(self):
        for key in ("coverage", "consistency", "privacy"):
            metadata = copy.deepcopy(self.metadata)
            metadata[key]["status"] = "failed"
            with self.assertRaises(CaptureError):
                delivery_payloads(self.directory, metadata, self.options)
        self.options["canvas"]["width"] = 3120
        with self.assertRaises(CaptureError):
            delivery_payloads(self.directory, self.metadata, self.options)
        self.assertFalse((self.directory / "delivery").exists())

    def test_export_checks_final_redacted_image_digest(self):
        (self.directory / "screenshot.png").write_bytes(png_bytes("black"))
        with self.assertRaisesRegex(CaptureError, "digest"):
            delivery_payloads(self.directory, self.metadata, self.options)

    def test_export_requires_consistent_enabled_receipt_and_unchanged_disabled_image(self):
        mutations = ({"enabled": True}, {"status": "verified"}, {"status": "not_required"},
                     {"before_sha256": "0" * 64}, {"regions": [{"resource_id": "body"}]})
        for mutation in mutations:
            metadata = copy.deepcopy(self.metadata)
            metadata["privacy"].update(mutation)
            with self.subTest(mutation=mutation), self.assertRaises(CaptureError):
                delivery_payloads(self.directory, metadata, self.options)

    def test_invalid_file_names_are_rejected_without_rewriting_identity(self):
        for bad in ("../secret", "bad/name", "trailing.", "", "x:y"):
            with self.assertRaises(CaptureError):
                sample_prefix({**self.options, "app": bad})

    def test_unavailable_arkui_coordinates_do_not_create_a_delivery(self):
        with patch("har_capture.export.load_scene_evidence", return_value={}), \
                patch("har_capture.export.normalized_arkui", side_effect=CaptureError("Coordinates unverified")):
            with self.assertRaises(CaptureError):
                write_delivery(self.directory, self.metadata, self.options)
        self.assertFalse((self.directory / "delivery").exists())
        self.assertTrue((self.directory / "screenshot.png").exists())


if __name__ == "__main__":
    unittest.main()
