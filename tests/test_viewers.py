"""Tests for body classification, download naming, and the viewer widgets."""

from __future__ import annotations

import json

import pytest

from api_tester import content
from api_tester.client import ApiResult


class TestClassification:
    @pytest.mark.parametrize(
        ("content_type", "payload", "expected"),
        [
            ("application/json", b"{}", content.JSON),
            ("application/problem+json; charset=utf-8", b"{}", content.JSON),
            ("text/plain", b"hello", content.TEXT),
            ("text/html", b"<html></html>", content.HTML),
            ("application/xml", b"<a/>", content.XML),
            ("image/png", b"\x89PNG\r\n", content.IMAGE),
            ("application/pdf", b"%PDF-1.4\x00", content.BINARY),
            ("application/octet-stream", b"\x00\x01\x02", content.BINARY),
            ("application/json", b"", content.EMPTY),
        ],
    )
    def test_declared_types(self, content_type, payload, expected):
        assert content.classify(content_type, payload) == expected

    def test_sniffs_json_without_content_type(self):
        assert content.classify("", b'  {"a": 1}') == content.JSON

    def test_sniffs_binary_without_content_type(self):
        assert content.classify("", b"\x00\x01binary") == content.BINARY

    def test_utf8_text_is_not_binary(self):
        assert not content.looks_binary("héllo wörld".encode())

    def test_octet_stream_carrying_text_stays_text(self):
        assert content.classify("application/octet-stream", b"plain text") == content.TEXT


class TestFilenames:
    def test_quoted_filename(self):
        assert (
            content.filename_from_disposition('attachment; filename="report.xlsx"')
            == "report.xlsx"
        )

    def test_unquoted_filename(self):
        assert content.filename_from_disposition("attachment; filename=report.csv") == "report.csv"

    def test_rfc5987_filename_wins(self):
        disposition = "attachment; filename=\"fallback.txt\"; filename*=UTF-8''trial%20report.pdf"
        assert content.filename_from_disposition(disposition) == "trial report.pdf"

    def test_path_traversal_is_stripped(self):
        assert (
            content.filename_from_disposition(r'attachment; filename="..\..\evil.exe"')
            == "evil.exe"
        )

    def test_missing_disposition(self):
        assert content.filename_from_disposition("") == ""

    def test_falls_back_to_url_tail(self):
        name = content.suggested_filename(
            "application/pdf", "", "https://api.test/v1/reports/summary.pdf"
        )
        assert name == "summary.pdf"

    def test_falls_back_to_content_type_extension(self):
        assert content.suggested_filename("application/json", "", "https://api.test/v1/brands") == (
            "response.json"
        )

    def test_unknown_type_uses_subtype(self):
        assert content.suggested_filename("application/x-custom", "", "") == "response.x-custom"

    def test_disposition_beats_url(self):
        name = content.suggested_filename(
            "application/pdf", 'attachment; filename="a.pdf"', "https://api.test/b.pdf"
        )
        assert name == "a.pdf"


class TestFormatting:
    def test_pretty_json_indents(self):
        formatted, error = content.pretty_json('{"a":1}')
        assert error == ""
        assert formatted == '{\n  "a": 1\n}'

    def test_pretty_json_reports_error_and_preserves_input(self):
        formatted, error = content.pretty_json("{not json")
        assert formatted == "{not json"
        assert "column" in error

    def test_minify_json(self):
        compact, error = content.minify_json('{\n  "a": 1\n}')
        assert (compact, error) == ('{"a":1}', "")

    def test_pretty_xml(self):
        formatted, error = content.pretty_xml("<a><b>1</b></a>")
        assert error == ""
        assert "<b>1</b>" in formatted

    def test_describe_json(self):
        assert content.describe_json([1, 2]) == "array · 2 items"
        assert content.describe_json({"a": 1}) == "object · 1 field"
        assert content.describe_json(None) == "null"

    def test_human_size(self):
        assert content.human_size(512) == "512 B"
        assert content.human_size(2048) == "2.0 KB"

    def test_decode_text_never_raises(self):
        assert content.decode_text(b"\xff\xfe", "text/plain")


class TestInspectBody:
    def test_json_body_is_formatted(self):
        info = content.inspect_body(b'{"a":1}', "application/json")
        assert info.kind == content.JSON
        assert info.text == '{\n  "a": 1\n}'
        assert not info.is_binary

    def test_binary_body_keeps_size_and_filename(self):
        info = content.inspect_body(
            b"\x00" * 10, "application/pdf", 'attachment; filename="x.pdf"'
        )
        assert info.is_binary
        assert info.size == 10
        assert info.filename == "x.pdf"
        assert info.text == ""

    def test_invalid_json_surfaces_error(self):
        info = content.inspect_body(b"{oops", "application/json")
        assert info.error
        assert info.text == "{oops"


class TestHeaderGrouping:
    def test_groups_are_ordered_and_complete(self):
        groups = content.group_headers(
            {
                "Content-Type": "application/json",
                "X-Request-Id": "abc",
                "Cache-Control": "no-store",
                "X-Custom": "1",
            }
        )
        names = [group for group, _ in groups]
        assert names == ["Content", "Caching", "Tracing", "Other"]
        assert dict(groups[3][1]) == {"X-Custom": "1"}

    def test_no_headers(self):
        assert content.group_headers({}) == []


class TestApiResultDefaults:
    def test_new_fields_default_so_existing_call_sites_work(self):
        result = ApiResult(
            passed=True,
            status_code=200,
            elapsed_ms=1,
            url="https://api.test/v1/brands",
            response_headers={},
            response_body="{}",
        )
        assert result.content == b""
        assert result.size == 0
        assert result.request_headers == {}


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class TestViewerWidgets:
    def test_request_body_actions_are_theme_aware_icons_with_stable_layout(self, qt_app):
        from api_tester import theme
        from api_tester.viewers import RequestBodyEditor

        editor = RequestBodyEditor()
        buttons = [
            button for button, _tooltip in editor.action_buttons.values()
        ]
        policies = [
            (
                button.sizePolicy().horizontalPolicy(),
                button.sizePolicy().verticalPolicy(),
            )
            for button in buttons
        ]
        layout = editor.layout().itemAt(0).layout()
        placements = [layout.indexOf(button) for button in buttons]

        assert all(button.text() == "" for button in buttons)
        assert all(button.toolTip() for button in buttons)
        assert all(button.accessibleName() == button.toolTip() for button in buttons)

        theme.apply_theme(qt_app, "Light")
        editor.refresh_theme()
        light_keys = [button.icon().cacheKey() for button in buttons]
        assert all(not button.icon().isNull() for button in buttons)
        theme.apply_theme(qt_app, "Dark")
        editor.refresh_theme()
        assert [button.icon().cacheKey() for button in buttons] != light_keys
        assert placements == [layout.indexOf(button) for button in buttons]
        assert policies == [
            (
                button.sizePolicy().horizontalPolicy(),
                button.sizePolicy().verticalPolicy(),
            )
            for button in buttons
        ]
        theme.apply_theme(qt_app, "Light")

    def test_response_toolbar_uses_theme_aware_icon_actions(self, qt_app):
        from api_tester import theme
        from api_tester.viewers import ResponseViewer

        theme.apply_theme(qt_app, "Light")
        viewer = ResponseViewer()
        buttons = [button for button, _name, _tooltip in viewer._response_actions]
        light_keys = [button.icon().cacheKey() for button in buttons]

        assert len(buttons) == 7
        assert all(button.text() == "" for button in buttons)
        assert all(button.toolTip() for button in buttons)
        assert all(button.accessibleName() == button.toolTip() for button in buttons)
        assert all(not button.icon().isNull() for button in buttons)
        assert all(button.size().width() == 34 for button in buttons)

        theme.apply_theme(qt_app, "Dark")
        viewer.refresh_theme()
        assert [button.icon().cacheKey() for button in buttons] != light_keys
        theme.apply_theme(qt_app, "Light")

    def test_url_is_below_status_and_uses_full_header_width(self, qt_app):
        from api_tester.viewers import ResponseViewer

        viewer = ResponseViewer()
        viewer.status_label.setText("PASS - HTTP 200")
        viewer.url_label.setText("https://api.test/items?Filter=Name__eq:=Red Apple")
        viewer.resize(900, 600)
        viewer.show()
        qt_app.processEvents()
        try:
            assert viewer.url_label.y() > viewer.status_label.geometry().bottom()
            assert viewer.url_label.x() == viewer.status_label.x()
            assert viewer.url_label.width() == viewer.status_label.width()
            assert viewer.url_label.wordWrap()
        finally:
            viewer.close()

    def test_response_viewer_renders_json(self, qt_app):
        from api_tester.viewers import ResponseViewer

        viewer = ResponseViewer()
        viewer.show_result(
            ApiResult(
                passed=True,
                status_code=200,
                elapsed_ms=12,
                url="https://api.test/v1/brands",
                response_headers={"Content-Type": "application/json"},
                response_body='{"a": 1}',
                content=b'{"a":1}',
                content_type="application/json",
            )
        )
        assert '"a": 1' in viewer.text_view.toPlainText()
        assert viewer.body_stack.currentWidget() is viewer.text_view
        assert viewer.headers_table.rowCount() > 0

    def test_response_viewer_switches_to_binary_panel(self, qt_app):
        from api_tester.viewers import ResponseViewer

        viewer = ResponseViewer()
        viewer.show_result(
            ApiResult(
                passed=True,
                status_code=200,
                elapsed_ms=5,
                url="https://api.test/v1/export",
                response_headers={"Content-Type": "application/pdf"},
                response_body="<binary>",
                content=b"%PDF-1.4\x00\x01",
                content_type="application/pdf",
                content_disposition='attachment; filename="trial.pdf"',
            )
        )
        assert viewer.body_stack.currentWidget() is viewer.binary_view
        assert viewer.binary_view._filename == "trial.pdf"
        assert viewer.binary_view.save_button.isEnabled()

    def test_binary_panel_writes_file(self, qt_app, tmp_path, monkeypatch):
        from api_tester import viewers

        panel = viewers.BinaryPanel()
        info = content.inspect_body(b"\x00binary", "application/pdf", 'attachment; filename="a.pdf"')
        panel.show_body(info, b"\x00binary")
        target = tmp_path / "saved.pdf"
        monkeypatch.setattr(
            viewers.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "")
        )
        assert panel.save() == str(target)
        assert target.read_bytes() == b"\x00binary"

    def test_response_viewer_falls_back_to_text_body(self, qt_app):
        """Persisted results carry only decoded text, not bytes."""
        from api_tester.runner import CaseResult
        from api_tester.viewers import ResponseViewer

        viewer = ResponseViewer()
        viewer.show_result(
            CaseResult(
                case_id="c1",
                case_name="case",
                endpoint_id="e1",
                method="GET",
                path="/v1/brands",
                service="Brand",
                passed=True,
                status_code=200,
                response_body='{"b": 2}',
            )
        )
        assert '"b": 2' in viewer.text_view.toPlainText()

    def test_response_viewer_error_state(self, qt_app):
        from api_tester.viewers import ResponseViewer

        viewer = ResponseViewer()
        viewer.show_error("connection refused")
        assert "connection refused" in viewer.text_view.toPlainText()

    def test_response_viewer_records_timeline_and_previous_response(self, qt_app):
        from api_tester.viewers import ResponseViewer

        viewer = ResponseViewer()
        first = ApiResult(
            passed=True,
            status_code=200,
            elapsed_ms=10,
            url="https://api.test/items",
            response_headers={"Content-Type": "application/json"},
            response_body='{"id":1}',
            content=b'{"id":1}',
            content_type="application/json",
            timings={"dns_ms": 1.2, "tcp_ms": 2.3, "tls_ms": 4.5, "http_ms": 10},
        )
        viewer.show_result(first)
        assert len(viewer.timeline.rows()) == 4
        viewer.show_result(
            ApiResult(
                passed=True,
                status_code=200,
                elapsed_ms=8,
                url=first.url,
                response_headers=first.response_headers,
                response_body='{"id":2}',
                content=b'{"id":2}',
                content_type="application/json",
            )
        )
        assert viewer.compare_button.isEnabled()

    def test_diagnostic_export_uses_structured_redacted_data(
        self, qt_app, tmp_path, monkeypatch
    ):
        from api_tester import viewers

        viewer = viewers.ResponseViewer()
        viewer.show_result(
            ApiResult(
                passed=False,
                status_code=500,
                elapsed_ms=22,
                url="https://api.test/items",
                response_headers={"Content-Type": "text/plain"},
                response_body="failure",
                content=b"failure",
                content_type="text/plain",
                request_headers={"Authorization": "******"},
            )
        )
        target = tmp_path / "diagnostic.json"
        monkeypatch.setattr(
            viewers.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), "")
        )
        assert viewer._export_diagnostic() == str(target)
        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["request_headers"]["Authorization"] == "******"
        assert document["status_code"] == 500

    def test_request_body_editor_beautify(self, qt_app):
        from api_tester.viewers import RequestBodyEditor

        editor = RequestBodyEditor()
        editor.setPlainText('{"a":1}')
        assert editor.beautify()
        assert editor.toPlainText() == '{\n  "a": 1\n}'
        assert "Valid JSON" in editor.status.text()

    def test_request_body_editor_reports_invalid_json(self, qt_app):
        from api_tester.viewers import RequestBodyEditor

        editor = RequestBodyEditor()
        editor.setPlainText("{nope")
        assert not editor.beautify()
        assert editor.toPlainText() == "{nope"
        assert "Invalid JSON" in editor.status.text()

    def test_request_body_editor_minify(self, qt_app):
        from api_tester.viewers import RequestBodyEditor

        editor = RequestBodyEditor()
        editor.setPlainText('{\n  "a": 1\n}')
        assert editor.minify()
        assert editor.toPlainText() == '{"a":1}'

    def test_text_mode_disables_json_tools(self, qt_app):
        from api_tester.viewers import RequestBodyEditor, TEXT_MODE

        editor = RequestBodyEditor()
        editor.set_mode(TEXT_MODE)
        assert not editor.beautify_button.isEnabled()
        assert editor.status.text() == ""

    def test_highlighter_colours_keys(self, qt_app):
        from api_tester.viewers import JsonTextEdit

        editor = JsonTextEdit(read_only=True)
        editor.setPlainText(json.dumps({"name": "abc", "count": 2, "ok": True}, indent=2))
        block = editor.document().findBlockByLineNumber(1)
        assert block.layout() is not None
        assert block.layout().formats()

    def test_file_picker_emits_and_reports_path(self, qt_app, monkeypatch):
        from api_tester import viewers

        picker = viewers.FilePicker()
        seen = []
        picker.changed.connect(seen.append)
        monkeypatch.setattr(
            viewers.QFileDialog, "getOpenFileName", lambda *a, **k: (r"C:\tmp\a.xlsx", "")
        )
        assert picker.browse() == r"C:\tmp\a.xlsx"
        assert picker.text() == r"C:\tmp\a.xlsx"
        assert seen[-1] == r"C:\tmp\a.xlsx"

    def test_file_picker_cancel_leaves_value(self, qt_app, monkeypatch):
        from api_tester import viewers

        picker = viewers.FilePicker()
        picker.setText("keep.txt")
        monkeypatch.setattr(viewers.QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
        assert picker.browse() == ""
        assert picker.text() == "keep.txt"
