"""The inspector dialog must show real values and real events.

These drive the dialog against a live ``AuthenticationManager`` so the
displayed value is the same one the request layer would send, rather than
something the dialog reformatted on its own.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QApplication

from tests.test_authentication_ui import make_profile, qt_app  # noqa: F401

from api_tester import auth_log
from api_tester.authentication import AuthenticationManager, AuthProfile
from api_tester.auth_inspector_ui import AuthInspectorDialog
from api_tester.authentication_ui import AuthenticationSettings


@pytest.fixture(name="app")
def _app(qt_app):  # noqa: F811
    return qt_app


@pytest.fixture(name="settings")
def _settings(app, tmp_path):
    log = auth_log.AuthActivityLog()
    manager = AuthenticationManager(cache_root=tmp_path, activity_log=log)
    profile = make_profile(base_urls={"TrialAuth": "https://a.example"})
    key = AuthProfile(
        id="key", name="Gateway key", method="api_key", header_name="x-api-key"
    )
    bearer = AuthProfile(id="tok", name="Manual token", method="manual_bearer")
    profile.auth_profiles = {"key": key, "tok": bearer}
    manager.set_secrets(profile.id, key, {"key": "super-secret-key"})
    manager.set_secrets(profile.id, bearer, {"token": "raw-token-value"})
    widget = AuthenticationSettings(profile, ["TrialAuth"], manager=manager)
    try:
        yield widget, manager, log
    finally:
        widget.stop()


@pytest.fixture(name="dialog")
def _dialog(settings):
    widget, _manager, _log = settings
    instance = AuthInspectorDialog(widget)
    try:
        yield instance
    finally:
        instance.close()
        instance.deleteLater()


def test_every_profile_is_offered_for_inspection(dialog) -> None:
    labels = [
        dialog.profile_picker.itemText(i)
        for i in range(dialog.profile_picker.count())
    ]
    assert labels == ["Gateway key", "Manual token"]


def test_the_value_shown_is_the_unmasked_credential(dialog) -> None:
    """The user explicitly asked for no masking in this view."""
    dialog.profile_picker.setCurrentIndex(0)
    assert dialog.values_table.rowCount() == 1
    assert dialog.values_table.item(0, 0).text() == "header"
    assert dialog.values_table.item(0, 1).text() == "x-api-key"
    assert dialog.values_table.item(0, 2).text() == "super-secret-key"


def test_switching_profiles_replaces_the_displayed_value(dialog) -> None:
    dialog.profile_picker.setCurrentIndex(0)
    dialog.profile_picker.setCurrentIndex(1)
    assert dialog.values_table.item(0, 1).text() == "Authorization"
    assert "raw-token-value" in dialog.values_table.item(0, 2).text()


def test_copy_puts_the_raw_value_on_the_clipboard(dialog) -> None:
    dialog.profile_picker.setCurrentIndex(0)
    dialog.values_table.setCurrentCell(0, 2)
    dialog._copy_value()
    assert QApplication.clipboard().text() == "super-secret-key"


def test_copy_with_nothing_selected_is_harmless(dialog) -> None:
    dialog.values_table.setCurrentCell(-1, -1)
    dialog._copy_value()


def test_a_broken_profile_reports_the_error_rather_than_a_value(
    settings, dialog
) -> None:
    widget, _manager, _log = settings
    widget.profiles["key"].header_name = "x-api-key"
    widget.profiles["broken"] = AuthProfile(
        id="broken", name="No secret", method="api_key", header_name="x-api-key"
    )
    dialog._refresh_profiles()
    dialog.profile_picker.setCurrentIndex(
        dialog.profile_picker.findData("broken")
    )
    assert dialog.values_table.rowCount() == 0
    assert dialog.status_label.text().strip()


def test_inspecting_writes_to_the_activity_log(settings, dialog) -> None:
    _widget, _manager, log = settings
    log.clear()
    dialog.profile_picker.setCurrentIndex(0)
    dialog._inspect()
    assert log.entries()[0].event == auth_log.APPLIED


def test_the_log_tab_renders_the_managers_events(settings, dialog) -> None:
    """Reading the global log instead of the manager's would show nothing."""
    _widget, _manager, log = settings
    log.clear()
    log.add(auth_log.SIGNED_IN, "Gateway key", "api_key", detail="done")
    dialog._refresh_log()
    assert dialog.log_table.rowCount() == 1
    assert dialog.log_table.item(0, 1).text() == auth_log.SIGNED_IN
    assert dialog.log_table.item(0, 2).text() == "Gateway key"


def test_the_log_never_renders_a_credential_value(settings, dialog) -> None:
    _widget, _manager, log = settings
    log.clear()
    dialog.profile_picker.setCurrentIndex(0)
    dialog._inspect()
    dialog._refresh_log()
    rendered = " ".join(
        dialog.log_table.item(row, column).text()
        for row in range(dialog.log_table.rowCount())
        for column in range(dialog.log_table.columnCount())
    )
    assert "super-secret-key" not in rendered
    assert "x-api-key" in rendered


def test_unfollowing_freezes_the_log_view(settings, dialog) -> None:
    _widget, _manager, log = settings
    log.clear()
    dialog._refresh_log()
    dialog.follow_box.setChecked(False)
    log.add(auth_log.SIGNED_IN, "Gateway key", "api_key")
    dialog._refresh_log()
    assert dialog.log_table.rowCount() == 0


def test_clearing_empties_both_the_log_and_the_table(settings, dialog) -> None:
    _widget, _manager, log = settings
    log.add(auth_log.SIGNED_IN, "Gateway key", "api_key")
    dialog._clear_log()
    assert dialog.log_table.rowCount() == 0
    assert log.entries() == []


def test_closing_stops_the_follow_timer(settings) -> None:
    widget, _manager, _log = settings
    instance = AuthInspectorDialog(widget)
    assert instance._timer.isActive()
    instance.close()
    assert not instance._timer.isActive()


def test_the_settings_page_can_open_the_inspector(settings, monkeypatch) -> None:
    widget, _manager, _log = settings
    opened = []
    monkeypatch.setattr(AuthInspectorDialog, "exec", lambda self: opened.append(self))
    widget.inspect_button.click()
    assert len(opened) == 1
    opened[0].close()


def test_selecting_an_event_shows_its_full_detail(settings, dialog) -> None:
    _widget, _manager, log = settings
    log.clear()
    log.add(
        auth_log.HTTP, "Partner OAuth", "oauth2", environment="dev",
        http_method="POST", url="https://id.example.com/token", status=200,
        sent_fields=("grant_type", "client_secret"), duration_ms=42.0,
        detail="grant client_credentials",
    )
    dialog._refresh_log()
    dialog.log_table.setCurrentCell(0, 0)

    text = dialog.log_detail.toPlainText()
    assert "POST https://id.example.com/token" in text
    assert "status:      200" in text
    assert "42 ms" in text
    assert "grant_type, client_secret" in text
    assert "environment: dev" in text


def test_the_http_status_has_its_own_column(settings, dialog) -> None:
    _widget, _manager, log = settings
    log.clear()
    log.add(auth_log.HTTP, "P", "oauth2", http_method="POST",
            url="https://id.example.com/token", status=401, ok=False)
    dialog._refresh_log()
    assert dialog.log_table.item(0, 4).text() == "401"


def test_copy_log_produces_pasteable_text_without_values(settings, dialog) -> None:
    _widget, _manager, log = settings
    log.clear()
    dialog.profile_picker.setCurrentIndex(0)
    dialog._inspect()
    dialog._refresh_log()
    dialog._copy_log()

    text = QApplication.clipboard().text()
    assert "profile:" in text
    assert "super-secret-key" not in text
