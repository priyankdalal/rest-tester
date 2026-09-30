from __future__ import annotations

import pytest
from api_tester import theme


LIGHT_ONLY_LITERALS = (
    "#dbeafe",
    "#eff6ff",
    "#f8fafc",
    "#fee2e2",
    "#dcfce7",
    "#101828",
    "#475467",
    "#fbfdff",
)


def test_both_palettes_render_without_missing_tokens() -> None:
    light = theme.build_stylesheet(theme.LIGHT_TOKENS)
    dark = theme.build_stylesheet(theme.DARK_TOKENS)
    assert light and dark
    assert light != dark
    assert "QComboBox::down-arrow" in light
    assert "chevron-down-light.svg" in light
    assert "chevron-down-dark.svg" in dark
    assert "QSpinBox::up-arrow" in light
    assert "QDoubleSpinBox::down-arrow" in light
    assert "chevron-up-light.svg" in light
    assert "chevron-up-dark.svg" in dark
    assert "QSpinBox::up-button:hover" in light
    assert "QComboBox::drop-down" in light
    assert "QTabBar::left-arrow" in light
    assert "QTabBar::right-arrow" in light
    assert "chevron-left-light.svg" in light
    assert "chevron-right-dark.svg" in dark
    assert "QTabBar::tab:selected" in light
    assert "border: 1px solid #0878f9" in light
    assert "QSplitter::handle { background-color: transparent; }" in light
    assert "QWidget#suiteProgressPanel" in light
    assert "QProgressBar#suiteRunProgress::chunk" in light
    assert "stop: 0 #0878f9, stop: 1 #6b3fa0" in light
    assert "QWidget#caseResultSummary" in light
    assert "QTableWidget#caseResultTable" in light
    assert "QTableWidget#caseResultTable {\n    border: 1px solid #d7e2f0;" in light
    assert "QSplitter#caseResultTablesSplitter {\n    background-color: transparent;" in light
    assert 'QLabel[pageTitle="true"]' in light
    assert 'QLabel[pageDescription="true"]' in light
    assert 'QLabel[monospace="true"]' in light
    assert "gridline-color: transparent" in light
    assert "QTableWidget::item:hover" in light
    assert "QTableWidget QComboBox, QTableView QComboBox" in light
    assert "padding: 1px 24px 1px 5px" in light
    assert "QTableWidget QPushButton, QTableView QPushButton" in light
    assert "QTableWidget QLineEdit, QTableView QLineEdit" in light
    assert 'QLineEdit[validationState="error"]' in light


def test_table_cell_controls_use_compact_application_styles() -> None:
    stylesheet = theme.build_stylesheet(theme.LIGHT_TOKENS)
    combo_rule = stylesheet.split(
        "QTableWidget QComboBox, QTableView QComboBox {", 1
    )[1].split("}", 1)[0]
    button_rule = stylesheet.split(
        "QTableWidget QPushButton, QTableView QPushButton,", 1
    )[1].split("}", 1)[0]
    editor_rule = stylesheet.split(
        "QTableWidget QLineEdit, QTableView QLineEdit,", 1
    )[1].split("}", 1)[0]

    for rule in (combo_rule, button_rule, editor_rule):
        assert "margin: 2px" in rule
        assert "min-height: 16px" in rule
        assert "border-radius: 3px" in rule
    assert "padding: 1px 22px 1px 4px" in editor_rule
    assert "width: 20px" in stylesheet.split(
        "QTableWidget QComboBox::drop-down, QTableView QComboBox::drop-down {",
        1,
    )[1].split("}", 1)[0]


def test_dark_palette_does_not_leak_light_surface_colours() -> None:
    dark = theme.build_stylesheet(theme.DARK_TOKENS)
    for literal in LIGHT_ONLY_LITERALS:
        assert literal not in dark, literal


def test_dark_palette_defines_distinct_values_for_every_token() -> None:
    shared = {"PASS", "FAIL", "WARN", "SKIP"}
    assert set(theme.DARK_TOKENS) == set(theme.LIGHT_TOKENS)
    differing = {
        name
        for name in theme.LIGHT_TOKENS
        if theme.LIGHT_TOKENS[name] != theme.DARK_TOKENS[name]
    }
    assert differing - shared


def test_applying_a_theme_rebinds_module_colours_and_bumps_palette_version() -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, "Light")
    light_text = theme.TEXT
    light_version = theme.PALETTE_VERSION
    assert theme.is_dark_mode(app, "Light") is False

    theme.apply_theme(app, "Dark")
    assert theme.is_dark_mode(app, "Dark") is True
    assert theme.TEXT != light_text
    assert theme.PALETTE_VERSION != light_version

    theme.apply_theme(app, "Light")
    assert theme.TEXT == light_text


@pytest.mark.parametrize("mode", ["Light", "Dark"])
def test_standard_form_controls_share_a_consistent_height_without_clipping_icons(mode: str) -> None:
    pytest.importorskip("PyQt6")
    from PyQt6.QtCore import QSize
    from PyQt6.QtWidgets import (
        QApplication,
        QComboBox,
        QDoubleSpinBox,
        QHBoxLayout,
        QLineEdit,
        QPushButton,
        QWidget,
    )

    from api_tester.icons import icon

    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, mode)
    controls = [QLineEdit(), QComboBox(), QDoubleSpinBox()]
    button = QPushButton("Configure")
    button.setIcon(icon("settings", theme.TEXT, 18))
    button.setIconSize(QSize(18, 18))
    controls.append(button)
    host = QWidget()
    layout = QHBoxLayout(host)
    for control in controls:
        layout.addWidget(control)
    host.show()
    app.processEvents()

    heights = [control.height() for control in controls]
    assert max(heights) - min(heights) <= 2
    assert button.height() >= button.iconSize().height() + 10
    host.close()


def test_checkbox_indicator_draws_a_glyph_in_every_marked_state() -> None:
    """A filled box with no ``image:`` renders as a plain blue square.

    The checked/indeterminate rules must therefore ship a glyph asset, and the
    disabled variants must swap to the muted one.
    """
    for tokens in (theme.LIGHT_TOKENS, theme.DARK_TOKENS):
        stylesheet = theme.build_stylesheet(tokens)
        checked = stylesheet.split("QListWidget::indicator:checked {", 1)[1].split(
            "}", 1
        )[0]
        assert 'image: url("' in checked
        assert "check.svg" in checked
        assert tokens["PRIMARY"] in checked

        indeterminate = stylesheet.split(
            "QListWidget::indicator:indeterminate {", 1
        )[1].split("}", 1)[0]
        assert "check-dash.svg" in indeterminate

        muted = stylesheet.split("QListWidget::indicator:checked:disabled {", 1)[
            1
        ].split("}", 1)[0]
        assert "check-muted.svg" in muted


def test_radio_indicator_stays_a_dot_and_never_borrows_the_check_glyph() -> None:
    stylesheet = theme.build_stylesheet(theme.LIGHT_TOKENS)
    radio_checked = stylesheet.split("QRadioButton::indicator:checked {", 1)[1].split(
        "}", 1
    )[0]

    assert "image: none" in radio_checked
    assert "border: 2px solid" in radio_checked
    assert "QRadioButton::indicator { border-radius: 9px; }" in stylesheet


def test_header_icon_buttons_hide_their_menu_arrow() -> None:
    for tokens in (theme.LIGHT_TOKENS, theme.DARK_TOKENS):
        stylesheet = theme.build_stylesheet(tokens)
        assert "QToolButton#headerIconButton" in stylesheet
        indicator = stylesheet.split(
            "QToolButton#headerIconButton::menu-indicator {", 1
        )[1].split("}", 1)[0]
        assert "width: 0" in indicator


def test_check_glyph_assets_exist_on_disk() -> None:
    from pathlib import Path

    for tokens in (theme.LIGHT_TOKENS, theme.DARK_TOKENS):
        for key in ("CHECK_MARK", "CHECK_DASH", "CHECK_MARK_MUTED"):
            assert Path(tokens[key]).exists(), f"missing asset for {key}"
