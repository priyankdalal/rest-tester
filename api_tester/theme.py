"""Application palette and stylesheet.

The stylesheet is a token template rendered from an explicit palette, so light
and dark mode each define every surface, border, text, state, and syntax colour
deliberately instead of being derived by search-and-replacing a light theme.

Outcome colours (pass/fail/warn/skip) and HTTP verb colours are shared by both
themes: they remain the single source of truth for the whole app.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt

# Surfaces
BACKGROUND = "#f3f7fc"
SURFACE = "#ffffff"
SURFACE_ALT = "#eef4fb"
BORDER = "#d7e2f0"
BORDER_STRONG = "#b9cae0"

# Text
TEXT = "#173052"
TEXT_MUTED = "#65758b"
TEXT_INVERSE = "#ffffff"
#: Row height for tables whose cells are mostly inline editors. Derived, not
#: guessed: the ``#roomyEditorTable`` editors resolve to 29px tall (21px of
#: content plus padding and border), ``QTableWidget::item`` insets every cell
#: by 5px top and bottom, and the row contributes a 1px grid line — so
#: anything under 40 clips the editor against the row boundary.
ROOMY_ROW_HEIGHT = 40

# Accents
PRIMARY = "#0878f9"
PRIMARY_HOVER = "#0069e6"
PRIMARY_PRESSED = "#0058c7"
ACCENT = "#6b3fa0"
NAVIGATION = "#06264b"
PATCH = "#8b5cf6"

# Soft state fills
ACCENT_SOFT = "#dbeafe"
ACCENT_SOFT_TEXT = "#0058c7"
HOVER_SOFT = "#eff6ff"
SELECT_SOFT = "#dbeafe"
SELECT_SOFT_TEXT = "#0058c7"
DANGER_SOFT = "#fee2e2"
SUCCESS_SOFT = "#dcfce7"

# Navigation rail
NAV_TEXT = "#dce9f8"
NAV_MUTED = "#b8cbe2"
NAV_HOVER = "#0b396c"

# Code surfaces (response body, raw view, request body)
CODE_BACKGROUND = "#fbfdff"
CODE_TEXT = "#173052"
CODE_BORDER = "#d7e2f0"
CODE_SELECTION = "#bcd9fb"

# Outcomes — the single source of truth for the whole app.
PASS = "#16a34a"
FAIL = "#ef3340"
WARN = "#f59e0b"
SKIP = "#98a2b3"

# HTTP verb badges
METHOD_COLORS = {
    "GET": "#16a34a",
    "POST": "#0878f9",
    "PUT": "#f59e0b",
    "PATCH": PATCH,
    "DELETE": "#ef3340",
    "HEAD": "#5b6775",
    "OPTIONS": "#5b6775",
}

_METHOD_PILL_PALETTES = {
    "GET": (
        ("#f0fdf4", "#16a34a", "#86efac"),
        ("#173b2b", "#4ade80", "#286344"),
    ),
    "HEAD": (
        ("#f8fafc", "#5b6775", "#cbd5e1"),
        ("#25364a", "#cbd5e1", "#465a72"),
    ),
    "OPTIONS": (
        ("#f8fafc", "#5b6775", "#cbd5e1"),
        ("#25364a", "#cbd5e1", "#465a72"),
    ),
    "POST": (
        ("#eff6ff", "#0878f9", "#60a5fa"),
        ("#17365b", "#60a5fa", "#28548a"),
    ),
    "PUT": (
        ("#fffbeb", "#b45309", "#fbbf24"),
        ("#42351a", "#fbbf24", "#70551e"),
    ),
    "PATCH": (
        ("#faf5ff", "#8b5cf6", "#c084fc"),
        ("#35264f", "#c4b5fd", "#5c4386"),
    ),
    "DELETE": (
        ("#fff1f2", "#ef3340", "#fb7185"),
        ("#45232a", "#fda4af", "#743943"),
    ),
}


def _method_pill_tokens(*, dark: bool) -> dict[str, str]:
    palette_index = 1 if dark else 0
    return {
        f"METHOD_{method}_{part}": color
        for method, palettes in _METHOD_PILL_PALETTES.items()
        for part, color in zip(("BG", "TEXT", "BORDER"), palettes[palette_index])
    }

# JSON syntax highlighting
JSON_KEY = "#1d4ed8"
JSON_STRING = "#0f7b3f"
JSON_NUMBER = "#b45309"
JSON_KEYWORD = "#8b2fb8"
JSON_PUNCTUATION = "#5b6775"
JSON_ERROR = "#b42318"

MONO_FAMILY = "Cascadia Mono, Consolas, Menlo, monospace"
ASSET_DIR = Path(__file__).resolve().parent / "assets"


def status_color(status_code: int) -> str:
    if 200 <= status_code < 300:
        return PASS
    if 300 <= status_code < 400:
        return PRIMARY
    if 400 <= status_code < 500:
        return WARN
    if status_code >= 500:
        return FAIL
    return SKIP


def method_color(method: str) -> str:
    return METHOD_COLORS.get((method or "").upper(), TEXT_MUTED)


_TEMPLATE = """
QWidget {{
    background-color: {BACKGROUND};
    color: {TEXT};
    font-size: 10pt;
}}
QMainWindow, QDialog {{ background-color: {BACKGROUND}; }}
QLabel {{
    background: transparent;
}}
QLabel[pageTitle="true"] {{
    color: {TEXT};
    font-size: 16pt;
    font-weight: 700;
}}
QLabel[pageDescription="true"] {{
    color: {TEXT_MUTED};
    font-size: 9pt;
}}
QLabel[sectionTitle="true"] {{
    color: {TEXT};
    font-size: 11pt;
    font-weight: 600;
}}
QLabel[muted="true"] {{
    color: {TEXT_MUTED};
    font-size: 9pt;
}}
QLabel[monospace="true"] {{
    font-family: "Cascadia Mono", "Consolas", monospace;
}}
QLineEdit[monospace="true"], QPlainTextEdit[monospace="true"],
QTextEdit[monospace="true"], QComboBox[monospace="true"] {{
    font-family: "Cascadia Mono", "Consolas", monospace;
}}
QLabel[numeric="true"] {{
    font-family: "Segoe UI", sans-serif;
}}

QWidget#appHeader {{
    background-color: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
QLabel#appLogo {{
    background: transparent;
}}
QLabel#appTitle {{
    background: transparent;
    color: {TEXT};
    font-size: 11pt;
    font-weight: 600;
}}
QTableWidget#environmentTable QPushButton {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 0;
    min-height: 0;
    max-height: 28px;
}}
QTableWidget#environmentTable QPushButton:hover {{
    background-color: {ACCENT_SOFT};
    border-color: {BORDER_STRONG};
}}
QTableWidget#environmentTable QPushButton[danger="true"]:hover {{
    background-color: {DANGER_SOFT};
    border-color: {FAIL};
}}
QTableWidget#environmentTable QPushButton:disabled {{
    background-color: transparent;
    border-color: transparent;
}}
QPushButton#environmentNewButton {{
    min-width: 150px;
}}
QLabel[fieldCaption="true"] {{
    background: transparent;
    color: {TEXT_MUTED};
    font-size: 8pt;
    font-weight: 600;
}}
QListWidget#navigationRail {{
    background-color: {NAVIGATION};
    color: {NAV_TEXT};
    border: none;
    border-radius: 0;
    padding: 12px 7px;
    outline: none;
}}
QWidget#navigationPanel {{
    background-color: {NAVIGATION};
}}
QWidget#navigationFooter,
QWidget#navigationVersionRow {{
    background-color: {NAVIGATION};
}}
QLabel#navigationStats, QLabel#navigationVersion {{
    background: transparent;
    color: {NAV_MUTED};
    padding: 8px;
}}
QWidget#navigationFooter {{
    background: transparent;
}}
QLabel#navigationEnvironmentCaption {{
    background: transparent;
    color: {NAV_MUTED};
    font-size: 8pt;
    font-weight: 600;
    padding: 0 2px;
}}
QComboBox#navigationEnvironment {{
    background-color: rgba(255, 255, 255, 0.06);
    color: {NAV_TEXT};
    border: 1px solid rgba(184, 203, 226, 0.28);
    border-radius: 6px;
    padding: 5px 28px 5px 9px;
    font-weight: 600;
}}
QComboBox#navigationEnvironment:hover {{
    background-color: {NAV_HOVER};
    border-color: rgba(184, 203, 226, 0.45);
}}
QComboBox#navigationEnvironment:focus,
QComboBox#navigationEnvironment:on {{
    border-color: rgba(184, 203, 226, 0.6);
}}
QComboBox#navigationEnvironment:disabled {{
    color: {NAV_MUTED};
}}
QComboBox#navigationEnvironment::drop-down {{
    width: 24px;
    background: transparent;
}}
QComboBox#navigationEnvironment::drop-down:hover {{
    background: transparent;
}}
QComboBox#navigationEnvironment::down-arrow {{
    image: url("{NAV_COMBO_ARROW}");
    width: 10px;
    height: 7px;
}}
QComboBox#navigationEnvironment QAbstractItemView {{
    background-color: {NAVIGATION};
    color: {NAV_TEXT};
    border: 1px solid rgba(184, 203, 226, 0.28);
    selection-background-color: {NAV_HOVER};
    selection-color: {NAV_TEXT};
    outline: none;
}}
QToolButton#navigationFooterButton {{
    background-color: transparent;
    color: {NAV_MUTED};
    border: 1px solid transparent;
    border-radius: 12px;
    padding: 2px;
}}
QToolButton#navigationInfoButton::menu-indicator {{
    image: none;
    width: 0;
}}
QToolButton#navigationInfoButton {{
    background-color: transparent;
    color: {NAV_MUTED};
    border: 1px solid transparent;
    border-radius: 12px;
    padding: 2px;
}}
QToolButton#navigationInfoButton:hover {{
    background-color: {NAV_HOVER};
    border-color: {BORDER_STRONG};
}}
QToolButton#navigationInfoButton:pressed,
QToolButton#navigationInfoButton:open {{
    background-color: {NAV_HOVER};
}}
QToolButton#navigationFooterButton:hover {{
    background-color: {NAV_HOVER};
    border-color: {BORDER_STRONG};
}}
QToolButton#navigationFooterButton:pressed,
QToolButton#navigationFooterButton:open {{
    background-color: {NAV_HOVER};
}}
QListWidget#navigationRail::item {{
    border-radius: 4px;
    padding: 2px 9px;
    margin-bottom: 2px;
}}
QListWidget#navigationRail::item:hover {{ background-color: {NAV_HOVER}; }}
QListWidget#navigationRail[dialogRail="true"] {{
    padding: 10px 6px;
    border-radius: 6px;
}}
QListWidget#navigationRail::item:selected {{
    background-color: {PRIMARY};
    color: {TEXT_INVERSE};
    font-weight: 600;
}}
QPushButton#favoriteButton[favorite="true"] {{
    background-color: {SUCCESS_SOFT};
    color: {PASS};
    border-color: {PASS};
}}
QWidget#endpointExplorer,
QWidget#suiteNavigator {{
    background-color: {SURFACE};
    border: none;
}}
QWidget#requestWorkspace {{
    background-color: {BACKGROUND};
    border-top-left-radius: 8px;
    border-bottom-left-radius: 8px;
}}
QWidget#requestWorkspaceShell {{
    background-color: transparent;
}}
QWidget#suiteDetailShell {{
    background-color: transparent;
}}
QWidget#suiteTabContentShell {{
    background-color: transparent;
}}
QWidget#suiteActionBar {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QWidget#leftInsetShadow {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 {SHADOW_EDGE},
        stop: 0.35 {SHADOW_MID},
        stop: 1 transparent
    );
}}
QWidget#topInsetShadow {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 0, y2: 1,
        stop: 0 {SHADOW_EDGE},
        stop: 0.35 {SHADOW_MID},
        stop: 1 transparent
    );
}}
QWidget#cornerInsetShadow {{
    background: qradialgradient(
        cx: 1, cy: 1, radius: 1,
        fx: 1, fy: 1,
        stop: 0 transparent,
        stop: 0.65 {SHADOW_MID},
        stop: 1 {SHADOW_EDGE}
    );
}}
QLabel[workspaceTitle="true"] {{
    color: {TEXT};
    font-size: 14pt;
    font-weight: 700;
}}
QLabel#countBadge {{
    background-color: {ACCENT_SOFT};
    color: {ACCENT_SOFT_TEXT};
    border-radius: 9px;
    padding: 2px 7px;
    font-weight: 700;
}}
QLabel#endpointBreadcrumb {{
    color: {TEXT};
    font-size: 13pt;
    font-weight: 700;
    padding: 2px 0;
}}
QFrame#endpointIdentity {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QLabel#endpointMethod {{
    color: #ffffff;
    background-color: {PRIMARY};
    border-top-left-radius: 5px;
    border-bottom-left-radius: 5px;
    padding: 8px 12px;
    font-weight: 700;
}}
QLabel#endpointMethod[method="GET"] {{ background-color: {PASS}; }}
QLabel#endpointMethod[method="POST"] {{ background-color: {PRIMARY}; }}
QLabel#endpointMethod[method="PUT"] {{ background-color: {WARN}; }}
QLabel#endpointMethod[method="PATCH"] {{ background-color: {PATCH}; }}
QLabel#endpointMethod[method="DELETE"] {{ background-color: {FAIL}; }}
QLabel#endpointRoute {{
    background-color: transparent;
    color: {TEXT};
    padding: 8px 12px;
    font-weight: 600;
}}
QFrame#parametersCard {{
    background-color: {SURFACE};
    border: none;
}}
QFrame#headerStatusPill {{
    border-radius: 16px;
    border: 1px solid {DISCONNECTED_BORDER};
    background-color: {DISCONNECTED_BG};
}}
QFrame#headerStatusPill[state="connected"] {{
    border: 1px solid {CONNECTED_BORDER};
    background-color: {CONNECTED_BG};
}}
QWidget#headerStatusDotHost {{
    background: transparent;
    border: none;
}}
QToolButton#headerStatusEdit {{
    background-color: {SURFACE};
    border: none;
    border-left: 1px solid {BORDER};
    border-top-left-radius: 0px;
    border-bottom-left-radius: 0px;
    border-top-right-radius: 15px;
    border-bottom-right-radius: 15px;
    padding: 0px;
}}
QToolButton#headerStatusEdit:hover {{
    background-color: {SURFACE_ALT};
    border-left: 1px solid {BORDER_STRONG};
}}
QToolButton#headerStatusEdit:pressed {{
    background-color: {HOVER_SOFT};
    border-left: 1px solid {BORDER_STRONG};
    padding-top: 2px;
}}
QToolButton#headerStatusEdit::menu-indicator {{
    image: none;
    width: 0px;
    height: 0px;
}}
QWidget#responseViewer {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QWidget#responseStatusBar {{
    background-color: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}

/* Group boxes read as cards with an in-card heading, not a legend cut into
   the border: the title sits inside the padding box, above the contents. */
QGroupBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 0;
    padding: 40px 10px 10px 10px;
    font-size: 10.5pt;
    font-weight: 700;
}}
QGroupBox::title {{
    subcontrol-origin: padding;
    subcontrol-position: top left;
    left: 12px;
    top: 12px;
    padding: 0;
    background: transparent;
    color: {TEXT};
}}
QGroupBox#summaryTile {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    padding-top: 32px;
    font-size: 9pt;
    font-weight: 600;
}}
QGroupBox#summaryTile::title {{
    top: 10px;
    color: {TEXT_MUTED};
}}
QFrame#accordionSection {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QToolButton#accordionHeader {{
    background-color: {SURFACE_ALT};
    color: {TEXT};
    border: none;
    border-bottom: 1px solid transparent;
    border-radius: 7px;
    padding: 10px 12px;
    text-align: left;
    font-weight: 600;
}}
QToolButton#accordionHeader:hover {{
    background-color: {ACCENT_SOFT};
    color: {PRIMARY};
}}
QToolButton#accordionHeader:checked {{
    border-bottom-color: {BORDER};
    border-bottom-left-radius: 0;
    border-bottom-right-radius: 0;
}}
QToolButton#accordionHeader[collapsed="true"] {{
    padding-top: 8px;
    padding-bottom: 8px;
}}
QLabel#accordionSummary {{
    background-color: {SURFACE};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 2px 7px;
    font-size: 8pt;
    font-weight: 600;
}}
QWidget#accordionBody {{
    background-color: {SURFACE};
}}
QWidget#accordionContent,
QWidget#runBreakdowns {{
    background-color: transparent;
}}
QLabel#summaryValue {{
    background: transparent;
    font-size: 22px;
    font-weight: bold;
    color: {TEXT};
}}
QLabel#summaryValue[outcome="passed"] {{ color: {PASS}; }}
QLabel#summaryValue[outcome="failed"] {{ color: {FAIL}; }}
QLabel#summaryValue[outcome="errored"] {{ color: {WARN}; }}
QLabel#summaryValue[outcome="skipped"] {{ color: {TEXT_MUTED}; }}

QCheckBox, QRadioButton {{
    background: transparent;
    color: {TEXT};
    spacing: 7px;
}}
QCheckBox::indicator, QRadioButton::indicator,
QTableWidget::indicator, QTreeWidget::indicator, QListWidget::indicator {{
    width: 16px;
    height: 16px;
    background-color: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 5px;
}}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover,
QTableWidget::indicator:hover, QTreeWidget::indicator:hover,
QListWidget::indicator:hover {{
    border: 1px solid {PRIMARY};
    background-color: {HOVER_SOFT};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked,
QTableWidget::indicator:checked, QTreeWidget::indicator:checked,
QListWidget::indicator:checked {{
    background-color: {PRIMARY};
    border: 1px solid {PRIMARY};
    image: url("{CHECK_MARK}");
}}
QRadioButton::indicator:checked {{
    image: none;
    border: 2px solid {SURFACE};
}}
QCheckBox::indicator:checked:hover,
QTableWidget::indicator:checked:hover, QTreeWidget::indicator:checked:hover,
QListWidget::indicator:checked:hover {{
    background-color: {PRIMARY_HOVER};
    border: 1px solid {PRIMARY_HOVER};
}}
QCheckBox::indicator:indeterminate, QTableWidget::indicator:indeterminate,
QTreeWidget::indicator:indeterminate, QListWidget::indicator:indeterminate {{
    background-color: {PRIMARY};
    border: 1px solid {PRIMARY};
    image: url("{CHECK_DASH}");
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled,
QTableWidget::indicator:disabled, QTreeWidget::indicator:disabled,
QListWidget::indicator:disabled {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
}}
QCheckBox::indicator:checked:disabled,
QCheckBox::indicator:indeterminate:disabled,
QTableWidget::indicator:checked:disabled,
QTreeWidget::indicator:checked:disabled,
QListWidget::indicator:checked:disabled {{
    image: url("{CHECK_MARK_MUTED}");
}}
QCheckBox:disabled, QRadioButton:disabled {{ color: {TEXT_MUTED}; }}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QDateEdit {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 5px 6px;
    selection-background-color: {PRIMARY};
    selection-color: {TEXT_INVERSE};
}}
QLineEdit, QSpinBox, QDoubleSpinBox, QDateTimeEdit, QDateEdit {{
    min-height: 22px;
    max-height: 22px;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QDateTimeEdit:focus, QDateEdit:focus {{
    border: 1px solid {PRIMARY};
}}
QSpinBox, QDoubleSpinBox, QDateTimeEdit, QDateEdit {{
    padding-right: 30px;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QDateTimeEdit::up-button, QDateEdit::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 26px;
    border: none;
    border-top-right-radius: 4px;
}}
QSpinBox::down-button, QDoubleSpinBox::down-button,
QDateTimeEdit::down-button, QDateEdit::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 26px;
    border: none;
    border-bottom-right-radius: 4px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QDateTimeEdit::up-button:hover, QDateEdit::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover,
QDateTimeEdit::down-button:hover, QDateEdit::down-button:hover {{
    background-color: {HOVER_SOFT};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow,
QDateTimeEdit::up-arrow, QDateEdit::up-arrow {{
    image: url("{SPIN_UP_ARROW}");
    width: 10px;
    height: 7px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow,
QDateTimeEdit::down-arrow, QDateEdit::down-arrow {{
    image: url("{COMBO_ARROW}");
    width: 10px;
    height: 7px;
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
    background-color: {SURFACE_ALT};
    color: {TEXT_MUTED};
}}
QLineEdit[readOnly="true"], QPlainTextEdit[readOnly="true"],
QTextEdit[readOnly="true"] {{
    background-color: {SURFACE_ALT};
    color: {TEXT_MUTED};
}}
QLineEdit[validationState="error"], QPlainTextEdit[validationState="error"],
QTextEdit[validationState="error"], QComboBox[validationState="error"] {{
    border: 1px solid {FAIL};
    background-color: {DANGER_SOFT};
}}

QComboBox {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 36px 5px 10px;
    min-height: 22px;
    max-height: 22px;
}}
QComboBox:focus {{ border: 1px solid {PRIMARY}; }}
QComboBox:hover {{ border-color: {BORDER_STRONG}; }}
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 30px;
    border: none;
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
}}
QComboBox::drop-down:hover {{
    background-color: {HOVER_SOFT};
}}
QComboBox::down-arrow {{
    image: url("{COMBO_ARROW}");
    width: 12px;
    height: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {PRIMARY};
    selection-color: {TEXT_INVERSE};
}}
QListView#searchSuggestionPopup {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 4px;
    outline: 0;
}}
QListView#searchSuggestionPopup::item {{
    padding: 5px 8px;
    border-radius: 4px;
}}
QListView#searchSuggestionPopup::item:hover {{
    background-color: {SURFACE_ALT};
}}
QListView#searchSuggestionPopup::item:selected {{
    background-color: {PRIMARY};
    color: {TEXT_INVERSE};
}}
/* An editable combo's inner edit field must not inherit the global QLineEdit
   border and padding: the combo already draws both, and the extra padding
   scrolled short values such as "200-299" out of view. */
QComboBox QLineEdit {{
    border: none;
    padding: 0;
    background: transparent;
    min-height: 0;
}}

QPushButton {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 5px 13px;
    min-height: 22px;
    max-height: 22px;
}}
QPushButton:hover {{ background-color: {SURFACE_ALT}; }}
QPushButton:pressed {{ background-color: {BORDER}; }}
QPushButton:disabled {{ color: {SKIP}; border-color: {BORDER}; }}
QPushButton[accent="true"] {{
    background-color: {PRIMARY};
    border: 1px solid {PRIMARY_PRESSED};
    color: {TEXT_INVERSE};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{ background-color: {PRIMARY_HOVER}; }}
QPushButton[accent="true"]:pressed {{ background-color: {PRIMARY_PRESSED}; }}
QPushButton[accent="true"]:disabled {{
    background-color: {SKIP};
    border-color: {SKIP};
    color: {TEXT_INVERSE};
}}
QToolButton#endpointSendButton[accent="true"] {{
    background-color: {PRIMARY};
    border: 1px solid {PRIMARY_PRESSED};
    border-radius: 6px;
    color: {TEXT_INVERSE};
    padding: 5px 36px 5px 13px;
    min-height: 22px;
    max-height: 22px;
    font-weight: 600;
}}
QToolButton#endpointSendButton[accent="true"]:hover {{
    background-color: {PRIMARY_HOVER};
}}
QToolButton#endpointSendButton[accent="true"]:pressed,
QToolButton#endpointSendButton[accent="true"]:open {{
    background-color: {PRIMARY_PRESSED};
}}
QToolButton#endpointSendButton[accent="true"]:disabled {{
    background-color: {SKIP};
    border-color: {SKIP};
    color: {TEXT_INVERSE};
}}
QToolButton#endpointSendButton::menu-button {{
    subcontrol-origin: padding;
    subcontrol-position: right center;
    width: 30px;
    border-left: 1px solid {PRIMARY_HOVER};
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
}}
QToolButton#endpointSendButton::menu-button:hover {{
    background-color: {PRIMARY_HOVER};
}}
QToolButton#endpointSendButton::menu-indicator {{
    image: url({COMBO_ARROW});
    subcontrol-origin: padding;
    subcontrol-position: right center;
    width: 10px;
    height: 7px;
    right: 10px;
}}
QToolButton#endpointSplitButton {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 5px 36px 5px 13px;
    min-height: 22px;
    max-height: 22px;
}}
QToolButton#endpointSplitButton:hover {{
    background-color: {SURFACE_ALT};
}}
QToolButton#endpointSplitButton:pressed,
QToolButton#endpointSplitButton:open {{
    background-color: {BORDER};
}}
QToolButton#endpointSplitButton:disabled {{
    color: {TEXT_MUTED};
}}
QToolButton#endpointSplitButton::menu-button {{
    subcontrol-origin: padding;
    subcontrol-position: right center;
    width: 30px;
    border-left: 1px solid {BORDER_STRONG};
    border-top-right-radius: 5px;
    border-bottom-right-radius: 5px;
}}
QToolButton#endpointSplitButton::menu-button:hover {{
    background-color: {SURFACE_ALT};
}}
QToolButton#endpointSendButton::menu-button:disabled,
QToolButton#endpointSplitButton::menu-button:disabled {{
    border-left: 1px solid transparent;
    background-color: transparent;
}}
QToolButton#endpointSplitButton::menu-indicator {{
    image: url({COMBO_ARROW});
    subcontrol-origin: padding;
    subcontrol-position: right center;
    width: 10px;
    height: 7px;
    right: 10px;
}}
QToolButton#endpointMenuButton {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER_STRONG};
    border-radius: 6px;
    padding: 7px 29px 7px 13px;
    min-height: 18px;
}}
QToolButton#endpointMenuButton:hover {{
    background-color: {SURFACE_ALT};
}}
QToolButton#endpointMenuButton:pressed,
QToolButton#endpointMenuButton:open {{
    background-color: {BORDER};
}}
QToolButton#endpointMenuButton::menu-indicator {{
    subcontrol-origin: padding;
    subcontrol-position: right center;
    right: 9px;
    width: 10px;
    height: 10px;
}}
QPushButton[danger="true"] {{ color: {FAIL}; }}
QPushButton[danger="true"]:hover {{
    background-color: {DANGER_SOFT};
    border-color: {FAIL};
}}
QPushButton[ghost="true"] {{
    background-color: transparent;
    border-color: transparent;
}}
QPushButton[ghost="true"]:hover {{
    background-color: {HOVER_SOFT};
    border-color: {BORDER};
}}
QPushButton#iconButton {{
    border: none;
    padding: 4px;
    background: transparent;
}}
QPushButton#iconButton:hover {{ background-color: {SURFACE_ALT}; }}
QToolButton#headerIconButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 0px;
}}
QToolButton#headerIconButton:hover {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
}}
QToolButton#headerIconButton:pressed {{
    background-color: {HOVER_SOFT};
    border: 1px solid {BORDER_STRONG};
}}
QToolButton#headerIconButton::menu-indicator {{
    image: none;
    width: 0px;
    height: 0px;
}}
QPushButton#rowButton {{
    padding: 2px 9px;
    min-height: 16px;
    border-radius: 5px;
}}
QPushButton#rowRemoveButton {{
    padding: 2px;
    min-height: 16px;
    min-width: 26px;
    border-radius: 5px;
    background: transparent;
    border: 1px solid transparent;
}}
QPushButton#rowRemoveButton:hover {{
    background-color: {DANGER_SOFT};
    border: 1px solid {FAIL};
}}

QTabWidget::pane {{
    background-color: {SURFACE};
    border: none;
    border-top: 1px solid {BORDER};
    top: -1px;
}}
QTabBar {{
    background-color: transparent;
    qproperty-drawBase: 0;
}}
QTabBar::tab {{
    background-color: transparent;
    color: {TEXT_MUTED};
    border: none;
    /* Reserves the underline on every tab so selecting one cannot shift the
       row, and lets the selected rule simply recolour it. */
    border-bottom: 2px solid transparent;
    padding: 8px 14px;
    margin: 0 2px;
    font-weight: 600;
}}
QTabBar::tab:hover {{
    color: {TEXT};
    border-bottom-color: {BORDER_STRONG};
}}
QTabBar::tab:selected {{
    background-color: transparent;
    color: {PRIMARY};
    border-bottom: 2px solid {PRIMARY};
    font-weight: 700;
}}
QTabBar::tab:disabled {{
    color: {SKIP};
}}
QTabBar::scroller {{
    width: 60px;
}}
QTabBar QToolButton {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    margin: 2px;
    padding: 5px;
}}
QTabBar QToolButton:hover {{
    background-color: {HOVER_SOFT};
    border-color: {BORDER};
}}
QTabBar QToolButton:pressed {{
    background-color: {SELECT_SOFT};
}}
QTabBar::left-arrow {{
    image: url("{TAB_LEFT_ARROW}");
    width: 8px;
    height: 12px;
}}
QTabBar::right-arrow {{
    image: url("{TAB_RIGHT_ARROW}");
    width: 8px;
    height: 12px;
}}
QTabWidget#endpointTabs::pane {{
    border: none;
    border-top: 1px solid {BORDER};
    background: {SURFACE};
}}
QTabWidget#requestBuilderTabs::pane {{
    border: none;
    border-top: 1px solid {BORDER};
    background: {SURFACE};
}}

/* QHeaderView defaults to no eliding, so a narrow column showed the middle of
   its centred label; elide on the right for every header, app-wide. */
QHeaderView {{ background-color: {SURFACE_ALT}; qproperty-textElideMode: ElideRight; }}
QHeaderView::section {{
    background-color: {SURFACE_ALT};
    color: {TEXT_MUTED};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 5px 8px;
    font-weight: 600;
}}
/* Keep the sort chevron beside the label; the default drew it above the
   text, pushing the label down so it looked wrapped and clipped. */
QHeaderView::section:horizontal {{
    padding-right: 22px;
}}
QHeaderView::up-arrow, QHeaderView::down-arrow {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    right: 6px;
    width: 10px;
    height: 7px;
}}
QHeaderView::up-arrow {{ image: url("{SPIN_UP_ARROW}"); }}
QHeaderView::down-arrow {{ image: url("{COMBO_ARROW}"); }}
QHeaderView::section:hover {{ background-color: {HOVER_SOFT}; }}
QTableCornerButton::section {{
    background-color: {SURFACE_ALT};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
}}
/* ``outline: none`` suppresses the dotted focus rectangle the style paints
   around the *current* item. Without it every clicked cell gained a dark
   1px dashed box on top of the selection fill, which read as a stray black
   border. The selection background is the only current-item cue we want. */
QTableWidget, QTableView, QTreeWidget, QTreeView, QListWidget, QListView {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    alternate-background-color: {SURFACE_ALT};
    selection-background-color: {SELECT_SOFT};
    selection-color: {SELECT_SOFT_TEXT};
    gridline-color: transparent;
    outline: none;
}}
QTreeWidget::item, QListWidget::item, QTableWidget::item {{
    min-height: 24px;
    padding: 5px 7px;
}}
QTableWidget::item {{
    border-bottom: 1px solid {BORDER};
}}
QTableWidget::item:hover {{
    background-color: {HOVER_SOFT};
}}
QTableWidget::item:selected {{
    background-color: {SELECT_SOFT};
    color: {SELECT_SOFT_TEXT};
}}
QTableWidget QComboBox, QTableView QComboBox {{
    margin: 2px;
    padding: 1px 24px 1px 5px;
    min-height: 16px;
    max-height: 16px;
    border-radius: 3px;
}}
QTableWidget QComboBox::drop-down, QTableView QComboBox::drop-down {{
    width: 20px;
    border-top-right-radius: 2px;
    border-bottom-right-radius: 2px;
}}
QTableWidget QPushButton, QTableView QPushButton,
QTableWidget QToolButton, QTableView QToolButton {{
    margin: 2px;
    padding: 1px 5px;
    min-height: 16px;
    max-height: 16px;
    border-radius: 3px;
}}
QTableWidget QLineEdit, QTableView QLineEdit,
QTableWidget QSpinBox, QTableView QSpinBox,
QTableWidget QDoubleSpinBox, QTableView QDoubleSpinBox,
QTableWidget QDateEdit, QTableView QDateEdit,
QTableWidget QDateTimeEdit, QTableView QDateTimeEdit {{
    margin: 2px;
    padding: 1px 22px 1px 4px;
    min-height: 16px;
    max-height: 16px;
    border-radius: 3px;
}}
QTableWidget QSpinBox::up-button, QTableView QSpinBox::up-button,
QTableWidget QDoubleSpinBox::up-button, QTableView QDoubleSpinBox::up-button,
QTableWidget QSpinBox::down-button, QTableView QSpinBox::down-button,
QTableWidget QDoubleSpinBox::down-button, QTableView QDoubleSpinBox::down-button {{
    width: 18px;
}}
QTableWidget QSpinBox::up-arrow, QTableView QSpinBox::up-arrow,
QTableWidget QDoubleSpinBox::up-arrow, QTableView QDoubleSpinBox::up-arrow,
QTableWidget QSpinBox::down-arrow, QTableView QSpinBox::down-arrow,
QTableWidget QDoubleSpinBox::down-arrow, QTableView QDoubleSpinBox::down-arrow {{
    width: 8px;
    height: 5px;
}}
/* Tables whose rows are mostly inline editors get 5px more room. The
   ID selector outranks the generic QTableWidget rules above. */
QTableWidget#roomyEditorTable QComboBox,
QTableWidget#roomyEditorTable QPushButton,
QTableWidget#roomyEditorTable QToolButton,
QTableWidget#roomyEditorTable QLineEdit,
QTableWidget#roomyEditorTable QSpinBox,
QTableWidget#roomyEditorTable QDoubleSpinBox {{
    min-height: 21px;
    max-height: 21px;
}}
QTreeWidget#qt_scrollarea_viewport {{ background: {SURFACE}; }}
QTreeWidget::item {{
    min-height: 24px;
    border-radius: 4px;
    padding: 4px;
}}
QTreeWidget::item:hover {{ background-color: {HOVER_SOFT}; }}
QTreeWidget::item:selected {{
    background-color: {SELECT_SOFT};
    color: {SELECT_SOFT_TEXT};
}}
QTreeWidget#endpointTree {{
    border: none;
    border-radius: 0;
}}
QTreeWidget#endpointTree::item {{
    min-height: 46px;
    padding: 0;
}}
QWidget#endpointTreeRow {{
    background-color: transparent;
    border-radius: 4px;
}}
QWidget#endpointActionCell {{
    background-color: transparent;
}}
QWidget#endpointTreeRow[selected="true"] {{
    background-color: {SELECT_SOFT};
}}
QLabel#endpointMethodPill {{
    background-color: {METHOD_GET_BG};
    color: {METHOD_GET_TEXT};
    border: 1px solid {METHOD_GET_BORDER};
    border-radius: 6px;
    font-weight: 700;
}}
QLabel#endpointMethodPill[method="HEAD"],
QLabel#endpointMethodPill[method="OPTIONS"] {{
    background-color: {METHOD_HEAD_BG};
    color: {METHOD_HEAD_TEXT};
    border-color: {METHOD_HEAD_BORDER};
}}
QLabel#endpointMethodPill[method="POST"] {{
    background-color: {METHOD_POST_BG};
    color: {METHOD_POST_TEXT};
    border-color: {METHOD_POST_BORDER};
}}
QLabel#endpointMethodPill[method="PUT"] {{
    background-color: {METHOD_PUT_BG};
    color: {METHOD_PUT_TEXT};
    border-color: {METHOD_PUT_BORDER};
}}
QLabel#endpointMethodPill[method="PATCH"] {{
    background-color: {METHOD_PATCH_BG};
    color: {METHOD_PATCH_TEXT};
    border-color: {METHOD_PATCH_BORDER};
}}
QLabel#endpointMethodPill[method="DELETE"] {{
    background-color: {METHOD_DELETE_BG};
    color: {METHOD_DELETE_TEXT};
    border-color: {METHOD_DELETE_BORDER};
}}
/* The base rule above is GET's colouring, so a pill whose method could not be
   resolved would otherwise be indistinguishable from a real GET. Neutral
   grey keeps the unresolved state honest. */
QLabel#endpointMethodPill[method="N/A"] {{
    background-color: {SURFACE_ALT};
    color: {TEXT_MUTED};
    border-color: {BORDER};
}}
QLabel#endpointTreeAction {{
    background: transparent;
    color: {TEXT};
    font-weight: 600;
}}
QWidget#endpointTreeRow[selected="true"] QLabel#endpointTreeAction {{
    color: {PRIMARY};
}}
QLabel#endpointTreePath {{
    background: transparent;
    color: {TEXT_MUTED};
}}
/* Saved request rows. The method pill is the shared #endpointMethodPill, so
   only the three text lines need their own treatment: a prominent name, a
   muted URL, and smaller dimmer identifiers. */
/* The shared QListWidget::item padding would inset a row widget and clip its
   bottom line, so a list of row widgets opts out and lets the row's own
   margins control spacing. Property-based so any such list can reuse it. */
QListWidget[rowWidgetList="true"]::item {{
    padding: 0;
    min-height: 0;
}}
QWidget#savedRequestRow {{
    background: transparent;
}}
QLabel#savedRequestName {{
    background: transparent;
    color: {TEXT};
    font-weight: 600;
}}
QLabel#savedRequestName[missing="true"] {{
    color: {FAIL};
}}
QLabel#savedRequestUrl {{
    background: transparent;
    color: {TEXT_MUTED};
}}
QLabel#savedRequestUrl[missing="true"] {{
    color: {FAIL};
    font-style: italic;
}}
QLabel#savedRequestIds {{
    background: transparent;
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QLabel#savedRequestUsed {{
    background: transparent;
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QLabel#savedRequestDot {{
    background-color: {BORDER_STRONG};
    border-radius: 5px;
}}
QLabel#savedRequestDot[state="pass"] {{ background-color: {PASS}; }}
QLabel#savedRequestDot[state="fail"] {{ background-color: {FAIL}; }}
QWidget#collectionRow {{
    background: transparent;
}}
QLabel#collectionFolderIcon {{
    background: transparent;
}}
QLabel#collectionName {{
    background: transparent;
    color: {TEXT};
    font-weight: 600;
}}
QLabel#collectionCount {{
    background: transparent;
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QLabel#collectionCount[warn="true"] {{
    color: {WARN};
}}
QLabel#collectionMeta {{
    background: transparent;
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QWidget#listPane {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QWidget#listPaneHeader {{
    background-color: {SURFACE_ALT};
    border: none;
    border-bottom: 1px solid {BORDER};
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
}}
QLabel#listPaneTitle {{
    background: transparent;
    color: {TEXT};
    font-weight: 600;
}}
QLabel#listPaneCount {{
    background-color: {BACKGROUND};
    border: 1px solid {BORDER};
    border-radius: 9px;
    color: {TEXT_MUTED};
    font-size: 11px;
    min-width: 18px;
    padding: 1px 6px;
}}
QWidget#listPane QListWidget {{
    background: transparent;
    border: none;
}}
QWidget#listPane QTreeWidget {{
    background: transparent;
    border: none;
}}
QFrame#toolbarDivider {{
    background-color: {BORDER};
    border: none;
}}
QWidget#builderToolbar {{
    background-color: {SURFACE};
    border: none;
    border-bottom: 1px solid {BORDER};
}}
QWidget#builderStatus {{
    background-color: {SURFACE};
    border: none;
    border-top: 1px solid {BORDER};
}}
QWidget#miniHandler {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QPushButton#miniHandlerClose {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
}}
QPushButton#miniHandlerClose:hover {{
    background-color: {BACKGROUND};
    border-color: {BORDER};
}}
QLabel#miniHandlerUrl {{
    background: transparent;
    color: {TEXT_MUTED};
}}
QLabel#miniHandlerCaption {{
    background: transparent;
    color: {TEXT_MUTED};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#miniHandlerError {{
    background: transparent;
    color: {FAIL};
    font-size: 11px;
}}
QPlainTextEdit#miniHandlerEditor {{
    background-color: {BACKGROUND};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {TEXT};
    padding: 6px;
}}
QLabel#miniHandlerChip {{
    background-color: {BACKGROUND};
    border: 1px solid {BORDER};
    border-radius: 9px;
    color: {TEXT_MUTED};
    font-size: 11px;
    padding: 2px 8px;
}}
QLabel#miniHandlerStatus {{
    border: 1px solid {BORDER};
    border-radius: 9px;
    color: {TEXT_INVERSE};
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
}}
QLabel#miniHandlerStatus[state="pass"] {{
    background-color: {PASS};
    border-color: {PASS};
}}
QLabel#miniHandlerStatus[state="fail"] {{
    background-color: {FAIL};
    border-color: {FAIL};
}}
QToolButton#endpointActionMenu {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 2px;
}}
QToolButton#endpointActionMenu:hover {{
    background-color: {SURFACE_ALT};
    border-color: {BORDER};
}}
QToolButton#endpointActionMenu:pressed {{
    background-color: {BORDER};
}}
QToolButton#endpointActionMenu::menu-indicator {{
    image: none;
    width: 0;
}}
QTableWidget#parametersTable {{
    border: none;
    background: {SURFACE};
}}
QWidget#caseResultSummary,
QWidget#caseResultColumn,
QWidget#caseResultStack {{
    background: transparent;
}}
QLabel#caseResultOutcome {{
    background: transparent;
    color: {TEXT};
    font-size: 11pt;
    font-weight: 700;
}}
QLabel#caseResultOutcome[outcome="pass"] {{ color: {PASS}; }}
QLabel#caseResultOutcome[outcome="fail"] {{ color: {FAIL}; }}
QLabel#caseResultOutcome[outcome="error"] {{ color: {WARN}; }}
QLabel#caseResultOutcome[outcome="skipped"] {{ color: {SKIP}; }}
QLabel#caseResultColumnTitle {{
    color: {TEXT_MUTED};
    font-weight: 600;
    padding-left: 2px;
}}
QTableWidget#caseResultTable {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    background-color: {SURFACE};
}}
QSplitter#caseResultTablesSplitter::handle {{
    background-color: transparent;
}}
QSplitter#caseResultTablesSplitter {{
    background-color: transparent;
}}
QWidget#tableEmptyState {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QWidget#emptyState {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QWidget#emptyState[tableOverlay="true"] {{
    background-color: {SURFACE};
    border: none;
    border-radius: 0;
}}
QLabel#emptyStateIcon {{
    background: transparent;
    border: none;
}}
QLabel[emptyStateTitle="true"] {{
    color: {TEXT};
    font-size: 11pt;
    font-weight: 600;
}}
QLabel[emptyStateGuidance="true"] {{
    color: {TEXT_MUTED};
    font-size: 9pt;
}}
QPushButton[emptyStateAction="true"] {{
    margin-top: 4px;
}}

/* Pagination — the split pill used by every pager in the app. Prev is a ghost
   button, Next carries the primary fill as the forward action.
   The per-button rules repeat the ``QWidget#pager`` ancestor: without it the
   descendant rule above outranks them on CSS specificity and Next loses its
   fill. */
QWidget#pager {{
    background-color: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 16px;
}}
QWidget#pager QPushButton {{
    background-color: transparent;
    border: none;
    padding: 0px;
    /* The global QPushButton rule pins every button to 22px tall and adds
       padding; the pager's buttons must instead fill the pill so the primary
       Next reaches its rounded edge. These metrics mirror Pager._CONTROL_HEIGHT
       and Pager._BUTTON_WIDTH — a bare ``min-width: 0`` here would let the
       layout squeeze the chevrons down to their icon size. */
    min-width: 34px;
    max-width: 34px;
    min-height: 30px;
    max-height: 30px;
}}
QWidget#pager QPushButton#pagerPrev {{
    background-color: {PRIMARY};
    border-top-left-radius: 15px;
    border-bottom-left-radius: 15px;
    /* The global QPushButton rule rounds all four corners; the inner edge must
       stay square so the filled end butts flush against the page box. */
    border-top-right-radius: 0px;
    border-bottom-right-radius: 0px;
}}
QWidget#pager QPushButton#pagerPrev:hover:enabled {{ background-color: {PRIMARY_HOVER}; }}
QWidget#pager QPushButton#pagerPrev:pressed:enabled {{ background-color: {PRIMARY_PRESSED}; }}
QWidget#pager QPushButton#pagerPrev:disabled {{ background-color: {SURFACE_ALT}; }}
QWidget#pager QPushButton#pagerNext {{
    background-color: {PRIMARY};
    border-top-right-radius: 15px;
    border-bottom-right-radius: 15px;
    border-top-left-radius: 0px;
    border-bottom-left-radius: 0px;
}}
QWidget#pager QPushButton#pagerNext:hover:enabled {{ background-color: {PRIMARY_HOVER}; }}
QWidget#pager QPushButton#pagerNext:pressed:enabled {{ background-color: {PRIMARY_PRESSED}; }}
QWidget#pager QPushButton#pagerNext:disabled {{ background-color: {SURFACE_ALT}; }}
QWidget#pager QLineEdit#pagerPage {{
    background-color: transparent;
    border: none;
    border-radius: 0px;
    color: {TEXT};
    font-weight: 700;
    padding: 0px;
    min-height: 30px;
    max-height: 30px;
}}
QWidget#pager QLineEdit#pagerPage:focus {{
    background-color: {HOVER_SOFT};
}}
QWidget#pager QLabel#pagerTotal {{
    background-color: transparent;
    border: none;
    color: {TEXT_MUTED};
    padding-left: 2px;
    padding-right: 10px;
}}

QSplitter::handle {{ background-color: transparent; }}
QSplitterHandle#endpointExplorerHandle {{
    background-color: {SURFACE};
}}
QSplitterHandle#endpointExplorerHandle:hover {{
    background-color: {SURFACE_ALT};
}}
QSplitterHandle#endpointExplorerHandle:pressed {{
    background-color: {SELECT_SOFT};
}}
QSplitter::handle:hover {{ background-color: {HOVER_SOFT}; }}
QSplitter::handle:pressed {{ background-color: {SELECT_SOFT}; }}
QSplitter::handle:horizontal {{ width: 7px; margin: 0 2px; }}
QSplitter::handle:vertical {{ height: 7px; margin: 2px 0; }}

QPlainTextEdit#responseCodeEditor {{
    background-color: {CODE_BACKGROUND};
    color: {CODE_TEXT};
    border: 1px solid {CODE_BORDER};
    border-radius: 7px;
    padding: 8px;
    selection-background-color: {CODE_SELECTION};
    selection-color: {TEXT};
}}

QScrollArea {{ background-color: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle {{ background: {BORDER_STRONG}; border-radius: 5px; min-height: 28px; min-width: 28px; }}
QScrollBar::handle:hover {{ background: {TEXT_MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QStatusBar {{ background-color: {SURFACE_ALT}; color: {TEXT_MUTED}; }}
QMenu {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 5px;
    font-size: 9pt;
    font-weight: 500;
}}
QMenu::item {{
    min-height: 20px;
    padding: 7px 34px 7px 12px;
    margin: 2px 3px;
    border: 1px solid transparent;
    border-radius: 5px;
    color: {TEXT};
    spacing: 8px;
}}
QMenu::item:selected {{
    background-color: {HOVER_SOFT};
    color: {PRIMARY};
    border-color: {BORDER};
    font-weight: 600;
}}
QMenu::item:disabled {{
    color: {TEXT_MUTED};
}}
QMenu::separator {{
    height: 1px;
    margin: 5px 10px;
    background-color: {BORDER};
}}
QMenu::icon {{
    margin-left: 5px;
}}
QMenu::right-arrow {{
    image: url({MENU_ARROW});
    width: 8px;
    height: 12px;
    subcontrol-origin: padding;
    subcontrol-position: right center;
    right: 12px;
}}
QToolTip {{
    background-color: {TEXT};
    color: {BACKGROUND};
    border: none;
    padding: 4px 6px;
}}
QProgressBar {{
    background-color: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
}}
QProgressBar::chunk {{ background-color: {PRIMARY}; border-radius: 3px; }}
QWidget#suiteProgressPanel {{
    background-color: {HOVER_SOFT};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel#suiteProgressStatus {{
    color: {SELECT_SOFT_TEXT};
    font-weight: 600;
}}
QLabel#suiteProgressCount {{
    color: {TEXT_MUTED};
    font-weight: 600;
}}
QProgressBar#suiteRunProgress {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 1px;
}}
QProgressBar#suiteRunProgress::chunk {{
    background: qlineargradient(
        x1: 0, y1: 0, x2: 1, y2: 0,
        stop: 0 {PRIMARY}, stop: 1 {ACCENT}
    );
    border-radius: 3px;
}}

QLabel[heading="true"] {{ font-size: 11pt; font-weight: 600; }}

QFrame#dataRunnerStatCard {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel#dataRunnerStatCardTitle {{
    color: {TEXT_MUTED};
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 0.4px;
}}
QLabel#dataRunnerStatCardValue {{
    font-size: 17pt;
    font-weight: 700;
}}
QLabel#dataRunnerStatCardSubtitle {{
    color: {TEXT_MUTED};
    font-size: 8pt;
}}
QFrame#dataRunnerContextCard {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel#dataRunnerContextCardTitle {{
    color: {TEXT_MUTED};
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 0.4px;
}}
/* Plain layout containers that sit on a card or group-box surface: without this
   they inherit the global QWidget background and paint a grey block. The
   property form is set automatically on every AccordionSection content pane. */
QWidget#dataRunnerTransparentPane,
QWidget[transparentPane="true"] {{
    background-color: transparent;
}}
QLabel#dataRunnerWriteWarning {{
    background-color: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {WARN};
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 8pt;
}}
QFrame#loadTestingRunHeader,
QFrame#loadTestingStageStrip,
QFrame#loadTestingSectionCard {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel#loadTestingHeroIcon {{
    background-color: {ACCENT_SOFT};
    color: {PRIMARY};
    border-radius: 8px;
    font-size: 18pt;
    font-weight: 700;
}}
QLabel#loadTestingRunTitle {{
    color: {TEXT};
    font-size: 12pt;
    font-weight: 700;
}}
QLabel#loadTestingRunSubtitle {{
    color: {TEXT_MUTED};
    font-size: 8pt;
}}
QFrame#loadTestingStatusPill {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QFrame#loadTestingStatusPill[status="running"],
QFrame#loadTestingStatusPill[status="pass"] {{
    border-color: {PASS};
}}
QFrame#loadTestingStatusPill[status="fail"],
QFrame#loadTestingStatusPill[status="aborted"] {{
    border-color: {FAIL};
}}
QFrame#loadTestingStatusPill[status="stopped"],
QFrame#loadTestingStatusPill[status="inconclusive"] {{
    border-color: {WARN};
}}
QLabel#loadTestingStatusPillText {{
    background: transparent;
    color: {TEXT_MUTED};
    font-size: 8pt;
    font-weight: 700;
}}
QLabel#loadTestingStatusPillText[status="running"],
QLabel#loadTestingStatusPillText[status="pass"] {{
    color: {PASS};
}}
QLabel#loadTestingStatusPillText[status="fail"],
QLabel#loadTestingStatusPillText[status="aborted"] {{
    color: {FAIL};
}}
QLabel#loadTestingStatusPillText[status="stopped"],
QLabel#loadTestingStatusPillText[status="inconclusive"] {{
    color: {WARN};
}}
QFrame#loadTestingFindingCard {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-left: 3px solid {PRIMARY};
    border-radius: 6px;
}}
QFrame#loadTestingFindingCard[severity="critical"],
QFrame#loadTestingFindingCard[severity="high"] {{
    border-left-color: {FAIL};
}}
QFrame#loadTestingFindingCard[severity="medium"] {{
    border-left-color: {WARN};
}}
QFrame#loadTestingFindingCard QLabel {{
    background: transparent;
}}
QLabel#loadTestingFindingTitle {{
    color: {TEXT};
    font-weight: 700;
}}
QLabel#loadTestingFindingHeading {{
    color: {TEXT_MUTED};
    font-size: 7.5pt;
    font-weight: 700;
}}
QLabel#loadTestingSeverityPill {{
    background-color: {SURFACE};
    color: {PRIMARY};
    border: 1px solid {PRIMARY};
    border-radius: 9px;
    padding: 1px 8px;
    font-size: 7.5pt;
    font-weight: 700;
}}
QLabel#loadTestingSeverityPill[severity="critical"],
QLabel#loadTestingSeverityPill[severity="high"] {{
    color: {FAIL};
    border-color: {FAIL};
}}
QLabel#loadTestingSeverityPill[severity="medium"] {{
    color: {WARN};
    border-color: {WARN};
}}
QLabel#pdfExportInfo,
QLabel#pdfExportWarning {{
    background-color: {SURFACE_ALT};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-left: 3px solid {PASS};
    border-radius: 6px;
    padding: 6px 8px;
}}
QLabel#pdfExportWarning {{
    border-left-color: {WARN};
}}
QFrame#dataRunnerTemplateBanner {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-left: 3px solid {ACCENT};
    border-radius: 6px;
}}
QFrame#dataRunnerTemplateBanner QLabel {{
    background: transparent;
    color: {TEXT};
}}
QLabel#aboutTitle {{
    color: {TEXT};
    font-size: 22px;
    font-weight: 700;
}}
QLabel#aboutVersion,
QLabel#aboutTagline,
QLabel#aboutNote,
QLabel#aboutFootnote {{
    color: {TEXT_MUTED};
}}
QLabel#aboutFeatureName {{
    color: {TEXT};
    font-weight: 700;
}}
QLabel#aboutLicenseBadge {{
    background-color: {ACCENT_SOFT};
    color: {PRIMARY};
    border-radius: 9px;
    padding: 2px 10px;
    font-weight: 600;
}}
QLabel#aboutNote {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 8px;
}}
QLabel#loadTestingSectionTitle,
QLabel#loadTestingContextValue {{
    color: {TEXT};
    font-weight: 700;
}}
QFrame#loadTestingSavedRunChip {{
    background-color: {ACCENT_SOFT};
    border: 1px solid {ACCENT};
    border-radius: 12px;
}}
QLabel#loadTestingSavedRunChipText {{
    background: transparent;
    color: {PRIMARY};
    font-size: 8pt;
    font-weight: 600;
}}
QToolButton#loadTestingSavedRunChipClose {{
    background: transparent;
    border: none;
    border-radius: 9px;
    padding: 3px;
}}
QToolButton#loadTestingSavedRunChipClose:hover {{
    background-color: {SURFACE};
}}
QTableWidget#loadTestingReportTable {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QLabel#loadTestingEndpointMethod {{
    background-color: {ACCENT_SOFT};
    color: {PRIMARY};
    border-radius: 4px;
    padding: 3px 6px;
    font-size: 8pt;
    font-weight: 700;
}}
QLabel#loadTestingRequestPath {{
    color: {TEXT};
    font-weight: 600;
}}
QLabel#loadTestingWarning {{
    background-color: {SURFACE_ALT};
    color: {WARN};
    border: 1px solid {WARN};
    border-radius: 6px;
    padding: 8px;
    font-size: 8pt;
}}
QProgressBar#loadTestingStageProgress {{
    background-color: {SURFACE_ALT};
    border: none;
    border-radius: 3px;
    max-height: 6px;
}}
QProgressBar#loadTestingStageProgress::chunk {{
    background-color: {ACCENT};
    border-radius: 3px;
}}
QTabWidget#loadTestingResultsTabs::pane {{
    border: none;
    border-top: 1px solid {BORDER};
    top: -1px;
}}
"""


#: Token values for each theme. Every colour the template needs is explicit.
_SHARED_TOKENS = {
    "PASS": PASS,
    "FAIL": FAIL,
    "WARN": WARN,
    "SKIP": SKIP,
    "ACCENT": ACCENT,
    "PATCH": PATCH,
}

LIGHT_TOKENS = {
    **_SHARED_TOKENS,
    **_method_pill_tokens(dark=False),
    "BACKGROUND": BACKGROUND,
    "SURFACE": SURFACE,
    "SURFACE_ALT": SURFACE_ALT,
    "BORDER": BORDER,
    "BORDER_STRONG": BORDER_STRONG,
    "TEXT": TEXT,
    "TEXT_MUTED": TEXT_MUTED,
    "TEXT_INVERSE": TEXT_INVERSE,
    "PRIMARY": PRIMARY,
    "PRIMARY_HOVER": PRIMARY_HOVER,
    "PRIMARY_PRESSED": PRIMARY_PRESSED,
    "NAVIGATION": NAVIGATION,
    "NAV_TEXT": NAV_TEXT,
    "NAV_MUTED": NAV_MUTED,
    "NAV_HOVER": NAV_HOVER,
    "ACCENT_SOFT": ACCENT_SOFT,
    "ACCENT_SOFT_TEXT": ACCENT_SOFT_TEXT,
    "HOVER_SOFT": HOVER_SOFT,
    "SELECT_SOFT": SELECT_SOFT,
    "SELECT_SOFT_TEXT": SELECT_SOFT_TEXT,
    "DANGER_SOFT": DANGER_SOFT,
    "SUCCESS_SOFT": SUCCESS_SOFT,
    "CONNECTED_BG": SUCCESS_SOFT,
    "CONNECTED_BORDER": "#5fc98a",
    "CONNECTED_TEXT": "#15803d",
    "CONNECTED_DOT": "#22c55e",
    "DISCONNECTED_BG": DANGER_SOFT,
    "DISCONNECTED_BORDER": "#f0a3a3",
    "DISCONNECTED_TEXT": "#b91c1c",
    "DISCONNECTED_DOT": "#ef4444",
    "CODE_BACKGROUND": CODE_BACKGROUND,
    "CODE_TEXT": CODE_TEXT,
    "CODE_BORDER": CODE_BORDER,
    "CODE_SELECTION": CODE_SELECTION,
    "JSON_KEY": JSON_KEY,
    "JSON_STRING": JSON_STRING,
    "JSON_NUMBER": JSON_NUMBER,
    "JSON_KEYWORD": JSON_KEYWORD,
    "JSON_PUNCTUATION": JSON_PUNCTUATION,
    "JSON_ERROR": JSON_ERROR,
    "COMBO_ARROW": (ASSET_DIR / "chevron-down-light.svg").as_posix(),
    "NAV_COMBO_ARROW": (ASSET_DIR / "chevron-down-dark.svg").as_posix(),
    "SPIN_UP_ARROW": (ASSET_DIR / "chevron-up-light.svg").as_posix(),
    "TAB_LEFT_ARROW": (ASSET_DIR / "chevron-left-light.svg").as_posix(),
    "TAB_RIGHT_ARROW": (ASSET_DIR / "chevron-right-light.svg").as_posix(),
    "MENU_ARROW": (ASSET_DIR / "chevron-right-light.svg").as_posix(),
    "CHECK_MARK": (ASSET_DIR / "check.svg").as_posix(),
    "CHECK_DASH": (ASSET_DIR / "check-dash.svg").as_posix(),
    "CHECK_MARK_MUTED": (ASSET_DIR / "check-muted.svg").as_posix(),
    "SHADOW_EDGE": "rgba(23, 48, 82, 48)",
    "SHADOW_MID": "rgba(23, 48, 82, 18)",
}

DARK_TOKENS = {
    **_SHARED_TOKENS,
    **_method_pill_tokens(dark=True),
    "BACKGROUND": "#0b1728",
    "SURFACE": "#132539",
    "SURFACE_ALT": "#1b3049",
    "BORDER": "#2d4763",
    "BORDER_STRONG": "#4a6a93",
    "TEXT": "#eaf1fb",
    "TEXT_MUTED": "#a9bdd6",
    "TEXT_INVERSE": "#ffffff",
    "PRIMARY": "#3b9bff",
    "PRIMARY_HOVER": "#57aaff",
    "PRIMARY_PRESSED": "#2b84e0",
    "NAVIGATION": "#061b33",
    "NAV_TEXT": "#dce9f8",
    "NAV_MUTED": "#b8cbe2",
    "NAV_HOVER": "#12395f",
    "ACCENT_SOFT": "#1d4470",
    "ACCENT_SOFT_TEXT": "#cfe4ff",
    "HOVER_SOFT": "#1b3049",
    "SELECT_SOFT": "#1d4470",
    "SELECT_SOFT_TEXT": "#eaf1fb",
    "DANGER_SOFT": "#5a2027",
    "SUCCESS_SOFT": "#14432a",
    "CONNECTED_BG": "#14432a",
    "CONNECTED_BORDER": "#3f9c68",
    "CONNECTED_TEXT": "#7ee2a8",
    "CONNECTED_DOT": "#4ade80",
    "DISCONNECTED_BG": "#4d1f25",
    "DISCONNECTED_BORDER": "#a7444c",
    "DISCONNECTED_TEXT": "#ff9f9f",
    "DISCONNECTED_DOT": "#f87171",
    "CODE_BACKGROUND": "#0a1a2c",
    "CODE_TEXT": "#dbe8f8",
    "CODE_BORDER": "#274259",
    "CODE_SELECTION": "#1d5fa8",
    "JSON_KEY": "#7fb5ff",
    "JSON_STRING": "#79d69f",
    "JSON_NUMBER": "#f2b880",
    "JSON_KEYWORD": "#d3a5f5",
    "JSON_PUNCTUATION": "#a9bdd6",
    "JSON_ERROR": "#ff8a8a",
    "COMBO_ARROW": (ASSET_DIR / "chevron-down-dark.svg").as_posix(),
    "NAV_COMBO_ARROW": (ASSET_DIR / "chevron-down-dark.svg").as_posix(),
    "SPIN_UP_ARROW": (ASSET_DIR / "chevron-up-dark.svg").as_posix(),
    "TAB_LEFT_ARROW": (ASSET_DIR / "chevron-left-dark.svg").as_posix(),
    "TAB_RIGHT_ARROW": (ASSET_DIR / "chevron-right-dark.svg").as_posix(),
    "MENU_ARROW": (ASSET_DIR / "chevron-right-dark.svg").as_posix(),
    "CHECK_MARK": (ASSET_DIR / "check.svg").as_posix(),
    "CHECK_DASH": (ASSET_DIR / "check-dash.svg").as_posix(),
    "CHECK_MARK_MUTED": (ASSET_DIR / "check-muted.svg").as_posix(),
    "SHADOW_EDGE": "rgba(0, 0, 0, 88)",
    "SHADOW_MID": "rgba(0, 0, 0, 34)",
}


def build_stylesheet(tokens: dict[str, str]) -> str:
    return _TEMPLATE.format(**tokens)


STYLESHEET = build_stylesheet(LIGHT_TOKENS)

#: Tokens of the palette currently applied to the application.
ACTIVE_TOKENS = dict(LIGHT_TOKENS)

#: Incremented on every theme change so cached colours can be rebuilt.
PALETTE_VERSION = 0


def is_dark_mode(app, mode: str) -> bool:
    if mode == "Dark":
        return True
    if mode != "System" or app is None:
        return False
    return app.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _activate(tokens: dict[str, str]) -> None:
    """Refreshes module colours so runtime styling follows the active theme."""
    global ACTIVE_TOKENS, PALETTE_VERSION
    global BACKGROUND, SURFACE, SURFACE_ALT, BORDER, BORDER_STRONG
    global TEXT, TEXT_MUTED, TEXT_INVERSE
    global PRIMARY, PRIMARY_HOVER, PRIMARY_PRESSED, NAVIGATION
    global NAV_TEXT, NAV_MUTED, NAV_HOVER
    global ACCENT_SOFT, ACCENT_SOFT_TEXT, HOVER_SOFT
    global SELECT_SOFT, SELECT_SOFT_TEXT, DANGER_SOFT, SUCCESS_SOFT
    global CODE_BACKGROUND, CODE_TEXT, CODE_BORDER, CODE_SELECTION
    global JSON_KEY, JSON_STRING, JSON_NUMBER, JSON_KEYWORD
    global JSON_PUNCTUATION, JSON_ERROR

    ACTIVE_TOKENS = dict(tokens)
    PALETTE_VERSION += 1
    BACKGROUND = tokens["BACKGROUND"]
    SURFACE = tokens["SURFACE"]
    SURFACE_ALT = tokens["SURFACE_ALT"]
    BORDER = tokens["BORDER"]
    BORDER_STRONG = tokens["BORDER_STRONG"]
    TEXT = tokens["TEXT"]
    TEXT_MUTED = tokens["TEXT_MUTED"]
    TEXT_INVERSE = tokens["TEXT_INVERSE"]
    PRIMARY = tokens["PRIMARY"]
    PRIMARY_HOVER = tokens["PRIMARY_HOVER"]
    PRIMARY_PRESSED = tokens["PRIMARY_PRESSED"]
    NAVIGATION = tokens["NAVIGATION"]
    NAV_TEXT = tokens["NAV_TEXT"]
    NAV_MUTED = tokens["NAV_MUTED"]
    NAV_HOVER = tokens["NAV_HOVER"]
    ACCENT_SOFT = tokens["ACCENT_SOFT"]
    ACCENT_SOFT_TEXT = tokens["ACCENT_SOFT_TEXT"]
    HOVER_SOFT = tokens["HOVER_SOFT"]
    SELECT_SOFT = tokens["SELECT_SOFT"]
    SELECT_SOFT_TEXT = tokens["SELECT_SOFT_TEXT"]
    DANGER_SOFT = tokens["DANGER_SOFT"]
    SUCCESS_SOFT = tokens["SUCCESS_SOFT"]
    CODE_BACKGROUND = tokens["CODE_BACKGROUND"]
    CODE_TEXT = tokens["CODE_TEXT"]
    CODE_BORDER = tokens["CODE_BORDER"]
    CODE_SELECTION = tokens["CODE_SELECTION"]
    JSON_KEY = tokens["JSON_KEY"]
    JSON_STRING = tokens["JSON_STRING"]
    JSON_NUMBER = tokens["JSON_NUMBER"]
    JSON_KEYWORD = tokens["JSON_KEYWORD"]
    JSON_PUNCTUATION = tokens["JSON_PUNCTUATION"]
    JSON_ERROR = tokens["JSON_ERROR"]


def apply_theme(app, mode: str = "Light") -> None:
    """Applies the palette for ``mode`` to a QApplication."""
    tokens = DARK_TOKENS if is_dark_mode(app, mode) else LIGHT_TOKENS
    _activate(tokens)
    if app is not None:
        app.setStyleSheet(build_stylesheet(tokens))
