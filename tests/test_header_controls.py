"""Coverage for the header theme toggle, notification bell and icon crispness.

These replaced the old theme dropdown, so the behaviour they encode -- a
two-state toggle that resolves a stored ``System`` palette, a bell driven by
the real authentication activity log, and device-pixel-ratio aware icons --
has no other regression net.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from api_tester import theme
from api_tester.auth_log import activity_log
from api_tester.icons import badged_icon, icon, solid_icon


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    activity_log.clear()
    value = main_module.MainWindow()
    yield value
    value.close()
    activity_log.clear()


@pytest.fixture
def fast_theme(monkeypatch):
    """Rebinds palette tokens without repainting the whole window.

    ``apply_theme`` calls ``setStyleSheet`` on the QApplication, which
    repolishes every widget in the populated endpoint tree and takes tens of
    seconds. These tests assert the toggle's *decision*, not Qt's repaint, and
    stylesheet content is covered separately in ``test_theme.py``.
    """
    monkeypatch.setattr(
        theme,
        "apply_theme",
        lambda app, mode="Light": theme._activate(
            theme.DARK_TOKENS if theme.is_dark_mode(app, mode) else theme.LIGHT_TOKENS
        ),
    )


def test_theme_dropdown_is_gone_from_the_header(window):
    assert not hasattr(window, "theme_combo")
    assert window.theme_toggle_button.objectName() == "headerIconButton"
    assert window.notifications_button.objectName() == "headerIconButton"


def test_toggle_flips_between_light_and_dark(window, fast_theme):
    window._theme_changed("Light")
    assert window._current_theme_is_dark() is False

    window.theme_toggle_button.click()
    assert window.app_settings.theme == "Dark"
    assert window._current_theme_is_dark() is True

    window.theme_toggle_button.click()
    assert window.app_settings.theme == "Light"
    assert window._current_theme_is_dark() is False


def test_toggle_resolves_a_stored_system_theme_before_flipping(window, fast_theme):
    window._theme_changed("System")
    resolved_dark = theme.is_dark_mode(QApplication.instance(), "System")

    window._toggle_theme()

    assert window.app_settings.theme == ("Light" if resolved_dark else "Dark")


def test_toggle_icon_and_tooltip_point_at_the_destination_theme(window, fast_theme):
    window._theme_changed("Light")
    assert window.theme_toggle_button.toolTip() == "Turn the lights off"
    assert window.theme_toggle_button.accessibleName() == "Switch to dark theme"

    window._theme_changed("Dark")
    assert window.theme_toggle_button.toolTip() == "Turn the lights on"
    assert window.theme_toggle_button.accessibleName() == "Switch to light theme"


def test_bell_badges_unread_authentication_events(window):
    window._populate_notifications_menu()
    assert window.notifications_button.toolTip() == "Recent authentication activity"

    activity_log.add("token refreshed", "Default", "oauth2")
    window._refresh_header_buttons()

    assert window.notifications_button.toolTip() == "1 new authentication event"

    activity_log.add("token refreshed", "Default", "oauth2")
    window._refresh_header_buttons()

    assert window.notifications_button.toolTip() == "2 new authentication events"


def test_opening_the_bell_menu_clears_the_unread_badge(window):
    activity_log.add("sign-in", "Default", "api-key")
    window._refresh_header_buttons()
    assert "new authentication event" in window.notifications_button.toolTip()

    window._populate_notifications_menu()

    assert window.notifications_button.toolTip() == "Recent authentication activity"


def test_bell_menu_lists_recent_events_newest_first_and_can_clear(window):
    for index in range(14):
        activity_log.add(f"event {index}", "Default", "api-key")

    window._populate_notifications_menu()
    actions = [a for a in window.notifications_menu.actions() if not a.isSeparator()]
    labels = [a.text() for a in actions]

    assert "event 13" in labels[0]
    assert len([label for label in labels if "event" in label]) == 12
    assert labels[-1] == "Clear activity"

    window._clear_notifications()

    assert len(activity_log) == 0
    window._populate_notifications_menu()
    remaining = [a.text() for a in window.notifications_menu.actions()]
    assert remaining == ["No activity yet"]


def test_empty_bell_menu_explains_itself(window):
    window._populate_notifications_menu()
    actions = window.notifications_menu.actions()

    assert [a.text() for a in actions] == ["No activity yet"]
    assert actions[0].isEnabled() is False


@pytest.mark.parametrize("name", ["bell", "moon", "sun", "settings"])
def test_new_header_glyphs_render_pixels(qt_app, name):
    image = icon(name, "#65758B", 18).pixmap(18, 18).toImage()

    assert any(
        image.pixelColor(x, y).alpha() > 0
        for y in range(image.height())
        for x in range(image.width())
    )


@pytest.mark.parametrize("size", [16, 18, 22])
def test_icons_are_stored_supersampled_for_crisp_scaling(qt_app, size):
    stored = icon("settings", "#65758B", size).availableSizes()[0]

    assert stored.width() >= size
    assert stored.width() % size == 0


def test_badged_icon_adds_pixels_the_plain_icon_does_not(qt_app):
    plain = icon("bell", "#65758B", 18).pixmap(18, 18).toImage()
    badged = badged_icon("bell", "#65758B", "#0878f9", 18).pixmap(18, 18).toImage()

    assert plain.size() == badged.size()
    changed = sum(
        1
        for y in range(badged.height())
        for x in range(badged.width())
        if plain.pixelColor(x, y) != badged.pixelColor(x, y)
    )
    assert changed > 0


def test_solid_icon_keeps_its_foreground_when_disabled(qt_app):
    """Qt greys disabled icons by default, which erases the brand fill.

    ``solid_icon`` supplies an explicit Disabled pixmap so the button keeps
    reading as a primary action while it is temporarily unavailable.
    """
    from PyQt6.QtGui import QIcon

    def colours(mode):
        image = solid_icon("verify", "#0878f9", 18).pixmap(18, 18, mode).toImage()
        return {
            image.pixelColor(x, y).name()
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0
        }

    normal = colours(QIcon.Mode.Normal)
    disabled = colours(QIcon.Mode.Disabled)

    assert "#0878f9" in normal
    assert "#0878f9" in disabled
    assert disabled == normal
