"""Shared, passive security-review report viewer.

The supplied scanner owns the review scope; this dialog never sends requests.
Reports and finding text are untrusted and are only displayed as plain text.
"""

from __future__ import annotations

import json
import threading
import html
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from PyQt6.QtCore import QDate, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QTextDocument
from PyQt6.QtPrintSupport import QPrinter
from PyQt6.QtWidgets import (
    QAbstractItemView, QBoxLayout, QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QTableWidget, QSizePolicy, QStackedWidget,
    QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QGridLayout, QScrollArea, QFrame,
)

from .security import SecurityReport, report_csv, report_html, safe_text
from . import theme
from .icons import app_icon, icon
from .widgets import attach_table_empty_state

if TYPE_CHECKING:
    from .workspace_store import WorkspaceStore


_DISPOSITIONS = ("open", "acknowledged", "accepted risk", "false positive")


def _plain(value: object) -> str:
    text = str(value)
    text = safe_text(text, limit=len(text))
    return "".join(c for c in text if c >= " " or c in "\n\t")


def _label(text: str = "") -> QLabel:
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(_plain(text))
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _inherit_report(store, report: SecurityReport) -> SecurityReport:
    inherit = getattr(store, "inherit_security_dispositions", None)
    if callable(inherit):
        payload = inherit(report.to_dict())
        if isinstance(payload, dict):
            return SecurityReport.from_dict(payload)
    return report


class _ScanThread(QThread):
    report_ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, scan: Callable[[threading.Event], SecurityReport],
                 cancel: threading.Event, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scan = scan
        self._cancel = cancel

    def run(self) -> None:
        try:
            report = self._scan(self._cancel)
            if not isinstance(report, SecurityReport):
                raise TypeError("Scanner did not return a SecurityReport.")
            self.report_ready.emit(report)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class SecurityReviewDialog(QDialog):
    """Review a cancellable background scan, or a saved historical snapshot."""

    def __init__(self, scan: Callable[[threading.Event], SecurityReport],
                 parent: QWidget | None = None, *,
                 store: WorkspaceStore | None = None,
                 current_fingerprint: Callable[[], str] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("API Security Review")
        self.setWindowIcon(app_icon())
        self.setObjectName("securityReview")
        available = self.screen().availableGeometry()
        self.resize(min(1380, int(available.width() * 0.9)), min(960, int(available.height() * 0.9)))
        self.setMinimumSize(600, 400)
        self._scan = scan
        self._store = store
        self._current_fingerprint = current_fingerprint
        self._historical = False
        self._comparison_text = ""
        self._cancel = threading.Event()
        self._thread: _ScanThread | None = None
        self._started = False
        self._running = False
        self._close_pending = False
        self._pending_report: SecurityReport | None = None
        self._failure = ""
        self.report: SecurityReport | None = None
        self._visible_findings: list[int] = []

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.scroll_area.setWidget(self.content)
        root_layout.addWidget(self.scroll_area, 1)
        layout = QVBoxLayout(self.content)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(12)
        hero = QWidget()
        hero.setObjectName("securityHero")
        hero_layout = QHBoxLayout(hero)
        self._hero_layout = hero_layout
        hero_layout.setContentsMargins(16, 12, 16, 16)
        hero_layout.setSpacing(18)
        hero_intro = QWidget()
        hero_intro_layout = QHBoxLayout(hero_intro)
        hero_intro_layout.setContentsMargins(0, 0, 0, 0)
        hero_intro_layout.setSpacing(18)
        hero_intro_layout.addWidget(self._icon_tile("security-shield", 54), 0, Qt.AlignmentFlag.AlignTop)
        hero_copy = QVBoxLayout()
        title_row = QHBoxLayout()
        self.heading = _label("API Security Review")
        self.heading.setObjectName("securityHeading")
        self.state_badge = _label("Ready")
        self.state_badge.setObjectName("securityStateBadge")
        title_row.addWidget(self.heading)
        title_row.addWidget(self.state_badge)
        title_row.addStretch()
        hero_copy.addLayout(title_row)
        self.disclaimer = _label(
            "Evidence-based security review. This is not a compliance certification, "
            "penetration test, or assurance that the API is secure. The selected review "
            "mode defines its scope; unassessed coverage requires independent verification."
        )
        self.disclaimer.setObjectName("securityMuted")
        hero_copy.addWidget(self.disclaimer)
        hero_intro_layout.addLayout(hero_copy, 1)
        hero_layout.addWidget(hero_intro, 1)
        freshness_card = QWidget()
        freshness_card.setObjectName("securityFreshness")
        freshness_layout = QHBoxLayout(freshness_card)
        freshness_layout.addWidget(self._icon_tile("info-circle", 24))
        freshness_copy = QVBoxLayout()
        self.freshness_title = _label("Evidence freshness")
        self.freshness_title.setObjectName("securityAccentTitle")
        self.freshness = _label()
        freshness_copy.addWidget(self.freshness_title)
        freshness_copy.addWidget(self.freshness)
        freshness_layout.addLayout(freshness_copy)
        freshness_card.setMaximumWidth(345)
        self._freshness_card = freshness_card
        hero_layout.addWidget(freshness_card, 1)
        layout.addWidget(hero)
        self._freshness_timer = QTimer(self)
        self._freshness_timer.setInterval(1000)
        self._freshness_timer.timeout.connect(self._update_freshness)
        self._freshness_timer.start()
        self.metadata = _label()
        self.summary = _label("Waiting to start review.")
        self.metadata.setParent(self)
        self.summary.setParent(self)
        self.metadata.hide()
        self.summary.hide()
        metadata_row = QGridLayout()
        self._metadata_layout = metadata_row
        self._metadata_cards: list[QWidget] = []
        metadata_row.setSpacing(10)
        self.metadata_values: dict[str, QLabel] = {}
        for key, title, glyph in (
            ("mode", "Mode", "sliders-v"), ("scope", "Scope", "security-scope"),
            ("created_at", "Created", "calendar"), ("catalog_fingerprint", "Catalog fingerprint", "fingerprint"),
            ("environment", "Environment", "security-environment"),
        ):
            card = QWidget()
            card.setObjectName("securityCard")
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 10, 12, 10)
            row.addWidget(self._icon_tile(glyph, 34))
            copy = QVBoxLayout()
            copy.setSpacing(3)
            caption = _label(title)
            caption.setObjectName("securityMuted")
            value = _label("Not available")
            value.setWordWrap(False)
            value.setMinimumWidth(0)
            value.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            self.metadata_values[key] = value
            copy.addWidget(caption)
            copy.addWidget(value)
            row.addLayout(copy, 1)
            if key == "catalog_fingerprint":
                self.copy_fingerprint = QPushButton()
                self.copy_fingerprint.setIcon(icon("copy", theme.TEXT_MUTED, 18))
                self.copy_fingerprint.setObjectName("securityIconButton")
                self.copy_fingerprint.setToolTip("Copy full catalog fingerprint")
                self.copy_fingerprint.setAccessibleName("Copy catalog fingerprint")
                self.copy_fingerprint.clicked.connect(self._copy_fingerprint)
                row.addWidget(self.copy_fingerprint)
            self._metadata_cards.append(card)
            metadata_row.addWidget(card, 0, len(self._metadata_cards) - 1)
        layout.addLayout(metadata_row)
        summary_card = QWidget()
        summary_card.setObjectName("securityCard")
        summary_row = QGridLayout(summary_card)
        self._summary_layout = summary_row
        self._summary_cards: list[QWidget] = []
        summary_row.setContentsMargins(16, 12, 16, 12)
        summary_heading = QWidget()
        summary_heading.setStyleSheet("background: transparent;")
        summary_heading_row = QHBoxLayout(summary_heading)
        summary_heading_row.setContentsMargins(0, 0, 0, 0)
        summary_heading_row.addWidget(self._icon_tile("security-report", 34))
        summary_title = _label("Review summary")
        summary_title.setObjectName("securitySectionTitle")
        summary_heading_row.addWidget(summary_title, 1)
        self._summary_cards.append(summary_heading)
        summary_row.addWidget(summary_heading, 0, 0)
        self.metric_values: dict[str, QLabel] = {}
        self.metric_captions: dict[str, QLabel] = {}
        for key, title, color in (
            ("endpoints", "Endpoints reviewed", theme.PRIMARY),
            ("findings", "Findings", theme.FAIL),
            ("partial", "Partially assessed", theme.WARN),
            ("unassessed", "Not assessed", theme.TEXT),
        ):
            cell = QWidget()
            cell.setObjectName("securityMetric")
            column = QVBoxLayout(cell)
            column.setContentsMargins(22, 0, 10, 0)
            value = _label("0")
            value.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {color};")
            caption = _label(title)
            caption.setObjectName("securityMuted")
            column.addWidget(value)
            column.addWidget(caption)
            self.metric_values[key] = value
            self.metric_captions[key] = caption
            self._summary_cards.append(cell)
            summary_row.addWidget(cell, 0, len(self._summary_cards) - 1)
        self.status = _label("Ready")
        self.status.hide()
        state_cell = QWidget()
        state_cell.setStyleSheet("background: transparent;")
        state_column = QVBoxLayout(state_cell)
        state_column.setContentsMargins(0, 0, 0, 0)
        state_caption = _label("Review state")
        state_caption.setObjectName("securityMuted")
        self.review_state = _label("Ready")
        state_column.addWidget(state_caption)
        state_column.addWidget(self.review_state)
        self._summary_cards.append(state_cell)
        summary_row.addWidget(state_cell, 0, 5)
        layout.addWidget(self.status)
        layout.addWidget(summary_card)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.diagnostics_banner = QPushButton("Diagnostics   No review diagnostics yet")
        self.diagnostics_banner.setObjectName("securityDiagnostics")
        self.diagnostics_banner.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.diagnostics_banner.setIcon(icon("info-circle", theme.PRIMARY, 18))
        self.diagnostics_banner.clicked.connect(self._open_diagnostics)
        layout.addWidget(self.diagnostics_banner)

        filters = QGridLayout()
        self._filter_layout = filters
        self.text_filter = QLineEdit()
        self.text_filter.setPlaceholderText("Search findings...")
        self.text_filter.addAction(icon("search", theme.TEXT_MUTED, 18), QLineEdit.ActionPosition.LeadingPosition)
        self.severity_filter = QComboBox()
        self.severity_filter.addItems(["All severities", "critical", "high", "medium", "low", "info"])
        filters.addWidget(self.text_filter, 0, 0)
        filters.addWidget(self.severity_filter, 0, 1)
        self.category_filter = QComboBox()
        self.category_filter.addItem("All categories")
        self.service_filter = QComboBox()
        self.service_filter.addItem("All services")
        filters.addWidget(self.category_filter, 0, 2)
        filters.addWidget(self.service_filter, 0, 3)
        self.clear_filters_button = QPushButton("Clear filters")
        self.clear_filters_button.setIcon(icon("filter", theme.TEXT_MUTED, 18))
        self.clear_filters_button.clicked.connect(self._clear_filters)
        filters.addWidget(self.clear_filters_button, 0, 4)
        self._filter_widgets = [self.text_filter, self.severity_filter, self.category_filter,
                                self.service_filter, self.clear_filters_button]
        for combo in (self.severity_filter, self.category_filter, self.service_filter):
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
        layout.addLayout(filters)
        self.table = self._table(
            ["Severity", "Rule", "Category", "Service", "Operation", "Type", "Disposition"]
        )
        self.table.setMinimumHeight(180)
        self.empty_findings = attach_table_empty_state(
            self.table, icon_name="security-search", title="No findings match your filters",
            guidance="Try adjusting the search or filter criteria.", keep_headers=True,
        )
        self.empty_findings.icon_label.setPixmap(icon("security-search", theme.PRIMARY, 54).pixmap(54))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 3)

        self.tabs = QTabWidget()
        detail_page = QWidget()
        detail_layout = QVBoxLayout(detail_page)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.detail_stack = QStackedWidget()
        self.detail_stack.addWidget(self.details)
        self.detail_hint = QWidget()
        self.detail_hint.setObjectName("securityDetailHint")
        hint_row = QHBoxLayout(self.detail_hint)
        hint_row.setContentsMargins(16, 12, 16, 12)
        hint_row.addWidget(self._icon_tile("info-circle", 24))
        hint_label = _label("Select a finding to inspect its evidence and limitations.")
        hint_label.setObjectName("securityMuted")
        hint_row.addWidget(hint_label, 1)
        self.detail_stack.addWidget(self.detail_hint)
        detail_layout.addWidget(self.detail_stack)
        disposition_row = QGridLayout()
        self._disposition_layout = disposition_row
        self.disposition = QComboBox()
        self.disposition.addItems(list(_DISPOSITIONS))
        self.reason = QLineEdit()
        self.reason.setPlaceholderText("Reason (required for non-open dispositions)")
        self.apply_disposition = QPushButton("Apply disposition")
        self.disposition.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.disposition.setMinimumContentsLength(10)
        expiry_row = QGridLayout()
        self._expiry_layout = expiry_row
        self.expiry_enabled = QCheckBox("Disposition expires")
        self.expiry_date = QDateEdit(QDate.currentDate().addDays(30))
        self.expiry_date.setCalendarPopup(True)
        self.expiry_date.setDisplayFormat("yyyy-MM-dd")
        self.expiry_date.setEnabled(False)
        self.expiry_enabled.toggled.connect(self.expiry_date.setEnabled)
        self.reopen_button = QPushButton("Revoke / reopen")
        self.reopen_button.clicked.connect(self._reopen)
        self.expiry_date.setMaximumWidth(230)
        self._expiry_widgets = [self.expiry_enabled, self.expiry_date, self.reopen_button]
        self.tabs.addTab(detail_page, "Finding details")
        self.coverage_table = self._table(["Category", "Status", "Explanation"])
        self.tabs.addTab(self.coverage_table, "Coverage")
        self.diagnostics = QPlainTextEdit()
        self.diagnostics.setReadOnly(True)
        self.tabs.addTab(self.diagnostics, "Diagnostics")
        self.comparison = QPlainTextEdit()
        self.comparison.setReadOnly(True)
        self.tabs.addTab(self.comparison, "Comparison")
        self.tabs.setMinimumHeight(140)
        layout.addWidget(self.tabs, 2)
        self._disposition_widgets = [_label("Disposition"), self.disposition, self.reason, self.apply_disposition]
        layout.addLayout(disposition_row)
        layout.addLayout(expiry_row)

        footer = QWidget()
        footer.setObjectName("securityFooter")
        footer.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        buttons = QGridLayout(footer)
        buttons.setContentsMargins(20, 8, 20, 12)
        self._footer_layout = buttons
        self.history_button = QPushButton("History")
        self.history_button.setEnabled(store is not None)
        self.compare_button = QPushButton("Compare report")
        self.compare_button.setEnabled(False)
        self.compare_button.clicked.connect(self._compare)
        self.export_buttons: dict[str, QPushButton] = {}
        for kind in ("JSON", "HTML", "CSV", "PDF"):
            button = QPushButton(f"Export {kind}")
            button.clicked.connect(lambda checked=False, k=kind: self.export_report(k))
            button.setEnabled(False)
            self.export_buttons[kind] = button
        self.cancel_button = QPushButton("Cancel review")
        self.cancel_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("securityClose")
        self.cancel_button.setObjectName("securityCancel")
        for button, glyph in (
            (self.history_button, "history"), (self.compare_button, "compare"),
            (self.apply_disposition, "copy"), (self.reopen_button, "renew"),
        ):
            button.setIcon(icon(glyph, theme.TEXT_MUTED, 16))
        for kind, button in self.export_buttons.items():
            button.setIcon(icon("export" if kind == "JSON" else "security-report", theme.TEXT_MUTED, 16))
        self._footer_widgets = [self.history_button, self.compare_button, *self.export_buttons.values(),
                                self.cancel_button, self.close_button]
        root_layout.addWidget(footer)
        self.text_filter.textChanged.connect(self._filter_findings)
        self.severity_filter.currentTextChanged.connect(self._filter_findings)
        self.category_filter.currentTextChanged.connect(self._filter_findings)
        self.service_filter.currentTextChanged.connect(self._filter_findings)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.apply_disposition.clicked.connect(self._update_disposition)
        self.history_button.clicked.connect(self._history)
        self.cancel_button.clicked.connect(self.cancel_scan)
        self.close_button.clicked.connect(self.close)
        self._apply_design()
        self._selection_changed()
        self._responsive_columns = 0
        self._reflow()

    @staticmethod
    def _grid_widgets(layout: QGridLayout, widgets: list[QWidget], columns: int) -> None:
        while layout.count():
            layout.takeAt(0)
        for column in range(8):
            layout.setColumnStretch(column, 0)
        for index, widget in enumerate(widgets):
            layout.addWidget(widget, index // columns, index % columns)
        for column in range(columns):
            layout.setColumnStretch(column, 1)

    def _reflow(self) -> None:
        width = self.width()
        columns = 5 if width >= 1150 else 3 if width >= 850 else 2
        if columns == self._responsive_columns:
            return
        self._responsive_columns = columns
        wide = columns == 5
        self._hero_layout.setDirection(QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom)
        self._freshness_card.setMaximumWidth(345 if wide else 16777215)
        self._grid_widgets(self._metadata_layout, self._metadata_cards, columns)
        self._grid_widgets(self._summary_layout, self._summary_cards, 6 if wide else 3 if columns == 3 else 2)
        self._grid_widgets(self._filter_layout, self._filter_widgets, 5 if wide else 3 if columns == 3 else 2)
        self._filter_layout.setColumnStretch(0, 3 if wide else 1)
        self._grid_widgets(self._disposition_layout, self._disposition_widgets, 4 if wide else 2)
        self._disposition_layout.setColumnStretch(2 if wide else 0, 3 if wide else 1)
        self._grid_widgets(self._expiry_layout, self._expiry_widgets, 3 if columns >= 3 else 2)
        self._grid_widgets(self._footer_layout, self._footer_widgets, 8 if wide else 4)
        self.content.layout().invalidate()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "_responsive_columns"):
            self._reflow()

    @staticmethod
    def _icon_tile(name: str, size: int) -> QLabel:
        label = QLabel()
        label.setObjectName("securityIconTile")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setFixedSize(size, size)
        label.setPixmap(icon(name, theme.PRIMARY, max(18, size - 22)).pixmap(max(18, size - 22)))
        return label

    def _apply_design(self) -> None:
        self.setStyleSheet(f"""
            QDialog#securityReview {{ background: {theme.BACKGROUND}; color: {theme.TEXT}; }}
            QDialog#securityReview QLabel {{ background: transparent; color: {theme.TEXT}; }}
            QDialog#securityReview QLabel#securityMuted {{ color: {theme.TEXT_MUTED}; }}
            QDialog#securityReview QLabel#securityHeading {{ font-size: 23px; font-weight: 700; }}
            QDialog#securityReview QLabel#securitySectionTitle {{ font-weight: 700; }}
            QDialog#securityReview QLabel#securityAccentTitle {{ color: {theme.PRIMARY}; font-weight: 600; }}
            QDialog#securityReview QLabel#securityStateBadge {{ background: {theme.SUCCESS_SOFT}; color: {theme.PASS};
                border-radius: 13px; padding: 4px 14px; font-weight: 600; }}
            QWidget#securityHero {{ border-bottom: 1px solid {theme.BORDER}; }}
            QWidget#securityCard {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER};
                border-radius: 7px; }}
            QWidget#securityMetric {{ background: transparent; border-left: 1px solid {theme.BORDER}; }}
            QDialog#securityReview QLabel#securityIconTile {{ background: {theme.ACCENT_SOFT}; border-radius: 9px; }}
            QWidget#securityFreshness {{ background: {theme.HOVER_SOFT}; border: 1px solid {theme.BORDER_STRONG};
                border-radius: 6px; }}
            QWidget#securityDetailHint {{ background: {theme.HOVER_SOFT}; border: 1px solid {theme.BORDER};
                border-radius: 5px; }}
            QDialog#securityReview QLineEdit, QDialog#securityReview QComboBox,
            QDialog#securityReview QDateEdit, QDialog#securityReview QPushButton {{
                min-height: 30px; border: 1px solid {theme.BORDER}; border-radius: 5px;
                background: {theme.SURFACE}; color: {theme.TEXT}; padding: 3px 10px; }}
            QDialog#securityReview QPushButton:hover {{ background: {theme.HOVER_SOFT}; border-color: {theme.PRIMARY}; }}
            QDialog#securityReview QPushButton:disabled {{ color: {theme.SKIP}; background: {theme.SURFACE_ALT}; }}
            QDialog#securityReview QPushButton#securityDiagnostics {{ text-align: left; color: {theme.TEXT_MUTED}; }}
            QDialog#securityReview QPushButton#securityIconButton {{ border: none; padding: 0; }}
            QDialog#securityReview QPushButton#securityClose {{ background: {theme.PRIMARY}; color: white; font-weight: 600; padding: 4px 22px; }}
            QDialog#securityReview QPushButton#securityClose:hover {{ background: {theme.PRIMARY_HOVER}; }}
            QDialog#securityReview QPushButton#securityCancel:enabled {{ color: {theme.FAIL}; border-color: {theme.FAIL}; }}
            QDialog#securityReview QLabel[emptyStateGuidance="true"] {{ color: {theme.TEXT_MUTED}; }}
            QDialog#securityReview QLabel[emptyStateTitle="true"] {{ font-weight: 600; }}
            QDialog#securityReview QTableWidget {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER};
                border-radius: 5px; gridline-color: {theme.BORDER}; selection-background-color: {theme.SELECT_SOFT};
                selection-color: {theme.TEXT}; }}
            QDialog#securityReview QHeaderView::section {{ background: {theme.SURFACE_ALT};
                color: {theme.TEXT_MUTED}; border: none; border-right: 1px solid {theme.BORDER};
                border-bottom: 1px solid {theme.BORDER}; padding: 10px 14px; font-weight: 600; }}
            QDialog#securityReview QTabWidget::pane {{ background: {theme.SURFACE}; border: 1px solid {theme.BORDER};
                border-radius: 6px; }}
            QDialog#securityReview QTabBar::tab {{ background: transparent; color: {theme.TEXT_MUTED};
                border: none; padding: 10px 18px; }}
            QDialog#securityReview QTabBar::tab:selected {{ color: {theme.PRIMARY};
                border-bottom: 2px solid {theme.PRIMARY}; font-weight: 600; }}
            QDialog#securityReview QPlainTextEdit {{ background: {theme.HOVER_SOFT};
                color: {theme.TEXT}; border: 1px solid {theme.BORDER}; border-radius: 5px; padding: 10px; }}
        """)

    def _copy_fingerprint(self) -> None:
        if self.report is not None:
            from PyQt6.QtWidgets import QApplication
            QApplication.clipboard().setText(_plain(self.report.catalog_fingerprint))

    def _open_diagnostics(self) -> None:
        self.tabs.setCurrentWidget(self.diagnostics)

    def _clear_filters(self) -> None:
        for widget in (self.text_filter, self.severity_filter, self.category_filter, self.service_filter):
            widget.blockSignals(True)
        self.text_filter.clear()
        for widget in (self.severity_filter, self.category_filter, self.service_filter):
            widget.setCurrentIndex(0)
        for widget in (self.text_filter, self.severity_filter, self.category_filter, self.service_filter):
            widget.blockSignals(False)
        self._filter_findings()

    def _set_state(self, text: str, state: str) -> None:
        color = theme.PASS if state == "complete" else theme.FAIL if state == "failed" else theme.WARN
        background = theme.SUCCESS_SOFT if state == "complete" else theme.SURFACE_ALT
        self.state_badge.setText(text)
        self.state_badge.setStyleSheet(f"background: {background}; color: {color};")
        self.review_state.setText(text)
        self.review_state.setStyleSheet(f"color: {color}; font-weight: 600;")

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        return table

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._started:
            self._started = True
            QTimer.singleShot(0, self._start_scan)

    def _start_scan(self) -> None:
        if self._cancel.is_set():
            self.status.setText("Review cancelled before starting.")
            return
        self._running = True
        self._set_state("Reviewing", "running")
        self.status.setText("Review in progress…")
        self.progress.show()
        self.cancel_button.setEnabled(True)
        self.history_button.setEnabled(False)
        self._thread = _ScanThread(self._scan, self._cancel)
        worker = self._thread
        cancel = self._cancel
        self.destroyed.connect(lambda: cancel.set())
        _PANEL_WORKERS.add(worker)
        self._thread.report_ready.connect(self._receive_report)
        self._thread.failed.connect(self._scan_failed)
        self._thread.finished.connect(self._scan_finished)
        worker.finished.connect(lambda: _PANEL_WORKERS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        self._thread.start()

    def cancel_scan(self) -> None:
        self._cancel.set()
        self._set_state("Cancelling", "partial")
        self.cancel_button.setEnabled(False)
        self.status.setText("Cancellation requested; waiting for the review to stop…")

    def _receive_report(self, report: SecurityReport) -> None:
        self._pending_report = report

    def _scan_failed(self, message: str) -> None:
        self._failure = message

    def _scan_finished(self) -> None:
        self._running = False
        self.progress.hide()
        self.cancel_button.setEnabled(False)
        self.history_button.setEnabled(self._store is not None)
        if self._cancel.is_set():
            self._set_state("Cancelled", "partial")
            self.status.setText("Review cancelled. No completed report was saved.")
            self.summary.setText("Review cancelled; coverage is incomplete.")
        elif self._failure:
            self._set_state("Failed", "failed")
            self.summary.setText("Review failed; coverage is incomplete.")
            self._error("Security review failed", self._failure)
        elif self._pending_report is not None:
            try:
                self._display_report(_inherit_report(self._store, self._pending_report))
                self._persist()
            except Exception as exc:
                self._error("Report persistence failed", str(exc))
        if self._close_pending:
            self.close()

    def closeEvent(self, event) -> None:
        if self._running:
            self._close_pending = True
            self.cancel_scan()
            event.ignore()
        else:
            self._cancel.set()
            super().closeEvent(event)

    def reject(self) -> None:
        # Escape must use the same cooperative-close path as the window button.
        if self._running:
            self.close()
        else:
            self._cancel.set()
            super().reject()

    def accept(self) -> None:
        if self._running:
            self.close()
        else:
            self._cancel.set()
            super().accept()

    def done(self, result: int) -> None:
        if self._running:
            self._close_pending = True
            self.cancel_scan()
        else:
            self._cancel.set()
            super().done(result)

    def _error(self, title: str, message: str) -> None:
        self.status.setText(_plain(f"{title}: {message}"))
        self.status.show()
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText(_plain(message))
        box.exec()

    def _display_report(self, report: SecurityReport, *, historical: bool = False) -> None:
        report.scope = safe_text(report.scope)
        for finding in report.findings:
            finding.reason = safe_text(finding.reason)
        self.report = report
        self._historical = historical
        self._comparison_text = ""
        self.comparison.clear()
        comparison = getattr(report, "comparison", {})
        if isinstance(comparison, dict) and comparison:
            lines = [f"Baseline: {comparison.get('baseline_id', '')}",
                     f"Incomparable: {bool(comparison.get('incomparable'))}",
                     f"Reason: {comparison.get('reason', '')}"]
            for category in ("new", "resolved", "unchanged", "changed"):
                findings = comparison.get(category, [])
                lines.append(f"\n{category.title()}: {len(findings)}")
                lines.extend(
                    f"- {f.get('rule_id', '')}: {f.get('title', '')} "
                    f"({f.get('method', '')} {f.get('path', '')})"
                    for f in findings if isinstance(f, dict)
                )
            self._comparison_text = _plain("\n".join(lines))
            self.comparison.setPlainText(self._comparison_text)
        self.compare_button.setEnabled(self._store is not None)
        if report.mode in {"controlled verification", "active verification"}:
            self.disclaimer.setText(
                "Controlled verification used explicitly approved requests and bounded checks. "
                "This is not a penetration test, compliance certification, or assurance that the API is secure."
            )
        else:
            self.disclaimer.setText(
                "Passive, evidence-based review only; no extra requests are sent. "
                "This is not a compliance certification, penetration test, or assurance that the API is secure. "
                "Unknown coverage requires independent verification."
            )
        self._update_freshness()
        self.heading.setText("API Security Review — historical snapshot" if historical else "API Security Review")
        self.metadata.setText(_plain(
            f"Mode: {report.mode} | Scope: {report.scope} | Created: {report.created_at}\n"
            f"Catalog fingerprint: {report.catalog_fingerprint} | Source fingerprint: "
            f"{report.source_fingerprint or 'not available'} | Rules: {report.rule_version}\n"
            f"Environment: {report.environment or 'not specified'} | Report: {report.id}"
        ))
        coverage_statuses = [c.status.strip().casefold() for c in report.coverage]
        partial = coverage_statuses.count("partial")
        unassessed = sum(
            c.status.strip().casefold() in {"not assessed", "unknown"}
            for c in report.coverage
        )
        evidence_note = ""
        if not report.endpoints_reviewed:
            evidence_note = "\nNo endpoints reviewed; no endpoint evidence, not a clean result."
        elif report.coverage and unassessed == len(report.coverage):
            evidence_note = "\nNo assessed coverage evidence; not a clean result."
        if report.diagnostics:
            evidence_note += "\nDiagnostics: " + _plain("; ".join(report.diagnostics))
        coverage_summary = f"{partial} partially assessed; {unassessed} not assessed"
        if report.mode in {"controlled verification", "active verification"}:
            coverage_summary = "; ".join(
                f"{coverage_statuses.count(status)} {status}"
                for status in ("verified", "assertion failed", "unknown", "not tested")
            )
        self.summary.setText(
            f"State: {_plain(report.state)} | {report.endpoints_reviewed} endpoints reviewed "
            f"| {len(report.findings)} findings "
            f"| {coverage_summary}"
            f"{evidence_note}\n"
            f"{'Historical snapshot' if historical else 'Frozen snapshot'}; "
            "catalog or source changes require a rescan."
        )
        self.status.setText(_plain(
            f"Historical snapshot — {report.state}; not a current scan."
            if historical else f"Review state: {report.state}"
        ))
        self._set_state("Historical" if historical else _plain(report.state.title()),
                        "partial" if historical else report.state)
        for key, label in self.metadata_values.items():
            value = str(getattr(report, key) or "Not specified")
            label.setText(_plain(value[:23] + "..." if key == "catalog_fingerprint" and len(value) > 26 else value))
            label.setToolTip(_plain(value))
        self.metadata_values["mode"].setText(_plain(report.mode.title()))
        active = report.mode in {"controlled verification", "active verification"}
        for key, value in (
            ("endpoints", report.endpoints_reviewed), ("findings", len(report.findings)),
            ("partial", coverage_statuses.count("verified") if active else partial),
            ("unassessed", sum(s in {"unknown", "not tested"} for s in coverage_statuses) if active else unassessed),
        ):
            self.metric_values[key].setText(str(value))
        self.metric_captions["partial"].setText("Verified assertions" if active else "Partially assessed")
        self.metric_captions["unassessed"].setText("Unknown / not tested" if active else "Not assessed")
        for widget, field in ((self.category_filter, "category"), (self.service_filter, "service")):
            selected = widget.currentText()
            widget.blockSignals(True)
            widget.clear()
            widget.addItem("All categories" if field == "category" else "All services")
            widget.addItems(sorted({str(getattr(finding, field)) for finding in report.findings}))
            index = widget.findText(selected)
            widget.setCurrentIndex(max(0, index))
            widget.blockSignals(False)
        diagnostic_text = _plain("; ".join(report.diagnostics)) or "No diagnostics reported."
        self.diagnostics_banner.setText("Diagnostics   " + diagnostic_text[:160] + ("..." if len(diagnostic_text) > 160 else ""))
        self.diagnostics_banner.setToolTip(diagnostic_text)
        self.diagnostics_banner.setAccessibleName("Open full review diagnostics")
        self.metadata_values["catalog_fingerprint"].setToolTip(self.metadata.text())
        self.status.setToolTip(self.summary.text())
        self.review_state.setToolTip(self.summary.text())
        self.coverage_table.setRowCount(len(report.coverage))
        for row, coverage in enumerate(report.coverage):
            for column, value in enumerate((coverage.category, coverage.status, coverage.explanation)):
                self.coverage_table.setItem(row, column, QTableWidgetItem(_plain(value)))
        self.diagnostics.setPlainText(_plain("\n".join(report.diagnostics)))
        for button in self.export_buttons.values():
            button.setEnabled(True)
        self._filter_findings()

    def _filter_findings(self, *_args) -> None:
        self._visible_findings = []
        if self.report is None:
            return
        text = safe_text(self.text_filter.text()).casefold().strip()
        severity = self.severity_filter.currentText()
        for index, finding in enumerate(self.report.findings):
            searchable = " ".join(str(getattr(finding, field)) for field in (
                "rule_id", "title", "severity", "category", "evidence_class", "service",
                "endpoint_id", "method", "path", "location", "evidence", "explanation",
                "remediation", "limitation", "disposition", "reason", "confidence",
            )).casefold()
            if (not text or text in searchable) and (
                severity == "All severities" or finding.severity.casefold() == severity
            ) and (
                self.category_filter.currentIndex() == 0 or finding.category == self.category_filter.currentText()
            ) and (
                self.service_filter.currentIndex() == 0 or finding.service == self.service_filter.currentText()
            ):
                self._visible_findings.append(index)
        self.table.setRowCount(0)
        self.table.setRowCount(len(self._visible_findings))
        for row, index in enumerate(self._visible_findings):
            finding = self.report.findings[index]
            values = (
                finding.severity, finding.rule_id, finding.category, finding.service,
                f"{finding.method} {finding.path}", finding.evidence_class, finding.disposition,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(_plain(value))
                item.setToolTip(_plain(value))
                if column == 0:
                    color = {"critical": theme.FAIL, "high": theme.FAIL, "medium": theme.WARN,
                             "low": theme.PRIMARY, "info": theme.TEXT_MUTED}.get(finding.severity.casefold(), theme.TEXT)
                    item.setForeground(QColor(color))
                self.table.setItem(row, column, item)
        self._selection_changed()
        if self.table.rowCount():
            self.table.selectRow(0)

    def _selected_finding(self):
        row = self.table.currentRow()
        if self.report is None or row < 0 or row >= len(self._visible_findings):
            return None
        return self.report.findings[self._visible_findings[row]]

    def _selection_changed(self) -> None:
        finding = self._selected_finding()
        for widget in (self.disposition, self.reason, self.apply_disposition,
                       self.expiry_enabled, self.reopen_button):
            widget.setEnabled(finding is not None)
        if finding is None:
            self.detail_stack.setCurrentWidget(self.detail_hint)
            self.details.setPlainText("Select a finding to inspect its evidence and limitations.")
            self.reason.clear()
            self.expiry_enabled.setChecked(False)
            self.expiry_date.setEnabled(False)
            return
        self.detail_stack.setCurrentWidget(self.details)
        self.details.setPlainText(_plain(
            f"{finding.title}\nRule: {finding.rule_id}\nEndpoint: {finding.endpoint_id}\n\n"
            f"Confidence\n{finding.confidence}\n\n"
            f"Explanation\n{finding.explanation}\n\nEvidence\n{finding.evidence}\n\n"
            f"Location\n{finding.location}\n\nRemediation\n{finding.remediation}\n\n"
            f"Limitations\n{finding.limitation}\n\nDisposition: {finding.disposition}\n"
            f"Reason: {finding.reason}\nExpires: {getattr(finding, 'expires_at', '') or 'never'}"
        ))
        self.disposition.setCurrentText(finding.disposition)
        self.reason.setText(_plain(finding.reason))
        expiry = QDate.fromString(getattr(finding, "expires_at", ""), "yyyy-MM-dd")
        self.expiry_enabled.setChecked(expiry.isValid())
        if expiry.isValid():
            self.expiry_date.setDate(expiry)

    def _update_disposition(self) -> None:
        finding = self._selected_finding()
        if finding is None:
            return
        disposition = self.disposition.currentText()
        reason = safe_text(self.reason.text().strip())
        if disposition != "open" and not reason:
            self._error("Reason required", "Provide a nonempty reason for a non-open disposition.")
            return
        expires_at = ""
        if disposition != "open" and self.expiry_enabled.isChecked():
            if self.expiry_date.date() <= QDate.currentDate():
                self._error("Invalid expiration", "Disposition expiration must be a future date.")
                return
            expires_at = self.expiry_date.date().toString("yyyy-MM-dd")
        finding.disposition = disposition
        finding.reason = reason
        finding.expires_at = expires_at
        if self._persist():
            self.status.setText("Disposition updated; original finding evidence retained.")
            self.status.show()
        self._filter_findings()

    def _persist(self) -> bool:
        if self._store is not None and self.report is not None:
            try:
                self._store.save_security_report(self.report.to_dict())
            except Exception as exc:
                self._error("Report persistence failed", str(exc))
                return False
        return True

    def export_report(self, kind: str) -> None:
        if self.report is None:
            return
        try:
            kind = kind.upper()
            suffix = {"JSON": "json", "HTML": "html", "CSV": "csv", "PDF": "pdf"}[kind]
            filename, _ = QFileDialog.getSaveFileName(
                self, f"Export security report as {kind}", f"security-report.{suffix}",
                f"{kind} files (*.{suffix})",
            )
            if not filename:
                return
            if kind == "PDF":
                printer = QPrinter(QPrinter.PrinterMode.HighResolution)
                printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
                printer.setOutputFileName(filename)
                document = QTextDocument()
                document.setHtml(self._export_html())
                document.print(printer)
                if not Path(filename).is_file() or not Path(filename).stat().st_size:
                    raise OSError("The PDF could not be written.")
                return
            if kind == "JSON":
                payload = json.dumps(self.report.to_dict(), ensure_ascii=False, indent=2)
            elif kind == "HTML":
                payload = self._export_html()
            else:
                payload = report_csv(self.report)
            Path(filename).write_text(payload, encoding="utf-8")
        except Exception as exc:
            self._error("Report export failed", str(exc))

    def _export_html(self) -> str:
        content = report_html(self.report)
        if self._comparison_text:
            extra = "<h2>Report comparison</h2><pre>" + html.escape(self._comparison_text) + "</pre>"
            content = content.replace("</body>", extra + "</body>") if "</body>" in content else content + extra
        return content

    def _reopen(self) -> None:
        self.disposition.setCurrentText("open")
        self.reason.clear()
        self.expiry_enabled.setChecked(False)
        self._update_disposition()

    def _update_freshness(self) -> None:
        if self.report is None:
            self.freshness.clear()
        elif self._historical:
            self.freshness.setText("Historical snapshot — not a current review.")
        elif self._current_fingerprint is not None:
            try:
                current = self._current_fingerprint()
                self.freshness.setText(
                    "STALE — evidence changed; rescan required."
                    if current != self.report.catalog_fingerprint else "Current evidence fingerprint matches."
                )
            except Exception:
                self.freshness.setText("Freshness unavailable — current evidence could not be checked.")
        else:
            self.freshness.setText("Frozen snapshot — current evidence freshness is not monitored.")
        text = self.freshness.text()
        self.freshness_title.setText(
            "Current evidence matches" if text == "Current evidence fingerprint matches."
            else "Evidence changed" if text.startswith("STALE")
            else "Historical evidence" if self._historical
            else "Evidence freshness"
        )

    def _compare(self) -> None:
        if self._store is None or self.report is None or self._running:
            return
        try:
            reports = [
                r for r in self._store.security_reports(limit=100)
                if r.get("id") != self.report.id and
                all(r.get(k, "") == getattr(self.report, k) for k in ("mode", "scope", "environment"))
            ]
            if not reports:
                self.status.setText("No matching mode, scope, and environment baseline is available.")
                self.status.show()
                return
            choices = [_plain(f"{r.get('created_at', '')} | {r.get('state', '')} | {r['id']}") for r in reports]
            choice, accepted = QInputDialog.getItem(self, "Compare security reports", "Baseline snapshot", choices, 0, False)
            if not accepted:
                return
            payload = self._store.security_report(reports[choices.index(choice)]["id"])
            if payload is None:
                raise ValueError("The selected baseline is no longer available.")
            from .security import compare_reports
            baseline = SecurityReport.from_dict(payload)
            result = compare_reports(self.report, baseline)
            if baseline.state != "complete" or self.report.state != "complete":
                result["resolved"] = []
                result["incomparable"] = True
                result["reason"] = "Incomplete, failed, or cancelled evidence cannot establish resolution."
            lines = [f"Baseline: {baseline.id}", f"Incomparable: {bool(result.get('incomparable'))}",
                     f"Reason: {result.get('reason', '')}"]
            serialized = {}
            for category in ("new", "resolved", "unchanged", "changed"):
                findings = result.get(category, [])
                lines.append(f"\n{category.title()}: {len(findings)}")
                lines.extend(f"- {f.rule_id}: {f.title} ({f.method} {f.path})" for f in findings)
                serialized[category] = [vars(f).copy() for f in findings]
            serialized.update(baseline_id=baseline.id, incomparable=bool(result.get("incomparable")),
                              reason=safe_text(result.get("reason", "")))
            self.report.comparison = serialized
            self._comparison_text = _plain("\n".join(lines))
            self.comparison.setPlainText(self._comparison_text)
            self.tabs.setCurrentWidget(self.comparison)
            self._persist()
        except Exception as exc:
            self._error("Report comparison failed", str(exc))

    def _history(self) -> None:
        if self._store is None or self._running:
            return
        try:
            reports = [r for r in self._store.security_reports(limit=50)
                       if r.get("state", "complete") == "complete"]
            if not reports:
                self.status.setText("No completed historical reports are available.")
                self.status.show()
                return
            choices = [
                _plain(f"{r.get('created_at', '')} | {r.get('mode', '')} | {r['id']}")
                for r in reports
            ]
            choice, accepted = QInputDialog.getItem(
                self, "Security report history", "Completed historical snapshot", choices, 0, False
            )
            if not accepted:
                return
            payload = self._store.security_report(reports[choices.index(choice)]["id"])
            if payload is None:
                raise ValueError("The selected historical report is no longer available.")
            report = SecurityReport.from_dict(payload)
            if report.state != "complete":
                raise ValueError("The selected report is not complete.")
            self._display_report(report, historical=True)
        except Exception as exc:
            self._error("Report history failed", str(exc))


# Unparented workers outlive widgets until cooperative scanners finish.
_PANEL_WORKERS: set[_ScanThread] = set()


class SecurityReportPanel(QWidget):
    """Reusable report summary; automatic reviews never open modal windows.

    Call configure with a scanner capturing an immutable evidence snapshot.
    shutdown requests cooperative cancellation and optionally waits a bounded time.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scan = None
        self._store = None
        self._current_fingerprint = None
        self._thread = None
        self._cancel = None
        self._generation = 0
        self._worker_generation = -1
        self._pending_report = None
        self._failure = ""
        self._auto_pending = False
        self._dialog = None
        self.report = None
        layout = QVBoxLayout(self)
        self.summary = _label("No security evidence configured.")
        layout.addWidget(self.summary)
        self.review_button = QPushButton("Review security")
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(self._review)
        layout.addWidget(self.review_button)
        layout.addStretch()

    def configure(self, scan: Callable[[threading.Event], SecurityReport], *,
                  store=None, current_fingerprint=None, auto=False) -> None:
        self.clear()
        self._scan = scan
        self._store = store
        self._current_fingerprint = current_fingerprint
        self._auto_pending = bool(auto)
        self.review_button.setEnabled(True)
        self.summary.setText("Evidence ready for a passive security review.")
        if self._auto_pending:
            QTimer.singleShot(0, self._start_auto)

    def set_review(self, scan, store=None, current_fingerprint=None, auto=False) -> None:
        self.configure(scan, store=store, current_fingerprint=current_fingerprint, auto=auto)

    def clear(self) -> None:
        self._generation += 1
        if self._cancel is not None:
            self._cancel.set()
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None
        self._auto_pending = False
        self._scan = None
        self._store = None
        self._current_fingerprint = None
        self.report = None
        self.review_button.setEnabled(False)
        self.summary.setText("No security evidence configured.")

    def _start_auto(self) -> None:
        if not self._auto_pending or self._scan is None or self._thread is not None:
            return
        self._auto_pending = False
        self._worker_generation = self._generation
        self._pending_report = None
        self._failure = ""
        self._cancel = threading.Event()
        cancel = self._cancel
        self.destroyed.connect(lambda: cancel.set())
        worker = _ScanThread(self._scan, cancel)
        self._thread = worker
        _PANEL_WORKERS.add(worker)
        worker.report_ready.connect(self._auto_report)
        worker.failed.connect(self._auto_failed)
        worker.finished.connect(self._auto_finished)
        worker.finished.connect(lambda: _PANEL_WORKERS.discard(worker))
        worker.finished.connect(worker.deleteLater)
        self.summary.setText("Security review in progress…")
        self.review_button.setEnabled(False)
        worker.start()

    def _auto_report(self, report) -> None:
        self._pending_report = report

    def _auto_failed(self, message) -> None:
        self._failure = message

    def _auto_finished(self) -> None:
        self._thread = None
        if self._worker_generation == self._generation and not self._cancel.is_set():
            if self._failure:
                self.summary.setText(_plain(f"Security review failed: {self._failure}"))
            elif self._pending_report is not None:
                try:
                    self.report = _inherit_report(self._store, self._pending_report)
                    if self._store is not None:
                        self._store.save_security_report(self.report.to_dict())
                    self._show_summary()
                except Exception as exc:
                    self.summary.setText(_plain(f"Security report persistence failed: {exc}"))
        self.review_button.setEnabled(self._scan is not None)
        if self._auto_pending:
            QTimer.singleShot(0, self._start_auto)

    def _show_summary(self) -> None:
        if self.report is not None:
            self.summary.setText(_plain(
                f"Previous review: {self.report.state} | {len(self.report.findings)} findings "
                f"| {self.report.endpoints_reviewed} endpoints | {self.report.created_at}"
            ))

    def _review(self) -> None:
        if self._scan is None:
            return
        if self._dialog is not None and self._dialog.isVisible():
            self._dialog.raise_()
            return
        dialog = SecurityReviewDialog(
            self._scan, self, store=self._store, current_fingerprint=self._current_fingerprint,
        )
        self._dialog = dialog
        generation = self._generation
        if self.report is not None:
            dialog._started = True
            dialog._display_report(self.report)
        dialog.finished.connect(lambda: self._dialog_finished(dialog, generation))
        dialog.show()

    def review(self) -> None:
        """Open the shared viewer, reusing an already completed automatic review."""
        self._review()

    def _dialog_finished(self, dialog, generation) -> None:
        if generation == self._generation and dialog.report is not None:
            self.report = dialog.report
            self._show_summary()

    def shutdown(self, timeout_ms: int = 0) -> bool:
        self._auto_pending = False
        if self._cancel is not None:
            self._cancel.set()
        if self._dialog is not None:
            self._dialog.close()
        stopped = self._thread is None or self._thread.wait(max(0, timeout_ms))
        if self._dialog is not None and self._dialog._running:
            stopped = False
        return stopped

    def closeEvent(self, event) -> None:
        self.shutdown()
        if self._dialog is not None and self._dialog._running:
            event.ignore()
        else:
            super().closeEvent(event)
