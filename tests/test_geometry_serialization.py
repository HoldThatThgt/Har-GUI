"""真实几何在准备、读取和登记之间保持规范 JSON 类型，摘要重算不能掩盖坐标篡改。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from har_capture import dataset
from har_capture.coordinates import normalize_arkui
from har_capture.hdc import CaptureError
from har_capture.metadata import file_info
from har_capture.privacy import atomic_json
from tests.delivery_fixture import CaptureFixture, snapshot_files
from tests.test_coordinates import fixture as coordinate_fixture
from tests.test_viewport_geometry import native_rectangle, raw_rectangle


def normalized_geometry(window=None, canvas=None):
    """为匿名窗口执行真实坐标证明，包含矩形、圆角及内容裁剪，不替代 normalize_arkui。"""
    args = list(coordinate_fixture())
    if window is not None:
        box = window["bounding_box"]
        width, height, x, y = (box[key] for key in ("width", "height", "x", "y"))
        args[1] = copy.deepcopy(window)
        args[2] = dict(canvas)
        args[0]["attributes"].update(FrameRect=raw_rectangle(0, 0, width, height))
        args[0]["attributes"]["PaintRect without transform"] = raw_rectangle(0, 0, width, height)
        args[3]["arkui_geometry"]["$rect"] = native_rectangle(0, 0, width, height)
        scene = args[3]["scene"]
        session = scene["path"][-1]
        session["attributes"].update(FrameRect=raw_rectangle(x, y, width, height),
            AccessibilityId=str(window["ui_node_id"]), compid=f"session{window['window_id']}")
        session["attributes"]["PaintRect without transform"] = raw_rectangle(x, y, width, height)
        native_session = scene["native"]["$children"][0]
        native_session.update({"$rect": native_rectangle(x, y, width, height),
                               "compid": f"session{window['window_id']}"})
        args[3]["render_tree"] = args[3]["render_tree"].replace("100 50 200 150", f"{x} {y} {width} {height}")
        args[3]["render_tree"] = args[3]["render_tree"].replace("0 0 200 150", f"0 0 {width} {height}")
        args[3]["render_tree"] = args[3]["render_tree"].replace("session9]", f"session{window['window_id']}]")
    args[3]["render_tree"] = args[3]["render_tree"].replace(
        "ClipToBounds[True], OtherModifiers:[ClipToBounds:[true]]",
        "ClipToBounds[True], CornerRadius[1 2 3 4], OtherModifiers:[ClipToBounds:[true], "
        "CornerRadius:[1 2 3 4], CustomClipToFrame:[x:7 y:0 z:30 w:40]]")
    result = normalize_arkui(*args)
    if result["coordinate_validation"]["status"] != "verified":
        raise AssertionError(result["coordinate_validation"]["issues"])
    return result


class GeometryCaptureFixture(CaptureFixture):
    """沿用无设备来源夹具，但事实中的几何必须由实际 normalize_arkui 生成。"""

    @staticmethod
    def normalized(evidence, window, canvas, scenes):
        return {"window_id": window["window_id"], "bundle_name": window["bundle_name"],
                "tree": normalized_geometry(window, canvas)}


class GeometrySerializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="har-gui-geometry-serialization-")
        self.addCleanup(self.temporary.cleanup)

    def test_actual_normalized_geometry_is_equal_after_json_roundtrip(self):
        tree = normalized_geometry()
        roundtrip = json.loads(json.dumps(tree, ensure_ascii=False, allow_nan=False))
        self.assertEqual(tree, roundtrip)
        clips = tree["children"][0]["children"][0]["geometry"]["clip_chain"]
        self.assertTrue(any(clip["kind"] == "rounded_rectangle" for clip in clips))
        self.assertTrue(any(clip.get("mode") == "frame" for clip in clips))
        self.assertTrue(any(clip.get("mode") == "content_and_children" for clip in clips))
        for clip in clips:
            if "corners" in clip:
                self.assertIs(type(clip["corners"]), list)
                self.assertTrue(all(type(point) is list for point in clip["corners"]))

    def prepare_fixture(self):
        fixture = GeometryCaptureFixture(Path(self.temporary.name))
        context = fixture.patch_geometry()
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        evidence = fixture.capture("geometry-frame")
        return fixture, evidence, fixture.prepare(evidence)

    def test_real_geometry_can_be_prepared_again_without_rewriting_any_bytes_or_times(self):
        fixture, evidence, first = self.prepare_fixture()
        before = snapshot_files([evidence])
        second = fixture.prepare(evidence)
        self.assertEqual(first, second)
        self.assertEqual(before, snapshot_files([evidence]))
        facts = json.loads((evidence / "accepted/capture.json").read_bytes())
        self.assertEqual(facts, json.loads(json.dumps(facts, ensure_ascii=False, allow_nan=False)))
        manifest = dataset.prepare_manifest(Path(first["manifest"]["path"]), fixture.identity,
                                            fixture.acceptance(first))
        self.assertEqual(manifest["sample_id"], "geometry-frame")
        self.assertTrue(manifest["visual_acceptance"]["pass"])
        self.assertEqual(before, snapshot_files([evidence]))
        self.assertFalse((fixture.root / "dataset.sqlite3").exists())

    def test_rehashed_prepared_coordinate_tampering_is_still_rejected_from_original_sources(self):
        fixture, evidence, prepared = self.prepare_fixture()
        record = evidence / "accepted/capture.json"
        stored = json.loads(record.read_bytes())
        stored["arkui"]["windows"][0]["tree"]["children"][0]["geometry"]["screen_bounds"]["x"] += 1
        atomic_json(record, stored)
        changed = copy.deepcopy(prepared)
        changed["manifest"]["files"]["capture"] = file_info(record)
        with self.assertRaisesRegex(CaptureError, "Prepared capture facts differ from their original evidence"):
            dataset.prepare_manifest(Path(changed["manifest"]["path"]), fixture.identity, fixture.acceptance(changed))
        self.assertEqual(json.loads(record.read_bytes()), stored)
        self.assertFalse((fixture.root / "dataset.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
