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
#: Row height for tables whose cells are mostly inline editors: the default
#: 30px plus 5px, paired with the ``#roomyEditorTable`` stylesheet rule.
ROOMY_ROW_HEIGHT = 35

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
QWidget#environmentToolbar {{
    background-color: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}
QWidget#environmentToolbar[shellRegion="environment"] {{
    background-color: {SURFACE_ALT};
}}
QLabel#environmentBaseUrl {{
    background: transparent;
    color: {TEXT_MUTED};
    padding: 0 8px;
}}
QPushButton#editEnvironmentButton {{
    min-width: 112px;
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
QLabel#connectionBadge {{
    background-color: {DANGER_SOFT};
    color: {FAIL};
    border-radius: 12px;
    padding: 6px 10px;
    font-weight: 600;
}}
QLabel#connectionBadge[connected="true"] {{
    background-color: {SUCCESS_SOFT};
    color: {PASS};
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
QLabel#navigationStats, QLabel#navigationVersion {{
    background: transparent;
    color: {NAV_MUTED};
    padding: 8px;
}}
QListWidget#navigationRail::item {{
    border-radius: 6px;
    padding: 12px 9px;
    margin-bottom: 3px;
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
QLabel#endpointSource {{
    color: {TEXT_MUTED};
    background: transparent;
    padding: 0 2px 3px 2px;
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
QWidget#responseViewer {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QWidget#responseStatusBar {{
    background-color: {SURFACE};
    border-bottom: 1px solid {BORDER};
}}

QGroupBox {{
    background-color: transparent;
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 4px;
    background: transparent;
    color: {TEXT_MUTED};
}}
QGroupBox#summaryTile {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
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
QWidget#accordionContent {{
    background-color: {SURFACE_ALT};
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
    width: 15px;
    height: 15px;
    background-color: {SURFACE};
    border: 1px solid {BORDER_STRONG};
    border-radius: 4px;
}}
QRadioButton::indicator {{ border-radius: 8px; }}
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
    border: 2px solid {SURFACE};
}}
QCheckBox::indicator:indeterminate, QTableWidget::indicator:indeterminate,
QTreeWidget::indicator:indeterminate, QListWidget::indicator:indeterminate {{
    background-color: {PRIMARY_HOVER};
    border: 2px solid {SURFACE};
}}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled,
QTableWidget::indicator:disabled, QTreeWidget::indicator:disabled,
QListWidget::indicator:disabled {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
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
    top: 0;
}}
QTabBar::tab {{
    background-color: {SURFACE_ALT};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 8px 16px;
    margin-right: 5px;
    margin-bottom: 6px;
}}
QTabBar::tab:hover {{
    background-color: {HOVER_SOFT};
    border-color: {BORDER_STRONG};
    color: {TEXT};
}}
QTabBar::tab:selected {{
    background-color: {SELECT_SOFT};
    border: 1px solid {PRIMARY};
    color: {SELECT_SOFT_TEXT};
    font-weight: 600;
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
    background: {SURFACE};
}}
QTabWidget#requestBuilderTabs::pane {{
    border: none;
    background: {SURFACE};
}}

QHeaderView {{ background-color: {SURFACE_ALT}; }}
QHeaderView::section {{
    background-color: {SURFACE_ALT};
    color: {TEXT_MUTED};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 5px 8px;
    font-weight: 600;
}}
QHeaderView::section:hover {{ background-color: {HOVER_SOFT}; }}
QTableCornerButton::section {{
    background-color: {SURFACE_ALT};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
}}
QTableWidget, QTableView, QTreeWidget, QTreeView, QListWidget, QListView {{
    background-color: {SURFACE};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    alternate-background-color: {SURFACE_ALT};
    selection-background-color: {SELECT_SOFT};
    selection-color: {SELECT_SOFT_TEXT};
    gridline-color: transparent;
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
    min-height: 28px;
    padding: 5px 4px;
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
QLabel#emptyStateTitle {{
    color: {TEXT};
    font-size: 11pt;
    font-weight: 600;
}}
QLabel#emptyStateDescription {{
    color: {TEXT_MUTED};
    font-size: 9pt;
}}
QWidget#emptyState {{
    background-color: {SURFACE_ALT};
    border: 1px solid {BORDER};
    border-radius: 8px;
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
}}
QMenu::item:selected {{ background-color: {PRIMARY}; color: {TEXT_INVERSE}; }}
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
QLabel#loadTestingStatusPill {{
    background-color: {SURFACE_ALT};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 3px 9px;
    font-size: 8pt;
    font-weight: 700;
}}
QLabel#loadTestingStatusPill[status="running"],
QLabel#loadTestingStatusPill[status="pass"] {{
    background-color: {SURFACE_ALT};
    color: {PASS};
    border-color: {PASS};
}}
QLabel#loadTestingStatusPill[status="fail"] {{
    background-color: {SURFACE_ALT};
    color: {FAIL};
    border-color: {FAIL};
}}
QLabel#loadTestingStatusPill[status="stopped"] {{
    background-color: {SURFACE_ALT};
    color: {WARN};
    border-color: {WARN};
}}
QLabel#loadTestingSectionTitle,
QLabel#loadTestingContextValue {{
    color: {TEXT};
    font-weight: 700;
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
    "SPIN_UP_ARROW": (ASSET_DIR / "chevron-up-light.svg").as_posix(),
    "TAB_LEFT_ARROW": (ASSET_DIR / "chevron-left-light.svg").as_posix(),
    "TAB_RIGHT_ARROW": (ASSET_DIR / "chevron-right-light.svg").as_posix(),
    "SHADOW_EDGE": "rgba(23, 48, 82, 48)",
    "SHADOW_MID": "rgba(23, 48, 82, 18)",
}

DARK_TOKENS = {
    **_SHARED_TOKENS,
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
    "SPIN_UP_ARROW": (ASSET_DIR / "chevron-up-dark.svg").as_posix(),
    "TAB_LEFT_ARROW": (ASSET_DIR / "chevron-left-dark.svg").as_posix(),
    "TAB_RIGHT_ARROW": (ASSET_DIR / "chevron-right-dark.svg").as_posix(),
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
