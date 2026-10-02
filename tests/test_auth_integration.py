import pytest
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication

from api_tester.authentication import AuthBinding, AuthProfile
from api_tester.catalog import Catalog, Endpoint, Service
from api_tester.request_auth import AuthenticationContext
from api_tester.saved_requests import SavedRequest


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    import api_tester.main as module

    monkeypatch.setattr(module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(module, "SUITES_PATH", tmp_path / "suites", raising=False)
    endpoints = tuple(
        Endpoint(id=f"test.{n}", service="Test", controller="Items", action=f"Get{n}",
                 method="GET", path=f"/items/{n}") for n in range(2)
    )
    catalog = Catalog((Service("Test", "Test", "https://example.test", endpoints),), {}, {}, {})
    monkeypatch.setattr(module, "load_catalog", lambda *args, **kwargs: catalog)
    widget = module.MainWindow()
    widget.app_settings.active.auth_profiles["identity"] = AuthProfile(
        id="identity", name="User", method="manual_bearer",
    )
    widget.app_settings.active.auth_bindings["Test"] = AuthBinding(identity="identity")
    widget._sync_compatibility_controls()
    widget.endpoint_tree.setCurrentItem(widget.endpoint_items[endpoints[0].id])
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def choose(combo, value):
    index = combo.findData(value)
    assert index >= 0
    combo.setCurrentIndex(index)


def test_explorer_preview_is_redacted_and_never_requests_a_token(window, monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("Preview attempted token acquisition")

    monkeypatch.setattr("api_tester.authentication.AuthenticationManager.headers", fail)
    assert window._prepared_request().headers["Authorization"] == "******"
    choose(window.request_authentication, "none")
    assert "Authorization" not in window._prepared_request().headers


def test_auth_override_is_preserved_per_endpoint_draft(window):
    first, second = list(window.endpoint_items.values())
    window.endpoint_tree.setCurrentItem(first)
    choose(window.request_authentication, "none")
    window.endpoint_tree.setCurrentItem(second)
    assert window.request_authentication.currentData() == "inherit"
    window.endpoint_tree.setCurrentItem(first)
    assert window.request_authentication.currentData() == "none"


def test_saved_request_and_collection_preserve_authentication(window):
    endpoint = window.current_endpoint
    request = SavedRequest(endpoint.id, "No credentials", authentication="none")
    window.saved_request_store.upsert_request(request)
    window._open_saved_request(request.id)
    assert window.request_authentication.currentData() == "none"
    assert window._collection_cases([request.id])[0].authentication == "none"


def test_suite_case_selector_updates_case_and_retains_missing_profile(window):
    tab = window.suite_tab
    tab.add_case_for_endpoint(window.current_endpoint, {}, None, "Auth case")
    choose(tab.case_authentication, "identity")
    assert tab.current_case.authentication == "identity"
    window.app_settings.active.auth_profiles.clear()
    tab.refresh_authentication_choices()
    assert tab.case_authentication.currentData() == "identity"
    assert "Missing profile" in tab.case_authentication.currentText()


def test_add_to_suite_preserves_explorer_override(window, monkeypatch):
    choose(window.request_authentication, "none")
    monkeypatch.setattr(window.suite_tab, "prompt_case_name", lambda *args: "Negative")
    window._add_to_suite()
    assert window.suite_tab.current_case.authentication == "none"
    assert window.suite_tab.case_authentication.currentData() == "none"


def test_environment_context_is_an_independent_configuration_snapshot(window):
    context = window._environment()["auth_context"]
    assert isinstance(context, AuthenticationContext)
    window.app_settings.active.auth_bindings["Test"].identity = "changed"
    assert context.bindings["Test"].identity == "identity"


def test_request_worker_passes_authentication_context_and_selection(app, monkeypatch):
    import api_tester.main as module

    calls = []
    context = object()
    monkeypatch.setattr(module, "execute_endpoint", lambda *args, **kwargs: calls.append((args, kwargs)))
    worker = module.RequestWorker(("argument",), context, "none")
    worker.run()
    assert calls == [(("argument",), {"auth_context": context, "auth_mode": "none"})]
