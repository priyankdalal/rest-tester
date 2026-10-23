import json
import threading
from copy import deepcopy
from dataclasses import replace

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from api_tester.catalog import Catalog, Endpoint, Service
from api_tester.catalog_builder import CatalogDocument, EndpointDraft, ServiceDraft
from api_tester.catalog_builder_ui import CatalogBuilderWindow
from api_tester.client import ApiResult, execute_endpoint
from api_tester.security import catalog_document, fingerprint


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def quiet_dialogs(monkeypatch):
    for method in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, method, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Discard)


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    from api_tester import main

    endpoint = Endpoint("one", "Demo", "Items", "List", "GET", "/items")
    other = replace(endpoint, id="two", path="/items/{id}")
    catalog = Catalog((Service("Demo", "", "", (endpoint, other)),), {}, {}, {})
    monkeypatch.setattr(main, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(main, "AI_SETTINGS_PATH", tmp_path / "ai.json")
    monkeypatch.setattr(main, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(main, "load_catalog", lambda *args, **kwargs: catalog)
    monkeypatch.setattr(main.MainWindow, "_check_ai_connection", lambda self: None)
    widget = main.MainWindow()
    widget._activate_endpoint("one")
    yield widget
    widget.app_settings.active_environment = next(iter(widget.app_settings.environments))
    widget.close()


@pytest.fixture
def captured_dialog(monkeypatch):
    reports = []

    class Dialog:
        def __init__(self, scan, parent=None, *, store=None, current_fingerprint=None):
            self.scan, self.store = scan, store

        def exec(self):
            report = self.scan(threading.Event())
            reports.append(report)
            if self.store is not None:
                self.store.save_security_report(report.to_dict())

    monkeypatch.setattr("api_tester.security_ui.SecurityReviewDialog", Dialog)
    return reports


@pytest.mark.parametrize("scope,count", [("endpoint", 1), ("module", 2), ("service", 2), ("catalog", 2)])
def test_explorer_scopes_use_shared_engine(window, captured_dialog, scope, count):
    window._review_catalog_security(scope)
    report = captured_dialog[-1]
    assert report.mode == "catalog"
    assert report.endpoints_reviewed == count
    assert window.workspace_store.security_report(report.id) == report.to_dict()


def test_explorer_security_action_uses_compact_header_semantics(window):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QToolButton

    button = window.security_review_button
    assert button.objectName() == "headerIconButton"
    assert button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert (button.width(), button.height()) == (32, 32)
    assert not button.icon().isNull()
    assert button.accessibleName() == "Security review options"
    assert button.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup
    labels = [action.text() for action in button.menu().actions() if not action.isSeparator()]
    assert labels == [
        "Selected endpoint", "Selected endpoint's module", "Selected endpoint's service",
        "Entire catalog", "Controlled verification...",
    ]
    window._refresh_header_buttons()
    assert not button.icon().isNull()


def test_response_review_uses_completed_exchange_not_current_editor(window, captured_dialog, monkeypatch):
    snapshot = catalog_document(window.catalog)
    window._request_catalog_snapshot = deepcopy(snapshot)
    window._request_environment = "Original environment"
    window._request_endpoint = window.current_endpoint
    result = ApiResult(
        True, 200, 1, "https://example.test/items", {"Content-Type": "text/plain"},
        "Traceback (most recent call last):", security_context={"verify_ssl": False},
    )
    # Completion has to be in the request environment to display this response.
    window.app_settings.environments["Original environment"] = deepcopy(window.app_settings.active)
    window.app_settings.environments["Changed environment"] = deepcopy(window.app_settings.active)
    window.app_settings.active_environment = "Original environment"
    window._request_completed(result)
    assert window.response.tabs.isTabEnabled(window.response.security_tab_index)
    window.payload.setPlainText('{"edited":true}')
    window.app_settings.active_environment = "Changed environment"
    def review():
        captured_dialog.append(window.response._security_scan(threading.Event()))
    monkeypatch.setattr(window.response.security_panel, "review", review)
    window.response.security_panel.review()
    report = captured_dialog[-1]
    assert report.environment == "Original environment"
    assert report.catalog_fingerprint == fingerprint(snapshot)
    assert any(finding.rule_id == "HTTP003" for finding in report.findings)
    assert result.passed is True
    window._activate_endpoint("two")
    assert not window.response.tabs.isTabEnabled(window.response.security_tab_index)


def test_response_failure_clears_old_security_action(window, captured_dialog):
    window.response.set_security_review(lambda event: None, window.workspace_store)
    window.response.show_error("Connection failed")
    assert not window.response.tabs.isTabEnabled(window.response.security_tab_index)
    assert not captured_dialog


def test_response_security_tab_is_permanent_and_initially_disabled(window):
    from PyQt6.QtWidgets import QPushButton

    response = window.response
    count = response.tabs.count()
    index = response.security_tab_index
    assert response.tabs.tabText(index) == "Security"
    assert response.tabs.widget(index) is response.security_panel
    assert not response.tabs.isTabEnabled(index)
    assert not any(button.text() == "Analyze Security" for button in response.findChildren(QPushButton))
    response.set_security_review(lambda event: None, window.workspace_store)
    assert response.tabs.isTabEnabled(index)
    response.tabs.setCurrentIndex(index)
    response.clear()
    assert response.tabs.count() == count
    assert not response.tabs.isTabEnabled(index)
    assert response.tabs.currentIndex() != index
    response.set_security_review(lambda event: None, window.workspace_store)
    assert response.tabs.count() == count
    response.show_error("No HTTP response")
    assert not response.tabs.isTabEnabled(index)


@pytest.mark.parametrize("send_method,expected", [
    ("_send_request", "200-299"), ("_send_without_verification", "100-599"),
])
def test_request_start_resets_security_without_removed_button(window, monkeypatch, send_method, expected):
    from unittest.mock import Mock
    from api_tester import main

    thread = Mock()
    worker = Mock()
    monkeypatch.setattr(main, "QThread", lambda: thread)
    factory = Mock(return_value=worker)
    monkeypatch.setattr(main, "RequestWorker", factory)
    window.base_url_inputs["Demo"].setText("https://example.test")
    window.expected_status.setCurrentText("200-299")
    window.response.set_security_review(lambda event: None, window.workspace_store)
    assert window.response.tabs.isTabEnabled(window.response.security_tab_index)
    getattr(window, send_method)()
    assert not window.response.tabs.isTabEnabled(window.response.security_tab_index)
    assert window.response._security_scan is None
    assert window.response.status_label.text() == "Running..."
    assert factory.call_args.args[0][6] == expected
    thread.start.assert_called_once()
    window._request_thread = None
    window._request_worker = None


def test_all_shared_response_views_have_no_analyze_security_button(window):
    from PyQt6.QtWidgets import QPushButton

    for response in (window.response, window.suite_tab.case_response):
        assert not hasattr(response, "security_button")
        assert response.tabs.tabText(response.security_tab_index) == "Security"
        assert not any(button.text() == "Analyze Security" for button in response.findChildren(QPushButton))
    assert not any(
        button.text() == "Analyze Security"
        for button in window.collections_page.handler.findChildren(QPushButton)
    )


def test_catalog_builder_scans_draft_without_saving(app, captured_dialog):
    builder = CatalogBuilderWindow()
    builder.document = CatalogDocument(services=[
        ServiceDraft("Demo", endpoints=[EndpointDraft("Demo", "Items", "List", path="/items")]),
    ])
    builder.security_review()
    assert captured_dialog[-1].scope == "Catalog Builder draft"
    assert captured_dialog[-1].endpoints_reviewed == 1
    assert builder.document.path is None
    builder.close()


def test_authored_metadata_edits_correct_selected_target(app, monkeypatch):
    builder = CatalogBuilderWindow()
    service = ServiceDraft("Demo", endpoints=[EndpointDraft("Demo", "Items", "List", path="/items")])
    builder.document = CatalogDocument(services=[service])
    builder._populate()
    endpoint = service.endpoints[0]
    builder._select_node("endpoint", ("Demo", endpoint.id))
    class Metadata:
        def __init__(self, metadata, parent, *, service=False):
            self.service = service

        def exec(self):
            return QDialog.DialogCode.Accepted

        def result_metadata(self):
            return {"document": {"openapi": "3.1.0", "paths": {}}} if self.service else {
                "require_authentication": True, "sensitive_response_fields": ["secretData"],
            }
    monkeypatch.setattr("api_tester.security_metadata_ui.SecurityMetadataDialog", Metadata)
    builder.edit_security_metadata()
    assert endpoint.security_metadata["require_authentication"] is True
    builder._select_node("service", "Demo")
    builder.edit_security_metadata()
    assert service.security_metadata["document"]["openapi"] == "3.1.0"
    assert "document" not in endpoint.security_metadata
    builder.dirty = False
    builder.close()


def test_cancelled_guided_metadata_leaves_draft_unchanged(app, monkeypatch):
    builder = CatalogBuilderWindow()
    service = ServiceDraft("Demo", endpoints=[EndpointDraft("Demo", "Items", "List", path="/items")])
    builder.document = CatalogDocument(services=[service])
    builder._populate()
    builder._select_node("endpoint", ("Demo", service.endpoints[0].id))
    class Metadata:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected
    monkeypatch.setattr("api_tester.security_metadata_ui.SecurityMetadataDialog", Metadata)
    builder.edit_security_metadata()
    assert not service.endpoints[0].security_metadata
    builder.close()


def test_actual_http_exchange_captures_security_context_without_credentials():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        endpoint = Endpoint("demo", "Demo", "Items", "List", "GET", "/items")
        result = execute_endpoint(
            endpoint, f"http://127.0.0.1:{server.server_port}", "", "private-key",
            {}, None, "200", probe_connection=False,
        )
        assert result.security_context["credential_present"] is True
        assert result.security_context["verify_ssl"] is True
        assert "private-key" not in json.dumps(result.security_context)
        assert result.passed is True
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_automatic_reviews_are_separately_opt_in(window, monkeypatch, tmp_path):
    configured = []

    def configure(panel, scan, **options):
        configured.append((scan(threading.Event()), options))

    monkeypatch.setattr("api_tester.security_ui.SecurityReportPanel.configure", configure)
    assert window._load_catalog_from(tmp_path / "catalog.json")
    assert not any(options.get("auto") for _, options in configured)
    window.app_settings.security_auto_catalog = True
    assert window._load_catalog_from(tmp_path / "catalog.json")
    assert any(options.get("auto") and report.mode == "catalog" for report, options in configured)
    configured.clear()
    window._activate_endpoint("one")
    window.app_settings.security_auto_response = True
    window._request_catalog_snapshot = catalog_document(window.catalog)
    window._request_environment = window.app_settings.active_environment
    window._request_endpoint = window.current_endpoint
    window._request_completed(ApiResult(
        True, 200, 1, "https://example.test/items", {"Content-Type": "application/json"}, "{}",
    ))
    assert any(options.get("auto") and report.mode == "passive response" for report, options in configured)


def test_suite_security_uses_frozen_run_evidence(window):
    from api_tester.runner import CaseResult

    tab = window.suite_tab
    original = window.current_endpoint
    tab._security_catalog_snapshot = catalog_document(window.catalog)
    tab._security_endpoints = {original.id: deepcopy(original)}
    tab._security_environment = "Original run environment"
    tab.endpoints[original.id] = replace(original, path="/changed-after-run")
    result = CaseResult(
        "case", "Synthetic", original.id, "GET", original.path, original.service, True,
        status_code=200, url="https://example.test/items",
        response_body="Traceback (most recent call last):",
        security_context={"verify_ssl": False},
    )
    tab._show_case_result(result)
    report = tab.case_response._security_scan(threading.Event())
    assert report.environment == "Original run environment"
    assert report.catalog_fingerprint == fingerprint(tab._security_catalog_snapshot)
    assert any(finding.path == "/items" and finding.rule_id == "HTTP003" for finding in report.findings)
    assert result.passed
