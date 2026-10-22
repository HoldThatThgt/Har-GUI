"""验证可视化页面的证据完整性、离线资源和数据嵌入边界。"""

import base64
import hashlib
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from har_capture.arkui import count_arkui_nodes, parse_arkui
from har_capture.collector import capture
from har_capture.hdc import CaptureError
from har_capture.visualization import build_viewer, encode_data, latest_capture
from tests.support import (
    FIXTURES, fake_hdc, layout_data, raw_accessibility, raw_arkui, raw_layout, raw_system_arkui,
    system_session_details,
)


class EmbeddedDataParser(HTMLParser):
    """仅按浏览器可识别的 script 边界提取 JSON 数据。"""

    def __init__(self):
        super().__init__()
        self.in_data = False
        self.chunks = []
        self.data_blocks = 0

    def handle_starttag(self, tag, attrs):
        self.in_data = tag == "script" and dict(attrs).get("type") == "application/json"
        if self.in_data:
            self.data_blocks += 1

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_data = False

    def handle_data(self, data):
        if self.in_data:
            self.chunks.append(data)


class VisualizationTests(unittest.TestCase):
    """使用真实采集格式和小型 PNG 验证页面生成流程。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "captures"
        templates = self.root / "viewer"
        templates.mkdir()
        (templates / "index.html").write_text(
            '<!doctype html><style>{{STYLES}}</style>'
            '<script type="application/json" id="capture-data">{{DATA}}</script>'
            '<script>{{SCRIPT}}</script>', encoding="utf-8")
        (templates / "styles.css").write_text("body { color: red; }", encoding="utf-8")
        (templates / "app.js").write_text('const templateLiteral = "{{DATA}}";', encoding="utf-8")
        self.template_patch = patch("har_capture.visualization.TEMPLATES", templates)
        self.template_patch.start()
        self.addCleanup(self.template_patch.stop)

    def new_capture(self, payloads=None, command_outputs=None):
        return capture(fake_hdc(payloads, command_outputs=command_outputs), self.output)

    def new_system_capture(self):
        return self.new_capture({"arkui_response_92": raw_system_arkui()},
                                command_outputs={"session_details": system_session_details()})

    def embedded_data(self, page):
        parser = EmbeddedDataParser()
        parser.feed(page.read_text(encoding="utf-8"))
        self.assertEqual(parser.data_blocks, 1)
        return json.loads("".join(parser.chunks))

    def write_metadata(self, directory, metadata):
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")

    def test_crlf_device_tables_remain_byte_exact_through_capture_and_viewer(self):
        names = {"windows": "windows.txt", "session_details": "session-details.txt"}
        outputs = {key: (FIXTURES / filename).read_text(encoding="utf-8").replace("\n", "\r\n")
                   for key, filename in names.items()}
        directory = self.new_capture(command_outputs=outputs)
        for key, filename in names.items():
            original = outputs[key].encode("utf-8")
            self.assertIn(b"\r\n", original)
            self.assertNotIn(b"\r\r\n", original)
            self.assertEqual((directory / filename).read_bytes(), original)
        payload = self.embedded_data(build_viewer(directory))
        self.assertEqual(payload["metadata"]["status"], "complete")
        self.assertEqual(set(payload["trees"]["arkui"]), {"80", "92"})
        for key, filename in names.items():
            original = outputs[key].encode("utf-8")
            self.assertEqual((directory / filename).read_bytes(), original)
            self.assertEqual(payload["metadata"]["files"][key]["sha256"], hashlib.sha256(original).hexdigest())
        for window in payload["metadata"]["windows"]:
            self.assertEqual(window["trees"]["arkui"]["session"]["window_id"], window["window_id"])

    def test_viewer_embeds_original_evidence_and_window_pointer_associations(self):
        directory = self.new_capture()
        original = {path.relative_to(directory): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in directory.rglob("*") if path.is_file()}
        page = build_viewer(directory)
        self.assertEqual(page, directory / "viewer.html")
        payload = self.embedded_data(page)
        metadata = json.loads((directory / "metadata.json").read_bytes())
        layout = json.loads((directory / "layout.json").read_bytes())
        self.assertEqual(payload["metadata"], metadata)
        self.assertEqual(payload["layout"], layout)
        self.assertEqual(payload["trees"]["accessibility"],
                         json.loads((directory / "accessibility.json").read_bytes()))
        self.assertEqual([window["window_id"] for window in metadata["windows"]], [92, 80])
        for window in payload["metadata"]["windows"]:
            index = int(window["layout"]["json_pointer"].removeprefix("/children/"))
            attrs = payload["layout"]["children"][index]["attributes"]
            self.assertEqual(attrs["hostWindowId"], str(window["window_id"]))
            self.assertEqual(attrs["bundleName"], window["bundle_name"])
            accessibility = window["trees"]["accessibility"]
            index = int(accessibility["json_pointer"].removeprefix("/"))
            root = payload["trees"]["accessibility"][index]
            self.assertEqual(root["attributes"]["hostWindowId"], str(window["window_id"]))
            self.assertEqual(root["attributes"]["bundleName"], window["bundle_name"])
            arkui = payload["trees"]["arkui"][str(window["window_id"])]
            saved = json.loads((directory / window["trees"]["arkui"]["file"]).read_bytes())
            self.assertEqual(arkui, saved)
            self.assertEqual(count_arkui_nodes(arkui), window["trees"]["arkui"]["node_count"])
            self.assertEqual(arkui["children"][0]["attributes"]["Content"], "原始组件数据")
        prefix, encoded = payload["screenshot"].split(",", 1)
        self.assertEqual(prefix, "data:image/png;base64")
        self.assertEqual(base64.b64decode(encoded), (directory / "screenshot.png").read_bytes())
        for name, digest in original.items():
            self.assertEqual(hashlib.sha256((directory / name).read_bytes()).hexdigest(), digest)

    def test_script_boundary_characters_round_trip_without_creating_html(self):
        value = {"text": '</script><script>alert("sample")</script>&<>\u2028\u2029中文',
                 "template": "{{DATA}} {{STYLES}} {{SCRIPT}}"}
        encoded = encode_data(value)
        self.assertEqual(json.loads(encoded), value)
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertNotIn("&", encoded)

    def test_untrusted_text_cannot_end_script_or_trigger_template_replacement(self):
        layout = layout_data()
        text = '</script><script>alert("sample")</script>{{STYLES}}{{SCRIPT}}{{DATA}}'
        layout["children"][0]["attributes"]["text"] = text
        arkui = raw_arkui().replace("原始组件数据".encode("utf-8"), text.encode("utf-8"))
        directory = self.new_capture({"layout": raw_layout(layout),
                                      "accessibility": raw_accessibility(layout["children"]),
                                      "arkui_response_92": arkui})
        page = build_viewer(directory)
        html = page.read_text(encoding="utf-8")
        payload = self.embedded_data(page)
        self.assertEqual(payload["layout"]["children"][0]["attributes"]["text"], text)
        self.assertEqual(payload["trees"]["accessibility"][0]["attributes"]["text"], text)
        self.assertEqual(payload["trees"]["arkui"]["92"]["children"][0]["attributes"]["Content"], text)
        self.assertNotIn('</script><script>alert("sample")', html)
        self.assertIn('const templateLiteral = "{{DATA}}";', html)
        self.assertEqual(html.count("body { color: red; }"), 1)

    def test_changed_original_files_are_rejected_before_page_publication(self):
        for name in ("screenshot.png", "layout.json", "accessibility.json", "arkui/window-92.txt",
                     "arkui/window-92.json",
                     "session-details.txt"):
            with self.subTest(name=name):
                directory = self.new_capture()
                artifact = directory / name
                artifact.write_bytes(artifact.read_bytes() + b" ")
                with self.assertRaises(CaptureError):
                    build_viewer(directory)
                self.assertFalse((directory / "viewer.html").exists())

    def test_both_tree_sources_are_required_without_older_schema_support(self):
        for missing in ("accessibility", "trees", "session_details"):
            with self.subTest(missing=missing):
                directory = self.new_capture()
                metadata = json.loads((directory / "metadata.json").read_bytes())
                if missing in ("accessibility", "session_details"):
                    del metadata["files"][missing]
                else:
                    del metadata["windows"][0]["trees"]
                self.write_metadata(directory, metadata)
                with self.assertRaises(KeyError):
                    build_viewer(directory)
                self.assertFalse((directory / "viewer.html").exists())

    def test_tree_associations_and_node_counts_must_match_metadata(self):
        mutations = (("accessibility", "json_pointer", "/0"), ("accessibility", "node_count", 99),
                     ("arkui", "node_count", 99), ("arkui", "scope", "display_root_scene"),
                     ("arkui", "display_id", 99))
        for source, key, value in mutations:
            with self.subTest(source=source, key=key):
                directory = self.new_capture()
                metadata = json.loads((directory / "metadata.json").read_bytes())
                metadata["windows"][0]["trees"][source][key] = value
                self.write_metadata(directory, metadata)
                with self.assertRaises(CaptureError):
                    build_viewer(directory)
                self.assertFalse((directory / "viewer.html").exists())

    def test_system_window_capture_and_viewer_preserve_only_its_own_component_tree(self):
        directory = self.new_system_capture()
        payload = self.embedded_data(build_viewer(directory))
        windows = {window["window_id"]: window for window in payload["metadata"]["windows"]}
        evidence = windows[92]["trees"]["arkui"]
        self.assertEqual(evidence["scope"], "window")
        self.assertEqual(evidence["origin"]["kind"], "source_slice")
        self.assertTrue(evidence["session"]["is_system"])
        self.assertEqual(evidence["source"], "WindowManagerService.-default.-all")
        self.assertEqual(windows[80]["trees"]["arkui"]["scope"], "window")
        root = payload["trees"]["arkui"]["92"]
        self.assertEqual(root["attributes"]["compid"], "session92")
        self.assertEqual(root["attributes"]["AccessibilityId"], "920")
        self.assertEqual(root["attributes"]["ID"], "192")
        self.assertEqual(count_arkui_nodes(root), 2)
        self.assertEqual(root["children"][0]["attributes"]["Content"], "原始系统组件数据")
        self.assertEqual((directory / evidence["response"]["file"]).read_bytes(), raw_system_arkui())
        selection = evidence["origin"]["selection"]
        start, length = selection["byte_offset"], selection["byte_length"]
        self.assertEqual((directory / evidence["raw"]["file"]).read_bytes(), raw_system_arkui()[start:start + length])
        self.assertEqual(json.loads((directory / evidence["file"]).read_bytes()), root)

    def test_system_original_response_digest_is_verified_before_visualization(self):
        directory = self.new_system_capture()
        response = directory / "arkui/window-92-response.txt"
        response.write_bytes(response.read_bytes() + b"\r\n")
        with self.assertRaises(CaptureError):
            build_viewer(directory)
        self.assertFalse((directory / "viewer.html").exists())

    def test_system_selection_metadata_cannot_claim_different_source_bytes(self):
        directory = self.new_system_capture()
        metadata = json.loads((directory / "metadata.json").read_bytes())
        metadata["windows"][0]["trees"]["arkui"]["origin"]["selection"]["byte_offset"] += 1
        self.write_metadata(directory, metadata)
        with self.assertRaises(CaptureError):
            build_viewer(directory)
        self.assertFalse((directory / "viewer.html").exists())

    def test_rehashed_system_slice_still_has_to_match_the_original_response(self):
        directory = self.new_system_capture()
        metadata = json.loads((directory / "metadata.json").read_bytes())
        evidence = metadata["windows"][0]["trees"]["arkui"]
        path = directory / evidence["raw"]["file"]
        original = path.read_bytes()
        changed = original.replace("原始系统组件数据".encode("utf-8"), "篡改系统组件数据".encode("utf-8"))
        self.assertNotEqual(original, changed)
        path.write_bytes(changed)
        digest = {"sha256": hashlib.sha256(changed).hexdigest(), "bytes": len(changed)}
        evidence["raw"].update(digest)
        metadata["files"]["arkui_raw_92"].update(digest)
        json_path = directory / evidence["file"]
        json_bytes = json.dumps(parse_arkui(changed.decode("utf-8")), ensure_ascii=False).encode("utf-8")
        json_path.write_bytes(json_bytes)
        json_digest = {"sha256": hashlib.sha256(json_bytes).hexdigest(), "bytes": len(json_bytes)}
        evidence.update(json_digest)
        metadata["files"]["arkui_92"].update(json_digest)
        self.write_metadata(directory, metadata)
        with self.assertRaises(CaptureError):
            build_viewer(directory)
        self.assertFalse((directory / "viewer.html").exists())

    def test_rehashed_json_must_still_equal_the_parsed_raw_tree(self):
        directory = self.new_capture()
        metadata = json.loads((directory / "metadata.json").read_bytes())
        evidence = metadata["windows"][0]["trees"]["arkui"]
        path = directory / evidence["file"]
        root = json.loads(path.read_bytes())
        root["children"][0]["attributes"]["Content"] = "修改后的组件文本"
        changed = json.dumps(root, ensure_ascii=False).encode("utf-8")
        path.write_bytes(changed)
        digest = {"sha256": hashlib.sha256(changed).hexdigest(), "bytes": len(changed)}
        evidence.update(digest)
        metadata["files"]["arkui_92"].update(digest)
        self.write_metadata(directory, metadata)
        with self.assertRaises(CaptureError):
            build_viewer(directory)
        self.assertFalse((directory / "viewer.html").exists())

    def test_missing_json_and_raw_metadata_do_not_reuse_the_older_text_format(self):
        for missing in ("json_file", "raw_metadata"):
            with self.subTest(missing=missing):
                directory = self.new_capture()
                metadata = json.loads((directory / "metadata.json").read_bytes())
                evidence = metadata["windows"][0]["trees"]["arkui"]
                if missing == "json_file":
                    (directory / evidence["file"]).unlink()
                    error = FileNotFoundError
                else:
                    del evidence["raw"]
                    self.write_metadata(directory, metadata)
                    error = KeyError
                with self.assertRaises(error):
                    build_viewer(directory)
                self.assertFalse((directory / "viewer.html").exists())

    def test_system_display_root_scope_is_no_longer_an_accepted_capture(self):
        directory = self.new_system_capture()
        metadata = json.loads((directory / "metadata.json").read_bytes())
        metadata["windows"][0]["trees"]["arkui"]["scope"] = "display_root_scene"
        self.write_metadata(directory, metadata)
        with self.assertRaises(CaptureError):
            build_viewer(directory)
        self.assertFalse((directory / "viewer.html").exists())

    def test_altered_session_claim_is_rejected_against_raw_details(self):
        directory = self.new_capture()
        metadata = json.loads((directory / "metadata.json").read_bytes())
        evidence = metadata["windows"][0]["trees"]["arkui"]
        evidence["session"]["ui_node_id"] += 1
        self.write_metadata(directory, metadata)
        with self.assertRaises(CaptureError):
            build_viewer(directory)
        self.assertFalse((directory / "viewer.html").exists())

    def test_display_dimensions_must_match_the_actual_image(self):
        directory = self.new_capture()
        metadata = json.loads((directory / "metadata.json").read_bytes())
        metadata["display"]["width"] += 1
        self.write_metadata(directory, metadata)
        with self.assertRaises(CaptureError):
            build_viewer(directory)
        self.assertFalse((directory / "viewer.html").exists())

    def test_incomplete_status_and_directory_suffix_are_rejected(self):
        for status, suffix in (("failed", ""), ("complete", ".partial"), ("complete", ".failed")):
            with self.subTest(status=status, suffix=suffix):
                directory = self.new_capture()
                metadata = json.loads((directory / "metadata.json").read_bytes())
                metadata["status"] = status
                self.write_metadata(directory, metadata)
                if suffix:
                    directory = directory.rename(directory.with_name(directory.name + suffix))
                with self.assertRaises(CaptureError):
                    build_viewer(directory)
                self.assertFalse((directory / "viewer.html").exists())

    def test_latest_capture_ignores_failed_and_partial_directories(self):
        older = self.new_capture().rename(self.output / "20261022T120000.000000Z-aaaaaaaa")
        newest = self.new_capture().rename(self.output / "20261022T130000.000000Z-bbbbbbbb")
        for suffix in (".partial", ".failed"):
            self.new_capture().rename(self.output / ("20261022T140000.000000Z-cccccccc" + suffix))
        self.assertEqual(latest_capture(self.output), newest)
        self.assertNotEqual(latest_capture(self.output), older)

    def test_latest_corrupt_metadata_does_not_select_an_older_capture(self):
        self.new_capture().rename(self.output / "20261022T120000.000000Z-aaaaaaaa")
        newest = self.new_capture().rename(self.output / "20261022T130000.000000Z-bbbbbbbb")
        (newest / "metadata.json").write_text("{", encoding="utf-8")
        self.assertEqual(latest_capture(self.output), newest)
        with self.assertRaises(json.JSONDecodeError):
            build_viewer(latest_capture(self.output))

    def test_latest_missing_metadata_does_not_select_an_older_capture(self):
        self.new_capture().rename(self.output / "20261022T120000.000000Z-aaaaaaaa")
        newest = self.new_capture().rename(self.output / "20261022T130000.000000Z-bbbbbbbb")
        (newest / "metadata.json").unlink()
        self.assertEqual(latest_capture(self.output), newest)
        with self.assertRaises(FileNotFoundError):
            build_viewer(latest_capture(self.output))

    def test_missing_or_empty_capture_root_is_an_error(self):
        with self.assertRaises(CaptureError):
            latest_capture(self.output)
        self.output.mkdir()
        with self.assertRaises(CaptureError):
            latest_capture(self.output)


if __name__ == "__main__":
    unittest.main()
