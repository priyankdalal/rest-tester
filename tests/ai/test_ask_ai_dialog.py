"""The Ask AI dialog and its hand-off into API Explorer, driven by a fake provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QCoreApplication
from PyQt6.QtWidgets import QApplication

from api_tester.ai import controller
from api_tester.ai.config import AiSettings, save_ai_settings
from api_tester.ai.provider import LlmResponse
from api_tester.ai.providers.fake import FakeProvider
from api_tester.ai.request_plan import ExplorerRequest
from api_tester.ai.ui import AskAiDialog
from api_tester.catalog import load_catalog

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_LIST = "f76aeb49ca37"


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_for(predicate, attempts: int = 400) -> None:
    for _ in range(attempts):
        QCoreApplication.processEvents()
        if predicate():
            return
        QCoreApplication.processEvents()
        import time

        time.sleep(0.01)
    raise AssertionError("condition not reached")


@pytest.mark.parametrize("existing_settings", [False, True])
def test_dialog_plans_previews_and_opens(qt_app, monkeypatch, tmp_path, existing_settings):
    plan = {
        "status": "plan", "title": "Active products by price", "summary": "Lists active products.",
        "endpoint_id": PRODUCT_LIST, "parameters": [{"name": "PageSize", "value": "5"}],
        "filters": [{"field": "status", "op": "eq", "values": ["active"]}], "filter_join": "and",
        "sort": [{"field": "Price", "descending": True}], "payload": None, "expected_status": "200-299",
        "assumptions": ["Most expensive first."], "warnings": [], "question": "", "choices": [],
    }
    fake = FakeProvider([LlmResponse(text=json.dumps(plan), json=plan, input_tokens=900, output_tokens=120, model="fake")])
    monkeypatch.setattr(controller, "build_provider", lambda _settings: fake)
    catalog = load_catalog(ROOT / "examples" / "store_catalog.json")
    opened: list[ExplorerRequest] = []
    settings_path = tmp_path / "ai_settings.json"
    if existing_settings:
        save_ai_settings(settings_path, AiSettings())
    original_settings = settings_path.read_bytes() if existing_settings else None
    dialog = AskAiDialog(
        None,
        catalog=lambda: catalog,
        secrets=lambda: ("super-secret-token",),
        on_open=opened.append,
        settings_path=settings_path,
    )
    try:
        assert not dialog.open_button.isEnabled()
        dialog.prompt_input.setPlainText("top 5 active products, most expensive first, token super-secret-token")
        dialog.ask()
        _wait_for(lambda: dialog._task is None and dialog.outcome is not None)

        preview = dialog.result_view.toPlainText()
        assert "Active products by price" in preview
        assert "Status equals Active" in preview
        assert "Price descending" in preview
        assert "Status__eq:=Active" in preview
        assert "fake" in dialog.meta_label.text()
        assert "super-secret-token" not in json.dumps([m.content for m in fake.requests[0]["messages"]])

        assert dialog.open_button.isEnabled()
        dialog.open_button.click()
        assert opened[0].endpoint_id == PRODUCT_LIST
        assert opened[0].values["query:Filter"] == "Status__eq:=Active"
        assert opened[0].values["query:Sort"] == "Price-"
        if existing_settings:
            assert settings_path.read_bytes() == original_settings
        else:
            assert not settings_path.exists()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_dialog_shows_provider_failures(qt_app, monkeypatch, tmp_path):
    from api_tester.ai.provider import LlmConnectionError

    monkeypatch.setattr(
        controller, "build_provider", lambda _settings: FakeProvider([LlmConnectionError("Ollama is not running")])
    )
    catalog = load_catalog(ROOT / "examples" / "store_catalog.json")
    dialog = AskAiDialog(
        None, catalog=lambda: catalog, secrets=lambda: (), on_open=lambda _r: None,
        settings_path=tmp_path / "ai_settings.json",
    )
    try:
        dialog.prompt_input.setPlainText("list products")
        dialog.ask()
        _wait_for(lambda: dialog._task is None)
        assert "Ollama is not running" in dialog.result_view.toPlainText()
        assert not dialog.open_button.isEnabled()
        assert dialog.ask_button.isEnabled()
    finally:
        dialog.close()
        dialog.deleteLater()


def test_main_window_opens_an_ai_request_as_an_explorer_draft(qt_app, monkeypatch, tmp_path):
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(main_module, "AI_SETTINGS_PATH", tmp_path / "ai_settings.json")
    connection_checks = []
    monkeypatch.setattr(main_module.MainWindow, "_check_ai_connection", lambda _self: connection_checks.append(True))
    window = main_module.MainWindow()
    try:
        assert window.ask_ai_button.accessibleName() == "Ask AI"
        assert "Ask AI (Ctrl+K)" in window.ask_ai_button.toolTip()
        endpoint = next(
            (
                item
                for service in window.catalog.services
                for item in service.endpoints
                if item.method == "GET" and any(p.name == "PageSize" for p in item.parameters)
            ),
            None,
        )
        if endpoint is None:
            pytest.skip("loaded catalog has no list endpoint")
        window.open_ai_request(
            ExplorerRequest(endpoint.id, {"query:PageSize": "7", "enabled:query:PageSize": "true"}, None, "200")
        )
        assert window.current_endpoint is endpoint or window.current_endpoint.id == endpoint.id
        assert window.request_editor.values().get("query:PageSize") == "7"
        assert window.expected_status.currentText() == "200"

        settings_opened = []
        monkeypatch.setattr(window, "_open_settings", settings_opened.append)
        window.ask_ai_button.set_available(False, "Offline test")
        previous_checks = len(connection_checks)
        window._show_ask_ai()
        assert settings_opened == ["AI Settings"]
        assert len(connection_checks) == previous_checks + 1

        window.ask_ai_button.set_available(True, "Fake provider ready")
        window._show_ask_ai()
        assert window.ask_ai_dialog.isVisible()
        assert settings_opened == ["AI Settings"]
        window.ask_ai_dialog.close()
    finally:
        window.close()
        window.deleteLater()
