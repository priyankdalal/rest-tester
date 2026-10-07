from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QLineEdit

from api_tester.authentication import AuthBinding, AuthError, AuthProfile
from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.client import execute_endpoint, generate_curl, generate_python, prepare_endpoint_request
from api_tester.headers import merge_headers
from api_tester.headers_ui import HeadersEditor
from api_tester.request_auth import AuthenticationContext
from api_tester.request_editor import RequestEditor
from api_tester.runner import run_case
from api_tester.saved_requests import SavedRequest
from api_tester.suite import TestCase as Case, TestSuite as Suite, load_suite, save_suite


@pytest.fixture
def endpoint():
    return Endpoint("demo", "Demo", "Items", "Get", "GET", "/items")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def transport(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status_code=200, content=b"{}", headers={"content-type": "application/json"},
            url=url, request=SimpleNamespace(headers=kwargs["headers"], body=None), reason="OK",
        )

    monkeypatch.setattr("api_tester.client.requests.request", request)
    monkeypatch.setattr("api_tester.client.requests.Session.request", lambda self, *args, **kwargs: request(*args, **kwargs))
    monkeypatch.setattr("api_tester.client.measure_connection", lambda *args: {})
    return calls


def test_case_insensitive_precedence_and_custom_headers(endpoint):
    request = prepare_endpoint_request(
        endpoint, "https://example.test", "", "",
        {"header:x-tenant": "request", "header:Accept": "text/plain"}, None,
        {"X-Tenant": "environment", "accept": "application/xml"},
    )
    assert request.headers == {"x-tenant": "request", "Accept": "text/plain"}
    assert merge_headers({"X": "a"}, {"x": "b"}) == {"x": "b"}


def test_disabled_override_preserves_environment_and_empty_custom_values(endpoint):
    request = prepare_endpoint_request(
        endpoint, "https://example.test", "", "",
        {"header:X-Tenant": "request", "enabled:header:X-Tenant": "false", "header:X-Empty": ""},
        None, {"x-tenant": "environment"},
    )
    assert request.headers["x-tenant"] == "environment"
    assert request.headers["X-Empty"] == ""


def test_required_catalog_header_uses_environment_or_request_value(endpoint):
    endpoint = replace(endpoint, parameters=(Parameter("X-Tenant", "header", "string", True),))
    request = prepare_endpoint_request(
        endpoint, "https://example.test", "", "", {"header:X-Tenant": ""}, None,
        {"x-tenant": "environment"},
    )
    assert request.headers["x-tenant"] == "environment"
    with pytest.raises(ValueError, match="Required header"):
        prepare_endpoint_request(endpoint, "https://example.test", "", "", {}, None)


@pytest.mark.parametrize("name,value", [
    ("Bad Name", "private-value"), ("Bad:Name", "private-value"), ("", "secret"),
    ("X-Test", "secret\r\nInjected: true"), ("X-Test", " private-value"),
    ("X-Test", "\u2603"), ("X-Test", "secret\n"),
    ("X-Test", "secret\x00"), ("X-Test", "secret\x7f"),
])
def test_invalid_headers_fail_without_echoing_values(endpoint, name, value):
    with pytest.raises(ValueError) as error:
        prepare_endpoint_request(endpoint, "https://example.test", "", "", {f"header:{name}": value}, None)
    assert value not in str(error.value)


def test_request_headers_are_sent_and_redacted_in_exports(endpoint, transport):
    values = {"header:X-Tenant": "tenant", "header:Authorization": "private-value"}
    result = execute_endpoint(endpoint, "https://example.test", "", "", values, None, "200", auth_mode="manual")
    assert result.passed
    assert transport[0]["headers"]["X-Tenant"] == "tenant"
    assert transport[0]["headers"]["Authorization"] == "private-value"
    assert result.request_headers["Authorization"] == "******"
    prepared = prepare_endpoint_request(endpoint, "https://example.test", "", "", values, None)
    for text in (generate_curl(prepared), generate_python(prepared)):
        assert "private-value" not in text
        assert "X-Tenant" in text


def test_managed_conflict_and_no_auth_apply_to_custom_headers(endpoint, transport):
    context = AuthenticationContext(
        "test", {"key": AuthProfile(id="key", method="api_key", header_name="X-Credential")},
        {"Demo": AuthBinding(api_key="key")}, {"Demo": "https://example.test"},
    )
    with pytest.raises(AuthError, match="conflicts"):
        execute_endpoint(
            endpoint, "https://example.test", "", "", {"header:x-credential": "private"}, None,
            "200", auth_context=context,
        )
    assert not transport
    execute_endpoint(
        endpoint, "https://example.test", "", "",
        {"header:X-Credential": "private", "header:Cookie": "private", "header:X-Trace": "ok"},
        None, "200", auth_context=context, auth_mode="none",
    )
    assert transport[0]["headers"] == {"Accept": "application/json", "X-Trace": "ok"}


def test_suite_roundtrip_and_execution_keep_custom_headers(endpoint, transport, tmp_path):
    case = Case(endpoint.id, values={"header:X-Tenant": "{{tenant}}", "header:X-Skip": "unused",
                                   "enabled:header:X-Skip": "false"})
    path = tmp_path / "suite.json"
    save_suite(Suite(cases=[case]), path)
    loaded = load_suite(path).cases[0]
    assert loaded.values == case.values
    result = run_case(
        loaded, endpoint, "https://example.test", "", "", {"tenant": "resolved"},
        custom_headers={"x-tenant": "default"},
    )
    assert result.passed
    assert transport[0]["headers"]["X-Tenant"] == "resolved"
    assert "X-Skip" not in transport[0]["headers"]


def test_saved_requests_preserve_nonsecret_header_values_and_toggles(endpoint):
    saved = SavedRequest(endpoint.id, "Custom", values={
        "header:X-Tenant": "{{tenant}}", "header:X-Trace": "trace",
        "enabled:header:X-Trace": "false", "header:Authorization": "private",
    })
    restored = SavedRequest.from_dict(saved.to_dict())
    assert restored.values["header:X-Tenant"] == "{{tenant}}"
    assert restored.values["enabled:header:X-Trace"] == "false"
    assert "header:Authorization" not in restored.values


def test_editor_loads_catalog_headers_and_preserves_overrides(app, endpoint):
    endpoint = replace(endpoint, parameters=(Parameter("X-Required", "header", "string", True),))
    editor = RequestEditor()
    values = {"header:X-Required": "id", "header:X-Custom": "custom", "enabled:header:X-Custom": "false"}
    editor.load(Catalog((), {}, {}, {}), endpoint, values)
    assert editor.request_tabs.tabText(editor.headers_tab_index) == "Headers"
    assert editor.headers_editor.table.rowCount() == 2
    assert editor.values()["header:X-Required"] == "id"
    assert editor.values()["enabled:header:X-Custom"] == "false"
    editor.load(Catalog((), {}, {}, {}), None)
    assert not editor.request_tabs.isTabEnabled(editor.headers_tab_index)


def test_preview_masking_sources_conflicts_and_variable_resolution(app, endpoint):
    editor = HeadersEditor()
    editor.load(endpoint, {"header:x-tenant": "{{tenant}}", "header:Authorization": "private"})
    editor.set_context(
        {"X-Tenant": "default", "X-Environment": "inherited"}, {"tenant": "resolved"},
        managed=frozenset({"authorization", "x-credential"}),
        secrets=frozenset({"authorization", "x-credential"}),
    )
    rows = {
        editor.effective.item(row, 0).text().lower():
        (editor.effective.item(row, 1).text(), editor.effective.item(row, 2).text())
        for row in range(editor.effective.rowCount())
    }
    assert rows["x-tenant"] == ("resolved", "Request")
    assert rows["x-environment"] == ("inherited", "Environment")
    assert rows["authorization"] == ("******", "Conflict")
    assert rows["x-credential"] == ("******", "Managed authentication")
    assert "Conflicts" in editor.warning.text()
    assert editor.table.cellWidget(1, 2).echoMode() == QLineEdit.EchoMode.Password
    assert "private" not in editor.warning.text()


def test_header_rows_remove_disable_and_last_row_wins(app, endpoint):
    editor = HeadersEditor()
    editor.load(endpoint, {})
    editor.add_row("X-Trace", "a")
    editor.add_row("x-trace", "b")
    assert editor.values()["header:x-trace"] == "b"
    assert "header:X-Trace" not in editor.values()
    editor.table.setCurrentCell(1, 1)
    editor.remove_selected()
    assert editor.values()["header:X-Trace"] == "a"
    editor.table.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    assert editor.values()["enabled:header:X-Trace"] == "false"
    editor.table.item(0, 1).setText("Cookie")
    assert editor.table.cellWidget(0, 2).echoMode() == QLineEdit.EchoMode.Password


@pytest.mark.parametrize("theme_name,font_size,width", [
    ("Light", 10, 640), ("Dark", 10, 1000), ("Light", 16, 1000), ("Dark", 16, 640),
])
def test_header_editor_layout_is_usable(app, endpoint, theme_name, font_size, width):
    from PyQt6.QtGui import QFont
    from api_tester import theme

    theme.apply_theme(app, theme_name)
    editor = HeadersEditor()
    editor.setFont(QFont("Segoe UI", font_size))
    editor.load(endpoint, {"header:X-Tenant": "tenant", "header:Authorization": "private"})
    editor.set_context({"X-Trace": "inherited"}, {})
    editor.resize(width, 750)
    editor.show()
    app.processEvents()
    assert editor.width() == width
    assert editor.add_button.width() >= editor.add_button.sizeHint().width()
    assert editor.delete_button.width() >= editor.delete_button.sizeHint().width()
    assert editor.table.viewport().height() > 60
    assert editor.effective.viewport().height() > 40
    assert editor.table.cellWidget(1, 2).echoMode() == QLineEdit.EchoMode.Password
    editor.close()


@pytest.fixture
def window(app, monkeypatch, tmp_path, endpoint):
    import api_tester.main as module

    second = replace(endpoint, id="second", path="/second")
    catalog = Catalog((Service("Demo", "Demo", "https://example.test", (endpoint, second)),), {}, {}, {})
    monkeypatch.setattr(module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(module, "AI_SETTINGS_PATH", tmp_path / "ai.json")
    monkeypatch.setattr(module, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(module, "load_catalog", lambda *args, **kwargs: catalog)
    monkeypatch.setattr(module.MainWindow, "_check_ai_connection", lambda self: None)
    widget = module.MainWindow()
    widget._activate_endpoint(endpoint.id)
    yield widget
    widget.close()


def test_explorer_drafts_saved_request_and_collection_keep_headers(window):
    window.request_editor.headers_editor.add_row("X-Tenant", "request")
    window.app_settings.active.custom_headers["x-tenant"] = "environment"
    assert window._prepared_request().headers["X-Tenant"] == "request"
    window._activate_endpoint("second")
    assert "header:X-Tenant" not in window._current_values()
    window._activate_endpoint("demo")
    assert window._current_values()["header:X-Tenant"] == "request"
    saved = SavedRequest("demo", "Saved", values=window._saved_request_values())
    window.saved_request_store.upsert_request(saved)
    window._open_saved_request(saved.id)
    assert window.request_editor.headers_editor.values()["header:X-Tenant"] == "request"
    assert window._collection_cases([saved.id])[0].values["header:X-Tenant"] == "request"


def test_explorer_resolves_environment_header_variables(window):
    window.app_settings.active.variables["tenant"] = "resolved"
    window.app_settings.active.custom_headers["X-Tenant"] = "{{tenant}}"
    assert window._prepared_request().headers["X-Tenant"] == "resolved"
    assert window._environment()["custom_headers"]["X-Tenant"] == "resolved"


def test_data_and_load_handoff_preserve_custom_headers(window):
    window.request_editor.headers_editor.add_row("X-Tenant", "{{tenant}}")
    window.request_editor.headers_editor.add_row("X-Empty", "")
    window.request_editor.headers_editor.add_row("X-Skip", "unused", enabled=False)
    window._open_in_data_runner()
    assert window.data_runner_tab._template_values["header:X-Tenant"] == "{{tenant}}"
    assert window.data_runner_tab._template_values["header:X-Empty"] == ""
    assert "header:X-Skip" not in window.data_runner_tab._template_values
    window._open_in_load_studio()
    scenario = window.load_testing_tab._build_scenario()
    assert scenario.template.values["header:X-Tenant"] == "{{tenant}}"
    assert scenario.template.values["header:X-Empty"] == ""
    assert scenario.template.values["enabled:header:X-Skip"] == "false"


def test_worker_transport_resolves_and_sends_template_headers(endpoint, transport):
    from api_tester.execution.models import ExecutionEnvironmentSnapshot, RequestTemplate
    from api_tester.execution.transport import WorkerTransport

    environment = ExecutionEnvironmentSnapshot(
        "test", "Test", {"Demo": "https://example.test"},
        variables={"tenant": "resolved"}, custom_headers={"x-tenant": "default"},
    )
    template = RequestTemplate(
        endpoint.id, endpoint.service, endpoint.method, endpoint.path,
        values={"header:X-Tenant": "{{tenant}}"},
    )
    with WorkerTransport(environment, 0, "test") as worker:
        result, sample, error = worker.execute(endpoint, template)
    assert error is None
    assert result.passed
    assert transport[0]["headers"]["X-Tenant"] == "resolved"


def test_suite_editor_handoff_and_removal_updates_case(window, monkeypatch):
    window.request_editor.headers_editor.add_row("X-Tenant", "request")
    monkeypatch.setattr(window.suite_tab, "prompt_case_name", lambda *args: "Headers")
    window._add_to_suite()
    case = window.suite_tab.current_case
    assert case.values["header:X-Tenant"] == "request"
    editor = window.suite_tab.request_editor.headers_editor
    editor.table.setCurrentCell(0, 1)
    editor.remove_selected()
    assert "header:X-Tenant" not in case.values
    assert "enabled:header:X-Tenant" not in case.values


def test_saved_values_do_not_persist_custom_managed_credentials(window):
    window.app_settings.active.auth_profiles["key"] = AuthProfile(
        id="key", method="api_key", header_name="X-Credential",
    )
    window.request_editor.headers_editor.add_row("X-Credential", "private")
    assert "header:X-Credential" not in window._saved_request_values()
