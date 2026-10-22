"""验证窗口管理器解析、严格归属校验与原始文件保留。"""

import copy
import tempfile
import unittest
from pathlib import Path

from har_capture.hdc import CaptureError
from har_capture.metadata import associate_windows, load_layout, parse_windows
from tests.support import FIXTURES, layout_data, raw_layout


class WindowMetadataTests(unittest.TestCase):
    """覆盖现场窗口列表中容易误解的字段。"""

    def setUp(self):
        self.text = (FIXTURES / "windows.txt").read_text(encoding="utf-8")
        self.inventory = parse_windows(self.text)

    def test_real_table_format_keeps_all_rows_despite_reported_total(self):
        self.assertIn("Total window num: 10", self.text)
        self.assertEqual(len(self.inventory), 7)
        self.assertEqual(sum(row["z_order"] >= 0 for row in self.inventory.values()), 3)

    def test_negative_coordinates_and_hidden_z_order_are_preserved(self):
        self.assertEqual(self.inventory[48]["bounding_box"]["y"], -46)
        self.assertEqual(self.inventory[48]["z_order"], -1)
        self.assertEqual(self.inventory[47]["transform"]["pivot_x"], 0.43)

    def test_same_window_names_are_kept_by_distinct_ids(self):
        self.assertEqual(self.inventory[75]["window_name"], self.inventory[76]["window_name"])
        self.assertNotEqual(self.inventory[75]["window_id"], self.inventory[76]["window_id"])

    def test_z_order_and_bounds_come_from_window_manager(self):
        data = layout_data()
        data["children"][1]["attributes"]["bounds"] = "[1000,100][2600,1600]"
        windows = associate_windows(data, self.inventory, 0)
        self.assertEqual([window["window_id"] for window in windows], [92, 80])
        self.assertEqual([window["z_order"] for window in windows], [112, 111])
        self.assertEqual(windows[0]["bounding_box"]["x"], 1034)
        self.assertEqual(windows[0]["layout_bounding_box"]["x"], 1000)
        self.assertEqual(windows[0]["layout"]["json_pointer"], "/children/1")

    def test_missing_bundle_or_window_id_fails(self):
        for field in ("bundleName", "hostWindowId"):
            for value in (None, "", " ", "omitted"):
                with self.subTest(field=field, value=value):
                    data = layout_data()
                    attrs = data["children"][0]["attributes"]
                    if value == "omitted":
                        del attrs[field]
                    else:
                        attrs[field] = value
                    with self.assertRaises(CaptureError):
                        associate_windows(data, self.inventory, 0)

    def test_missing_z_order_header_or_value_fails(self):
        bad_header = self.text.replace("ZOrd", "Depth", 1)
        bad_row = self.text.replace("16   112  0", "16        0", 1)
        self.assertNotEqual(bad_row, self.text)
        for text in (bad_header, bad_row):
            with self.subTest(text=text[:40]), self.assertRaises(CaptureError):
                parse_windows(text)

    def test_duplicate_window_ids_and_error_output_fail(self):
        row = next(line for line in self.text.splitlines() if line.startswith("ExampleSettings"))
        duplicate = self.text.replace("Focus window:", row + "\nFocus window:", 1)
        for text in (duplicate, "Permission denied", "", "WindowName DisplayId WinId ZOrd\n"):
            with self.subTest(text=text[:40]), self.assertRaises(CaptureError):
                parse_windows(text)

    def test_spaced_names_and_fractional_scientific_transforms(self):
        original = next(line for line in self.text.splitlines() if line.startswith("ExampleSettings"))
        changed = ("Example Window 0 202 92 1 102 16 112 0 [ -5 -10 1596 1468 ] "
                   "[ 0.5     -1.25   ] [ 1e-05   +1.25   .43     1.      ]")
        window = parse_windows(self.text.replace(original, changed))[92]
        self.assertEqual(window["window_name"], "Example Window")
        self.assertEqual(window["bounding_box"]["x"], -5)
        self.assertEqual(window["transform"], {"offset_x": 0.5, "offset_y": -1.25, "scale_x": 1e-5,
                                              "scale_y": 1.25, "pivot_x": 0.43, "pivot_y": 1.0})

    def test_real_adjacent_pivot_columns_keep_calculator_window_and_exact_values(self):
        original = next(line for line in self.text.splitlines() if line.startswith("ExampleSettings"))
        changed = ("calculator0          0         21868   95    1    102  0    113  0           "
                   "[ 489  239  2091 1394 ] [ 0       0       ] [ 1       1       0.4998570.499857]")
        windows = parse_windows(self.text.replace(original, changed))
        self.assertEqual(len(windows), len(self.inventory))
        self.assertEqual(windows[95]["window_name"], "calculator0")
        self.assertEqual(windows[95]["process_id"], 21868)
        self.assertEqual(windows[95]["bounding_box"], {"x": 489, "y": 239, "width": 2091, "height": 1394})
        self.assertEqual(windows[95]["transform"], {"offset_x": 0, "offset_y": 0, "scale_x": 1,
                                                   "scale_y": 1, "pivot_x": 0.499857, "pivot_y": 0.499857})

    def test_every_transform_column_can_fill_its_width_without_separators(self):
        original = next(line for line in self.text.splitlines() if line.startswith("ExampleSettings"))
        prefix = original[:original.index("[ 0       0       ]")]
        changed = prefix + "[ -12.34560.499857] [ 1.23e-05-1.2e-050.4998570.123456]"
        transform = parse_windows(self.text.replace(original, changed))[92]["transform"]
        self.assertEqual(transform, {"offset_x": -12.3456, "offset_y": 0.499857, "scale_x": 1.23e-5,
                                     "scale_y": -1.2e-5, "pivot_x": 0.499857, "pivot_y": 0.123456})

    def test_invalid_width_alignment_padding_and_extra_fields_are_rejected(self):
        original = next(line for line in self.text.splitlines() if line.startswith("ExampleSettings"))
        block = "[ 1       1       0.5     0.5     ]"
        invalid = ["[ 1 1 0.5 0.5 ]", "[ 1       1       0.499850.499857]",
                   "[  1      1       0.5     0.5     ]", "[ 1 2     1       0.5     0.5     ]",
                   "[ 1\t      1       0.5     0.5     ]", "[ 1       1       0.5     0.5     0       ]",
                   "[ 1       1       0.49985x0.499857]", "[ 1       1       1.234e- 0.499857]"]
        for value in invalid:
            changed = original.replace(block, value)
            with self.subTest(value=value), self.assertRaises(CaptureError):
                parse_windows(self.text.replace(original, changed))

    def test_reordered_header_and_nonfinite_transforms_fail(self):
        bad_header = self.text.replace("Flag ZOrd", "ZOrd Flag", 1)
        bad_number = self.text.replace("0.43    ", "1e999   ", 1)
        for text in (bad_header, bad_number):
            with self.subTest(text=text[:40]), self.assertRaises(CaptureError):
                parse_windows(text)

    def test_missing_or_hidden_window_manager_record_fails(self):
        for operation in ("missing", "hidden", "other_display"):
            with self.subTest(operation=operation):
                inventory = copy.deepcopy(self.inventory)
                if operation == "missing":
                    del inventory[80]
                elif operation == "hidden":
                    inventory[80]["z_order"] = -1
                else:
                    inventory[80]["display_id"] = 1
                with self.assertRaises(CaptureError):
                    associate_windows(layout_data(), inventory, 0)

    def test_child_ownership_and_duplicate_roots_fail(self):
        invalid_owner = layout_data()
        child = copy.deepcopy(invalid_owner["children"][0])
        child["attributes"]["hostWindowId"] = "999"
        invalid_owner["children"][0]["children"].append(child)
        duplicate = layout_data()
        duplicate["children"].append(copy.deepcopy(duplicate["children"][0]))
        for data in (invalid_owner, duplicate):
            with self.subTest(data=data), self.assertRaises(CaptureError):
                associate_windows(data, self.inventory, 0)

    def test_load_layout_preserves_exact_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "layout.json"
            original = raw_layout()
            path.write_bytes(original)
            self.assertEqual(len(load_layout(path)["children"]), 2)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
