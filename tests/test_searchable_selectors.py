"""Debounced searchable selectors in Load Studio and the API Explorer
"Open in Load Studio" split-button action."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication, QComboBox, QMessageBox, QToolButton

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.execution.models import ExecutionEnvironmentSnapshot
from api_tester.load_testing.ui import LoadTestingTab
from api_tester.widgets import SEARCH_DEBOUNCE_MS, SEARCH_TEXT_ROLE, SearchableComboBox


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait(app: QApplication, ms: int) -> None:
    remaining = ms
    while remaining > 0:
        app.processEvents()
        QThread.msleep(10)
        remaining -= 10
    app.processEvents()


def _type(combo: SearchableComboBox, text: str) -> None:
    # textEdited only fires for user edits, so emit it the way typing would.
    combo.lineEdit().setText(text)
    combo.lineEdit().textEdited.emit(text)


def _endpoint(endpoint_id: str, service: str, controller: str, method: str, path: str) -> Endpoint:
    return Endpoint(
        id=endpoint_id,
        service=service,
        controller=controller,
        action=method.title(),
        method=method,
        path=path,
        parameters=(
            Parameter(name="Id", source="path", type="int", required=True),
            Parameter(name="PageSize", source="query", type="int", required=False),
        ),
        payload={"name": "seed"} if method in {"POST", "PUT"} else None,
    )


def _catalog() -> Catalog:
    auth = (
        _endpoint("brands.get", "TrialAuth", "Brands", "GET", "/Brands/{Id}"),
        _endpoint("brands.post", "TrialAuth", "Brands", "POST", "/Brands"),
        _endpoint("users.get", "TrialAuth", "Users", "GET", "/Users/{Id}"),
    )
    library = (_endpoint("crops.get", "TrialLibrary", "Crops", "GET", "/Crops/{Id}"),)
    return Catalog(
        services=(
            Service("TrialAuth", "TrialAuth", "https://auth.test", auth),
            Service("TrialLibrary", "TrialLibrary", "https://lib.test", library),
        ),
        filter_schemas={},
        payload_schemas={},
        enums={},
    )


def _tab() -> LoadTestingTab:
    return LoadTestingTab(
        _catalog(),
        lambda: ExecutionEnvironmentSnapshot(environment_id="e", environment_name="QA"),
    )


def test_selectors_are_searchable_combos(app: QApplication) -> None:
    tab = _tab()
    for combo in (tab.service_combo, tab.endpoint_combo):
        assert isinstance(combo, SearchableComboBox)
        assert combo.isEditable()
        assert combo.insertPolicy() == QComboBox.InsertPolicy.NoInsert
        assert combo.completer() is None
        assert 300 <= combo.search_timer.interval() <= 500
    assert SEARCH_DEBOUNCE_MS == tab.endpoint_combo.search_timer.interval()
    assert "controller" in tab.endpoint_combo.lineEdit().placeholderText()


def test_search_waits_for_debounce_and_restarts_per_keystroke(app: QApplication) -> None:
    tab = _tab()
    combo = tab.endpoint_combo
    _type(combo, "user")
    assert combo.search_timer.isActive()
    assert combo.suggestion_count() == 3  # not filtered yet
    _wait(app, 150)
    _type(combo, "users")  # another keystroke restarts the timer
    _wait(app, 200)
    assert combo.suggestion_count() == 3
    _wait(app, SEARCH_DEBOUNCE_MS)
    assert not combo.search_timer.isActive()
    assert combo.suggestion_texts() == ["GET /Users/{Id}"]


def test_search_matches_every_token_case_insensitively(app: QApplication) -> None:
    tab = _tab()
    combo = tab.endpoint_combo
    _type(combo, "POST brands")
    combo.flush_search()
    assert combo.suggestion_texts() == ["POST /Brands"]
    _type(combo, "get BRANDS")
    combo.flush_search()
    assert combo.suggestion_texts() == ["GET /Brands/{Id}"]
    # Controller names are searchable even when absent from the route text.
    assert "Brands" in combo.itemData(0, SEARCH_TEXT_ROLE)
    _type(combo, "nothing-matches")
    combo.flush_search()
    assert combo.suggestion_count() == 0


def test_choosing_a_suggestion_selects_the_real_endpoint(app: QApplication) -> None:
    tab = _tab()
    combo = tab.endpoint_combo
    _type(combo, "users")
    combo.flush_search()
    combo.choose_suggestion(0)
    assert tab.current_endpoint is not None
    assert tab.current_endpoint.id == "users.get"
    assert combo.currentData() == "users.get"
    assert combo.lineEdit().text() == "GET /Users/{Id}"
    assert combo.suggestion_count() == 3  # filter cleared after choosing


def test_service_search_switches_service_without_firing_on_keystrokes(app: QApplication) -> None:
    tab = _tab()
    _type(tab.service_combo, "lib")
    # Typing alone must not re-populate endpoints (old currentTextChanged bug).
    assert tab.endpoint_combo.count() == 3
    tab.service_combo.flush_search()
    tab.service_combo.choose_suggestion(0)
    assert tab.service_combo.currentText() == "TrialLibrary"
    assert tab.endpoint_combo.count() == 1
    assert tab.current_endpoint.id == "crops.get"


def test_enter_picks_first_match_and_unmatched_text_reverts(app: QApplication) -> None:
    tab = _tab()
    combo = tab.endpoint_combo
    _type(combo, "post")
    combo.lineEdit().returnPressed.emit()
    assert tab.current_endpoint.id == "brands.post"
    _type(combo, "zzz")
    combo.lineEdit().returnPressed.emit()
    assert tab.current_endpoint.id == "brands.post"
    assert combo.lineEdit().text() == "POST /Brands"


def test_load_request_prefills_endpoint_values_and_payload(app: QApplication) -> None:
    tab = _tab()
    tab.steps.setCurrentIndex(1)
    assert tab.load_request(
        _catalog().services[1].endpoints[0],
        {"path:Id": "42", "query:PageSize": "10", "enabled:query:PageSize": "true"},
        None,
    )
    assert tab.service_combo.currentText() == "TrialLibrary"
    assert tab.current_endpoint.id == "crops.get"
    assert tab.parameters_table.item(0, 4).text() == "42"
    assert tab.parameters_table.item(1, 4).text() == "10"
    assert tab.steps.currentIndex() == 0

    post = _catalog().services[0].endpoints[1]
    assert tab.load_request(
        post,
        {"query:PageSize": "5", "enabled:query:PageSize": "false"},
        {"name": "Custom"},
    )
    assert tab.current_endpoint.id == "brands.post"
    assert tab.parameters_table.item(1, 4).text() == ""  # unticked in Explorer
    assert json.loads(tab.payload_editor.toPlainText()) == {"name": "Custom"}


def test_load_request_rejects_unknown_endpoint(app: QApplication) -> None:
    tab = _tab()
    unknown = _endpoint("x.get", "Other", "X", "GET", "/X")
    assert not tab.load_request(unknown, {}, None)
    assert tab.current_endpoint.id == "brands.get"


# ------------------------------------------------------------------ API Explorer


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
        )
    value = main_module.MainWindow()
    yield value
    value.close()
    value.deleteLater()


def test_add_to_suite_is_a_split_button_with_handoff_actions(window) -> None:
    button = window.add_to_suite_button
    assert isinstance(button, QToolButton)
    assert button.text() == "Add to Test Suite"
    assert button.popupMode() == QToolButton.ToolButtonPopupMode.MenuButtonPopup
    assert [action.text() for action in button.menu().actions()] == [
        "Open in Load Studio",
        "Open in Data Runner",
    ]
    assert all(not action.icon().isNull() for action in button.menu().actions())


def test_open_in_load_studio_carries_the_explorer_request(window) -> None:
    endpoint = next(
        (
            item
            for item in window.endpoints_by_id.values()
            if item.method in {"POST", "PUT"}
            and any(parameter.source == "query" for parameter in item.parameters)
        ),
        next(iter(window.endpoints_by_id.values())),
    )
    window.endpoint_tree.setCurrentItem(window.endpoint_items[endpoint.id])
    window.payload.setPlainText(json.dumps({"fromExplorer": True}))
    values = window._current_values()

    window.open_in_load_studio_action.trigger()

    load_tab = window.load_testing_tab
    assert window.workspace_tabs.currentWidget() is load_tab
    assert load_tab.current_endpoint.id == endpoint.id
    assert load_tab.endpoint_combo.currentData() == endpoint.id
    assert json.loads(load_tab.payload_editor.toPlainText()) == {"fromExplorer": True}
    for row in range(load_tab.parameters_table.rowCount()):
        key = f"{load_tab.parameters_table.item(row, 0).text()}:{load_tab.parameters_table.item(row, 1).text()}"
        if key in values and values.get(f"enabled:{key}") != "false":
            assert load_tab.parameters_table.item(row, 4).text() == values[key]


def test_open_in_load_studio_rejects_invalid_payload(window) -> None:
    endpoint = next(iter(window.endpoints_by_id.values()))
    window.endpoint_tree.setCurrentItem(window.endpoint_items[endpoint.id])
    window.workspace_tabs.setCurrentIndex(0)
    window.payload.setPlainText("{not json")
    window._open_in_load_studio()
    assert window.workspace_tabs.currentIndex() == 0


def test_open_in_data_runner_carries_the_explorer_request(window) -> None:
    endpoint = next(
        (item for item in window.endpoints_by_id.values() if item.method in {"POST", "PUT"}),
        next(iter(window.endpoints_by_id.values())),
    )
    window.endpoint_tree.setCurrentItem(window.endpoint_items[endpoint.id])
    window.payload.setPlainText(json.dumps({"fromExplorer": True}))
    values = window._current_values()

    window.open_in_data_runner_action.trigger()

    runner_tab = window.data_runner_tab
    assert window.workspace_tabs.currentWidget() is runner_tab
    assert runner_tab.current_endpoint.id == endpoint.id
    assert runner_tab.has_request_template()
    assert runner_tab._template_payload == {"fromExplorer": True}
    assert not runner_tab.template_banner.isHidden()
    for key, value in runner_tab._template_values.items():
        assert values[key] == value
        assert values.get(f"enabled:{key}") != "false"


def test_open_in_data_runner_rejects_invalid_payload(window) -> None:
    endpoint = next(iter(window.endpoints_by_id.values()))
    window.endpoint_tree.setCurrentItem(window.endpoint_items[endpoint.id])
    window.workspace_tabs.setCurrentIndex(0)
    window.payload.setPlainText("{not json")
    window._open_in_data_runner()
    assert window.workspace_tabs.currentIndex() == 0
    assert not window.data_runner_tab.has_request_template()


# ------------------------------------------------------------------ real-input regressions
# An editable QComboBox keeps focus itself and forwards focus/key events to
# its line edit directly, bypassing event filters; these drive the combo the
# way Qt does for real input rather than via programmatic setText.


def _focus_in(app: QApplication, combo: SearchableComboBox) -> None:
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QFocusEvent

    QApplication.sendEvent(combo, QFocusEvent(QEvent.Type.FocusIn, Qt.FocusReason.MouseFocusReason))
    _wait(app, 30)


def test_focus_in_selects_all_so_typing_replaces_the_value(app: QApplication) -> None:
    tab = _tab()
    combo = tab.endpoint_combo
    combo.lineEdit().deselect()
    _focus_in(app, combo)
    assert combo.lineEdit().selectedText() == combo.lineEdit().text() == "GET /Brands/{Id}"


def test_focus_out_restores_the_current_item_text(app: QApplication) -> None:
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtGui import QFocusEvent

    tab = _tab()
    combo = tab.endpoint_combo
    _type(combo, "half-typed")
    QApplication.sendEvent(combo, QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.OtherFocusReason))
    assert combo.lineEdit().text() == "GET /Brands/{Id}"
    assert tab.current_endpoint.id == "brands.get"


def test_escape_on_the_combo_restores_the_text(app: QApplication) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    tab = _tab()
    combo = tab.endpoint_combo
    _type(combo, "users")
    QTest.keyClick(combo, Qt.Key.Key_Escape)
    assert combo.lineEdit().text() == "GET /Brands/{Id}"
    assert tab.current_endpoint.id == "brands.get"


def test_stale_completer_activation_after_enter_is_ignored(app: QApplication) -> None:
    tab = _tab()
    combo = tab.endpoint_combo
    _type(combo, "post")
    combo.lineEdit().returnPressed.emit()
    assert tab.current_endpoint.id == "brands.post"
    # The completer then activates its (now unfiltered) row 0 in the same tick.
    combo._suggestion_chosen(combo._proxy.index(0, 0))
    assert tab.current_endpoint.id == "brands.post"
    _wait(app, 20)
    # Once the tick passes, a genuine pick works again.
    combo.choose_suggestion(0)
    assert tab.current_endpoint.id == "brands.get"
