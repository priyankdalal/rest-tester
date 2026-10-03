"""The Ask AI dialog: type what to test, review the plan, open it in API Explorer.

The dialog never sends a request. It shows exactly what the assistant planned
(endpoint, parameters, readable filters, body, expected status, assumptions,
warnings) and the user decides whether to open it in API Explorer, where the
normal Send button applies.
"""

from __future__ import annotations

import csv
import html
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut, QStandardItemModel
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QComboBox,
    QFrame,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..catalog import Catalog
from ..data_runner.csv_source import CsvImportSettings, CsvSource
from ..icons import icon, light_bulb_icon
from ..suite import ASSERTION_KINDS, TestSuite
from .catalog_index import CatalogIndex
from .config import AiSettings, PROVIDER_LABELS, load_ai_settings
from .controller import AiTask, plan_request_task, plan_suite_task, plan_workflow_task
from .planner import PlanOutcome, SuiteOutcome
from .request_plan import ExplorerRequest, to_explorer_request
from .suite_plan import to_test_suite
from .usage_ui import UsageStrip
from .redaction import redact_text
from ..widgets import ElidingLabel

if TYPE_CHECKING:
    from .workflow_plan import WorkflowPlan

EXAMPLES: tuple[str, ...] = (
    "List active brands sorted by name",
    "Get brand 42",
    "Top 5 brands created after 2024-01-01, newest first",
    "How many active users are there?",
)

SUITE_EXAMPLES: tuple[str, ...] = (
    "CRUD regression suite for brands",
    "Create a crop, read it back, rename it, then delete it",
    "Check the active brands filter only returns active rows",
    "Negative cases for get brand by id",
)

MODES = ("request", "suite", "data", "load")
MODE_LABELS = {"request": "request", "suite": "suite", "data": "data run", "load": "load test"}

OPERATOR_WORDS: dict[str, str] = {
    "eq": "equals", "neq": "is not", "gt": "is greater than", "lt": "is less than",
    "gte": "is at least", "lte": "is at most", "ct": "contains", "nct": "does not contain",
    "sw": "starts with", "ew": "ends with", "in": "is one of", "nin": "is none of", "bt": "is between",
}

#: Tasks stay referenced here until their thread finishes, even if the dialog
#: that started them was closed, so Qt never destroys a running QThread.
_LIVE_TASKS: set[AiTask] = set()


def _keep_alive(task: AiTask) -> AiTask:
    _LIVE_TASKS.add(task)
    task.done.connect(lambda: _LIVE_TASKS.discard(task))
    return task


def shutdown_ai_tasks(milliseconds: int = 2000) -> None:
    for task in list(_LIVE_TASKS):
        task.cancel()
        task.wait(milliseconds)


class AskAiDialog(QDialog):
    execution_started = pyqtSignal()
    execution_finished = pyqtSignal(object)

    def __init__(
        self,
        parent: QWidget | None,
        *,
        catalog: Callable[[], Catalog],
        secrets: Callable[[], tuple[str, ...]],
        on_open: Callable[[ExplorerRequest], None],
        settings_path: Path,
        on_open_suite: Callable[[TestSuite], None] | None = None,
        on_settings: Callable[[], None] | None = None,
        on_open_data: Callable[[WorkflowPlan, ExplorerRequest, CsvImportSettings, tuple[str, ...]], None] | None = None,
        on_open_load: Callable[[WorkflowPlan, ExplorerRequest], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("askAiDialog")
        self.setWindowTitle("Ask AI")
        self.setModal(False)
        self.resize(800, 700)
        self.setMinimumSize(640, 540)
        self._catalog = catalog
        self._secrets = secrets
        self._on_open = on_open
        self._on_open_suite = on_open_suite
        self._on_settings = on_settings
        self._on_open_data = on_open_data
        self._on_open_load = on_open_load
        self.csv_settings: CsvImportSettings | None = None
        self.csv_columns: tuple[str, ...] = ()
        self._settings_path = settings_path
        self.settings = load_ai_settings(settings_path)
        self._index: CatalogIndex | None = None
        self._index_catalog: Catalog | None = None
        self._task: AiTask | None = None
        self._started = 0.0
        self._execution_error = ""
        self._activity_started = 0.0
        self._activity_secrets: tuple[str, ...] = ()
        self.read_only_result = False
        self.mode = "request"
        self.outcome: PlanOutcome | None = None
        self.explorer_request: ExplorerRequest | None = None
        self.suite_outcome: SuiteOutcome | None = None
        self.test_suite: TestSuite | None = None
        self._build()
        self._show_idle()

    # -- layout ----------------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        warning_row = QHBoxLayout()
        warning_row.setSpacing(8)
        self.warning_group = QWidget()
        warning_group_layout = QHBoxLayout(self.warning_group)
        warning_group_layout.setContentsMargins(0, 0, 0, 0)
        warning_group_layout.setSpacing(8)
        self.review_warning_icon = QLabel()
        self.review_warning_icon.setFixedSize(20, 20)
        warning_group_layout.addWidget(self.review_warning_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.review_warning = QLabel(
            "AI can make mistakes. Please review the plan before execution. "
            "Nothing will run without your approval."
        )
        self.review_warning.setObjectName("aiReviewWarning")
        self.review_warning.setWordWrap(True)
        self.review_warning.setAlignment(Qt.AlignmentFlag.AlignCenter)
        warning_group_layout.addWidget(self.review_warning, 1)
        warning_row.addStretch(1)
        warning_row.addWidget(self.warning_group, 1000)
        warning_row.addStretch(1)
        layout.addLayout(warning_row)
        self.result_view = QTextBrowser()
        self.result_view.setAccessibleName("AI plan")
        self.result_view.setOpenExternalLinks(False)
        self.result_view.setObjectName("aiPlanPreview")
        layout.addWidget(self.result_view, 1)
        self.choices_row = QHBoxLayout()
        self.choices_row.setSpacing(6)
        layout.addLayout(self.choices_row)

        footer = QHBoxLayout()
        self.copy_button = QPushButton("Copy plan JSON")
        self.copy_button.clicked.connect(self._copy_plan)
        footer.addWidget(self.copy_button)
        footer.addStretch()
        self.open_button = QPushButton("Open in API Explorer")
        self.open_button.setProperty("accent", True)
        self.open_button.clicked.connect(self._open)
        footer.addWidget(self.open_button)
        layout.addLayout(footer)

        self.connection_label = QLabel("")
        self.connection_label.setWordWrap(True)
        self.connection_label.hide()
        layout.addWidget(self.connection_label)
        composer = QFrame()
        composer.setObjectName("aiComposer")
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(10, 8, 10, 8)
        composer_layout.setSpacing(6)
        self.mode_selector = QComboBox()
        self.mode_selector.setAccessibleName("Test selector")
        for name, mode, glyph in (
            ("Single request", "request", "api-explorer"),
            ("Test Suite", "suite", "test-suites"),
            ("Data Runner", "data", "data-runner"),
            ("Load Testing", "load", "load-testing"),
        ):
            self.mode_selector.addItem(icon(glyph, theme.TEXT_MUTED, 16), name, mode)
        model = self.mode_selector.model()
        if isinstance(model, QStandardItemModel):
            model.item(2).setEnabled(self._on_open_data is not None)
            model.item(3).setEnabled(self._on_open_load is not None)
            model.item(1).setEnabled(self._on_open_suite is not None)
        self.mode_selector.currentIndexChanged.connect(
            lambda: self.set_mode(self.mode_selector.currentData())
        )
        selector_row = QHBoxLayout()
        selector_row.addWidget(self.mode_selector)
        self.csv_button = QPushButton("Choose CSV")
        self.csv_button.setMaximumWidth(240)
        self.csv_button.setToolTip("Only CSV column names are sent to AI, never row values or the file path.")
        self.csv_button.clicked.connect(self._choose_csv)
        selector_row.addWidget(self.csv_button)
        selector_row.addStretch()
        composer_layout.addLayout(selector_row)
        self.prompt_input = QPlainTextEdit()
        self.prompt_input.setObjectName("aiPrompt")
        self.prompt_input.setAccessibleName("Prompt")
        self.prompt_input.setFixedHeight(90)
        composer_layout.addWidget(self.prompt_input)

        controls = QHBoxLayout()
        self.connection_state = QLabel()
        self.connection_state.setFixedSize(16, 16)
        self.connection_state.setAccessibleName("AI connection status")
        controls.addWidget(self.connection_state)
        self.provider_icon = QLabel()
        self.provider_icon.setFixedSize(18, 18)
        controls.addWidget(self.provider_icon)
        self.model_summary = ElidingLabel()
        self.model_summary.setWordWrap(False)
        self.model_summary.setAccessibleName("Active AI provider and model (read-only)")
        controls.addWidget(self.model_summary)
        self.settings_button = QPushButton()
        self.settings_button.setObjectName("aiSettings")
        self.settings_button.setAccessibleName("AI Settings")
        self.settings_button.setToolTip("Open AI Settings")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.setVisible(self._on_settings is not None)
        self.settings_button.clicked.connect(self._manage_settings)
        controls.addWidget(self.settings_button)
        controls.addStretch(1)
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("askAiProgress")
        self.progress_label.setWordWrap(True)
        self.cancel_button = QPushButton()
        self.cancel_button.setObjectName("aiCancel")
        self.cancel_button.setAccessibleName("Cancel AI generation")
        self.cancel_button.setToolTip("Cancel generation")
        self.cancel_button.clicked.connect(self._cancel)
        controls.addWidget(self.cancel_button)
        self.ask_button = QPushButton()
        self.ask_button.setObjectName("aiAsk")
        self.ask_button.setAccessibleName("Ask AI")
        self.ask_button.setToolTip("Ask AI (Ctrl+Enter). Review the plan before sending or running.")
        self.ask_button.setProperty("accent", True)
        self.ask_button.clicked.connect(self.ask)
        controls.addWidget(self.ask_button)
        composer_layout.addLayout(controls)
        layout.addWidget(composer)
        status = QHBoxLayout()
        status.addWidget(self.progress_label, 1)
        self.activity_button = QPushButton("Activity")
        self.activity_button.setObjectName("aiTextAction")
        self.activity_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.activity_button.setCheckable(True)
        status.addWidget(self.activity_button)
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("askAiMeta")
        self.meta_label.hide()
        self.usage_strip = UsageStrip(self, compact=True)
        self.token_placeholder = QPushButton("Token details")
        self.token_placeholder.setCursor(Qt.CursorShape.PointingHandCursor)
        self.token_placeholder.setObjectName("aiTextAction")
        self.token_placeholder.setToolTip("Token details become available after a model response.")
        self.token_placeholder.clicked.connect(self._show_token_details)
        status.addWidget(self.token_placeholder)
        status.addWidget(self.usage_strip)
        layout.addLayout(status)
        self.activity_panel = QWidget()
        activity_layout = QVBoxLayout(self.activity_panel)
        activity_layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("Execution diagnostics only, not private model reasoning. Session-only; no prompts or raw responses.")
        note.setWordWrap(True)
        note.setProperty("pageDescription", True)
        activity_layout.addWidget(note)
        self.activity_view = QPlainTextEdit()
        self.activity_view.setReadOnly(True)
        self.activity_view.setAccessibleName("AI execution activity")
        self.activity_view.setMaximumBlockCount(300)
        self.activity_view.setFixedHeight(140)
        activity_layout.addWidget(self.activity_view)
        copy_activity = QPushButton("Copy activity")
        copy_activity.clicked.connect(lambda: QApplication.clipboard().setText(self.activity_view.toPlainText()))
        activity_layout.addWidget(copy_activity, 0, Qt.AlignmentFlag.AlignRight)
        self.activity_panel.hide()
        self.activity_button.toggled.connect(self.activity_panel.setVisible)
        layout.addWidget(self.activity_panel)
        self.reload_settings()

        for sequence in ("Ctrl+Return", "Ctrl+Enter"):
            QShortcut(QKeySequence(sequence), self, activated=self.ask)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self.set_mode("request")
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self.setStyleSheet(f"""
            QLabel#aiReviewWarning {{ color: {theme.TEXT_MUTED}; }}
            QFrame#aiComposer {{
                border: 1px solid {theme.PRIMARY}; border-radius: 9px;
                background: {theme.SURFACE};
            }}
            QTextBrowser#aiPlanPreview {{
                border: none; background: {theme.BACKGROUND}; padding: 8px;
            }}
            QPlainTextEdit#aiPrompt {{
                border: none; background: {theme.SURFACE}; padding: 2px;
            }}
            QPushButton#aiAsk, QPushButton#aiCancel, QPushButton#aiSettings {{
                min-width: 28px; max-width: 28px; min-height: 28px; max-height: 28px;
                padding: 0; border-radius: 14px;
            }}
            QPushButton#aiTextAction {{ border: none; color: {theme.PRIMARY}; background: transparent; }}
            QComboBox {{ border: none; background: {theme.SURFACE}; }}
        """)
        self.ask_button.setIcon(icon("arrow-up", theme.TEXT_INVERSE, 16))
        self.cancel_button.setIcon(icon("close", theme.TEXT_MUTED, 16))
        self.settings_button.setIcon(icon("settings", theme.TEXT_MUTED, 16))
        self.copy_button.setIcon(icon("copy", theme.TEXT, 16))
        self.review_warning_icon.setPixmap(light_bulb_icon("yellow", 20).pixmap(20, 20))
        self.warning_group.setMaximumWidth(
            self.review_warning.fontMetrics().horizontalAdvance(self.review_warning.text()) + 28
        )
        self.provider_icon.setPixmap(icon("sparkles", theme.TEXT_MUTED, 16).pixmap(16, 16))
        self.usage_strip.details_button.setIcon(icon("info-circle", theme.TEXT_MUTED, 16))
        self.token_placeholder.setIcon(icon("info-circle", theme.TEXT_MUTED, 16))
        for row, name in enumerate(("api-explorer", "test-suites", "data-runner", "load-testing")):
            self.mode_selector.setItemIcon(row, icon(name, theme.TEXT_MUTED, 16))
        self.refresh_connection_state()

    def refresh_connection_state(self) -> None:
        button = getattr(self.parent(), "ask_ai_button", None)
        available = bool(button and button.available)
        status = "Connected" if available else "Not available" if button else "Not checked"
        self.connection_state.setPixmap(
            icon("status-dot", theme.PRIMARY if available else theme.SKIP, 16).pixmap(16, 16)
        )
        self.connection_state.setToolTip(f"{status}: {button.toolTip()}" if button else status)
        self.connection_state.setAccessibleDescription(status)

    # -- mode --------------------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        callbacks = {"suite": self._on_open_suite, "data": self._on_open_data, "load": self._on_open_load}
        if mode not in MODES or (mode in callbacks and callbacks[mode] is None):
            mode = "request"
        changed = mode != self.mode
        self.mode = mode
        self.mode_selector.blockSignals(True)
        self.mode_selector.setCurrentIndex(self.mode_selector.findData(mode))
        self.mode_selector.blockSignals(False)
        suite = mode == "suite"
        self.prompt_input.setPlaceholderText(
            "e.g. Map this CSV to create brands, using BrandName for the name"
            if mode == "data" else
            "e.g. Load test the brands list: ramp from 1 to 10 users in 30 seconds, then hold for 60 seconds"
            if mode == "load" else
            "e.g. Regression suite for brands: create, read back, rename, delete, confirm it is gone"
            if suite
            else "e.g. List active brands whose name contains corn, newest first"
        )
        self.csv_button.setVisible(mode == "data" and not self.read_only_result)
        self.open_button.setText({
            "request": "Open in API Explorer", "suite": "Open in Test Suites",
            "data": "Open in Data Runner", "load": "Open in Load Testing",
        }[mode])
        self.copy_button.setText("Copy suite JSON" if suite else "Copy plan JSON")
        if changed and self._task is None:
            self.activity_view.clear()
            self.outcome = None
            self.explorer_request = None
            self.suite_outcome = None
            self.test_suite = None
            self._clear_choices()
            self.meta_label.setText("")
            self.usage_strip.hide()
            self._show_idle()
        self._refresh_buttons()

    def _choose_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose CSV for AI mapping", "", "CSV files (*.csv)")
        if not path:
            return
        settings = CsvImportSettings(path=path, encoding="", delimiter="")
        try:
            columns = CsvSource(settings).read_columns()
        except (OSError, UnicodeError, ValueError, csv.Error) as exc:
            QMessageBox.warning(self, "Could not read CSV", str(exc))
            return
        self.csv_settings = settings
        self.csv_columns = columns
        self.activity_view.clear()
        name = Path(path).name
        self.csv_button.setText(f"CSV: {name if len(name) <= 28 else name[:25] + '...'}")
        self.csv_button.setToolTip(
            f"{path}\nColumns: {', '.join(columns)}\nOnly column names are sent to AI; row values stay local."
        )
        self.outcome = None
        self.explorer_request = None
        self.usage_strip.hide()
        self._clear_choices()
        self._show_idle()

    # -- settings ----------------------------------------------------------------
    def current_settings(self) -> AiSettings:
        return load_ai_settings(self._settings_path)

    def reload_settings(self) -> None:
        self.settings = self.current_settings()
        location = "Local" if self.settings.provider == "ollama" else "Hosted"
        name = f"{self.settings.connection_name}: " if self.settings.connection_name else ""
        self.model_summary.setText(
            f"{name}{PROVIDER_LABELS[self.settings.provider]} / {self.settings.planner_model} ({location})"
        )
        self.model_summary.setToolTip(self.model_summary.full_text())
        self.refresh_connection_state()

    def _manage_settings(self) -> None:
        if self._task is None and not self.read_only_result and self._on_settings is not None:
            self._on_settings()
            self.reload_settings()

    def _set_connection(self, message: str, color: str) -> None:
        self.connection_label.setText(message)
        self.connection_label.setStyleSheet(f"color: {color};")
        self.connection_label.setVisible(bool(message))

    # -- asking ----------------------------------------------------------------
    def index(self) -> CatalogIndex:
        catalog = self._catalog()
        if self._index is None or self._index_catalog is not catalog:
            self._index = CatalogIndex(catalog)
            self._index_catalog = catalog
        return self._index

    def ask(self) -> None:
        if self.read_only_result or self._task is not None:
            return
        prompt = self.prompt_input.toPlainText().strip()
        if not prompt:
            self.progress_label.setText("Type what you want to test first.")
            return
        if self.mode == "data":
            if self.csv_settings is None:
                self.progress_label.setText("Choose a CSV file before generating a Data Runner plan.")
                return
            try:
                columns = CsvSource(self.csv_settings).read_columns()
            except (OSError, UnicodeError, ValueError, csv.Error) as exc:
                self.progress_label.setText(f"Could not read CSV: {exc}")
                return
            if columns != self.csv_columns:
                self.progress_label.setText("CSV columns changed. Choose the CSV again before generating.")
                return
        self.reload_settings()
        self._execution_error = ""
        self.activity_view.clear()
        self._activity_started = time.monotonic()
        self._activity_secrets = self._secrets()
        self._append_activity(f"Starting {MODE_LABELS[self.mode]} planning; "
                              f"{PROVIDER_LABELS[self.settings.provider]} / {self.settings.planner_model}; "
                              f"up to {self.settings.max_repairs + 1} model calls")
        self._usage_connection = (
            f"{self.settings.connection_name or PROVIDER_LABELS[self.settings.provider]} / "
            f"{self.settings.planner_model}"
        )
        self._usage_provider = self.settings.provider
        self.usage_strip.hide()
        self.meta_label.clear()
        self._clear_choices()
        self.outcome = None
        self.explorer_request = None
        self.suite_outcome = None
        self.test_suite = None
        self.result_view.setHtml("")
        if self.mode in ("data", "load"):
            task = _keep_alive(plan_workflow_task(
                prompt, self.settings, self.index(), self._secrets(),
                mode=self.mode, columns=self.csv_columns if self.mode == "data" else (),
            ))
            task.succeeded.connect(self._on_workflow_outcome)
        elif self.mode == "suite":
            task = _keep_alive(plan_suite_task(prompt, self.settings, self.index(), self._secrets()))
            task.succeeded.connect(self._on_suite_outcome)
        else:
            task = _keep_alive(plan_request_task(prompt, self.settings, self.index(), self._secrets()))
            task.succeeded.connect(self._on_outcome)
        task.progress.connect(self._on_progress)
        task.failed.connect(self._on_failed)
        task.done.connect(self._on_done)
        self._task = task
        self._started = time.monotonic()
        self._progress_text = "Starting"
        self._set_busy(True)
        self.execution_started.emit()
        task.start()

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._append_activity("Cancellation requested; an in-flight provider call may finish before cancellation takes effect")
            self.progress_label.setText("Cancelling...")

    def _on_progress(self, text: str) -> None:
        self._append_activity(text)
        safe = redact_text(text, self._activity_secrets)
        self._progress_text = safe if len(safe) <= 90 else safe[:87] + "..."
        self.progress_label.setToolTip(safe[:3000])
        self._tick()

    def _tick(self) -> None:
        if self._task is not None:
            seconds = int(time.monotonic() - self._started)
            self.progress_label.setText(f"{self._progress_text}... {seconds}s")

    def _on_failed(self, message: str) -> None:
        self._append_activity("Failed: " + message)
        self._execution_error = message
        self.progress_label.setText("")
        what = MODE_LABELS[self.mode]
        self.result_view.setHtml(f"<p style='color:{theme.FAIL}'><b>Could not plan the {what}.</b><br>{html.escape(message)}</p>")

    def _on_done(self) -> None:
        self._task = None
        self._set_busy(False)
        outcome = self.suite_outcome if self.mode == "suite" else self.outcome
        if self._execution_error:
            self._append_activity("Generation ended with an error or cancellation")
        elif outcome is not None:
            self._append_activity(
                f"Generation ended: status={outcome.plan.status}; "
                f"attempts={outcome.attempts}; validation issues={len(outcome.issues)}. Nothing executed."
            )
        snapshot = {
            "id": uuid.uuid4().hex,
            "mode": self.mode,
            "prompt": self.prompt_input.toPlainText(),
            "html": self.result_view.toHtml(),
            "outcome": self.outcome,
            "suite_outcome": self.suite_outcome,
            "request": self.explorer_request,
            "suite": self.test_suite,
            "meta": self.meta_label.text(),
            "usage": list(self.usage_strip.calls) if not self.usage_strip.isHidden() else [],
            "connection": getattr(self, "_usage_connection", self.model_summary.full_text()),
            "provider": getattr(self, "_usage_provider", self.settings.provider),
            "csv_settings": self.csv_settings if self.mode == "data" else None,
            "csv_columns": self.csv_columns if self.mode == "data" else (),
            "error": self._execution_error,
            "activity": self.activity_view.toPlainText(),
            "ok": not self._execution_error and outcome is not None and outcome.ok,
        }
        self.execution_finished.emit(snapshot)

    def restore_result(self, snapshot: dict) -> None:
        """Restore a completed generation without sending another model call."""
        self.set_mode(snapshot["mode"])
        self.csv_settings = snapshot.get("csv_settings")
        self.csv_columns = snapshot.get("csv_columns", ())
        self.prompt_input.setPlainText(snapshot["prompt"])
        self.model_summary.setText(snapshot["connection"])
        self.model_summary.setToolTip(snapshot["connection"])
        self.refresh_connection_state()
        self.outcome = snapshot["outcome"]
        self.suite_outcome = snapshot["suite_outcome"]
        self.explorer_request = snapshot["request"]
        self.test_suite = snapshot["suite"]
        self.result_view.setHtml(snapshot["html"])
        self.activity_view.setPlainText(snapshot.get("activity", "No activity recorded for this generation."))
        self.meta_label.setText(snapshot["meta"])
        self.progress_label.clear()
        self._clear_choices()
        if snapshot["usage"]:
            self.usage_strip.show_usage(snapshot["usage"], snapshot["connection"], snapshot["provider"])
        else:
            self.usage_strip.hide()
        self._refresh_buttons()

    def _append_activity(self, message: str) -> None:
        elapsed = time.monotonic() - self._activity_started if self._activity_started else 0
        safe = redact_text(message, self._activity_secrets)
        self.activity_view.appendPlainText(f"[{elapsed:7.1f}s] {safe[:3000]}")

    def _set_busy(self, busy: bool) -> None:
        self.ask_button.setEnabled(not busy and not self.read_only_result)
        self.cancel_button.setEnabled(busy)
        self.prompt_input.setReadOnly(busy or self.read_only_result)
        self.mode_selector.setEnabled(not busy and not self.read_only_result)
        self.csv_button.setEnabled(not busy and not self.read_only_result)
        self.settings_button.setEnabled(not busy and not self.read_only_result)
        if busy:
            self._timer.start()
            self._tick()
        else:
            self._timer.stop()
        self._refresh_buttons()

    def _show_idle(self) -> None:
        self.cancel_button.setEnabled(False)
        what = {"request": "request plan", "suite": "test suite", "data": "data-run plan", "load": "load-test plan"}[self.mode]
        self.result_view.setHtml(
            f"<div style='text-align:center; margin-top:28px;'>"
            f"<p style='font-size:28px;color:{theme.PRIMARY};'>&#10023;</p>"
            f"<p><b>Enter a request to generate a {what}</b></p>"
            f"<p style='color:{theme.TEXT_MUTED};'>The assistant will design the steps and configuration for you.</p>"
            "</div>"
        )
        self._refresh_buttons()

    def _refresh_buttons(self) -> None:
        self.token_placeholder.setVisible(self.usage_strip.isHidden())
        if self.mode == "suite":
            self.open_button.setEnabled(self._task is None and self.test_suite is not None)
            self.copy_button.setEnabled(self.suite_outcome is not None)
            return
        self.open_button.setEnabled(self._task is None and self.explorer_request is not None)
        self.copy_button.setEnabled(self.outcome is not None)

    def _show_token_details(self) -> None:
        if not self.usage_strip.isHidden():
            self.usage_strip.show_details()
            return
        QMessageBox.information(
            self,
            "AI token usage",
            "Token usage is not available for this plan yet.\n\n"
            + ("Generation is in progress. Details will be available after a model response."
               if self._task is not None else
               "Generate a plan to view the token usage reported by the provider.")
            + "\nNo token counts are estimated or assumed to be zero.",
        )

    # -- results ----------------------------------------------------------------
    def _on_workflow_outcome(self, outcome: PlanOutcome) -> None:
        from .workflow_plan import WorkflowPlan, render_workflow_outcome, to_workflow_request

        self.outcome = outcome
        self.explorer_request = None
        if outcome.ok and outcome.plan.status == "plan" and isinstance(outcome.plan, WorkflowPlan):
            self.explorer_request = to_workflow_request(outcome.plan, self.index())
        self.progress_label.clear()
        self.result_view.setHtml(render_workflow_outcome(outcome, self.index()))
        self._show_meta(outcome)
        self._offer_choices(outcome.plan.status, outcome.plan.choices)
        self._refresh_buttons()

    def _on_outcome(self, outcome: PlanOutcome) -> None:
        self.outcome = outcome
        index = self.index()
        endpoint = index.get(outcome.plan.endpoint_id)
        if outcome.plan.status == "plan" and endpoint is not None:
            self.explorer_request = to_explorer_request(
                outcome.plan, endpoint, index.filter_schema(endpoint) is not None
            )
        self.progress_label.setText("")
        self.result_view.setHtml(render_outcome(outcome, endpoint, self.explorer_request))
        self._show_meta(outcome)
        self._offer_choices(outcome.plan.status, outcome.plan.choices)
        self._refresh_buttons()

    def _on_suite_outcome(self, outcome: SuiteOutcome) -> None:
        self.suite_outcome = outcome
        index = self.index()
        conversion_error = ""
        if outcome.plan.status == "plan" and outcome.plan.cases:
            try:
                self.test_suite = to_test_suite(outcome.plan, index)
            except ValueError as exc:
                conversion_error = str(exc)
        self.progress_label.setText("")
        self.result_view.setHtml(render_suite_outcome(outcome, index, conversion_error))
        self._show_meta(outcome)
        self._offer_choices(outcome.plan.status, outcome.plan.choices)
        self._refresh_buttons()

    def _show_meta(self, outcome) -> None:
        self.meta_label.setText(outcome.model or self.settings.planner_model)
        self.usage_strip.show_usage(
            outcome.usage,
            getattr(self, "_usage_connection", self.model_summary.full_text()),
            getattr(self, "_usage_provider", self.settings.provider),
        )

    def _offer_choices(self, status: str, choices) -> None:
        if status != "clarify":
            return
        for choice in choices:
            button = QPushButton(choice)
            button.clicked.connect(lambda _checked=False, value=choice: self._answer(value))
            self.choices_row.addWidget(button)
        self.choices_row.addStretch(1)

    def _answer(self, choice: str) -> None:
        current = self.suite_outcome if self.mode == "suite" else self.outcome
        question = current.plan.question if current else ""
        prompt = self.prompt_input.toPlainText().rstrip()
        self.prompt_input.setPlainText(f"{prompt}\n(Answer to \"{question}\": {choice})")
        self.ask()

    def _clear_choices(self) -> None:
        while self.choices_row.count():
            item = self.choices_row.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()

    def _copy_plan(self) -> None:
        if self.mode == "suite":
            if self.suite_outcome is not None:
                QApplication.clipboard().setText(json.dumps(self.suite_outcome.plan.to_json(), indent=2))
                self.progress_label.setText("Suite JSON copied.")
            return
        if self.outcome is not None:
            QApplication.clipboard().setText(json.dumps(self.outcome.plan.to_json(), indent=2))
            self.progress_label.setText("Plan JSON copied.")

    def _open(self) -> None:
        if self.mode in ("data", "load"):
            from .workflow_plan import WorkflowPlan

            if (self.explorer_request is None or self.outcome is None
                    or not isinstance(self.outcome.plan, WorkflowPlan) or self.outcome.plan.mode != self.mode):
                return
            if self.mode == "data" and self._on_open_data is not None and self.csv_settings is not None:
                self._on_open_data(self.outcome.plan, self.explorer_request, self.csv_settings, self.csv_columns)
            elif self.mode == "load" and self._on_open_load is not None:
                self._on_open_load(self.outcome.plan, self.explorer_request)
            return
        if self.mode == "suite":
            if self.test_suite is not None and self._on_open_suite is not None:
                self._on_open_suite(self.test_suite)
            return
        if self.explorer_request is None:
            return
        self._on_open(self.explorer_request)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        # Closing only hides the reusable dialog: a running plan keeps going and
        # is shown when the dialog is reopened. Cancel is explicit.
        super().closeEvent(event)


def _list(items, color: str | None = None) -> str:
    style = f" style='color:{color}'" if color else ""
    return "<ul>" + "".join(f"<li{style}>{html.escape(str(item))}</li>" for item in items) + "</ul>"


def render_outcome(outcome: PlanOutcome, endpoint, request: ExplorerRequest | None) -> str:
    plan = outcome.plan
    muted = theme.TEXT_MUTED
    parts = [f"<h3 style='margin:0'>{html.escape(plan.title or 'Plan')}</h3>"]
    if plan.summary:
        parts.append(f"<p>{html.escape(plan.summary)}</p>")
    if plan.status == "unsupported":
        parts.append(f"<p style='color:{theme.WARN}'><b>No catalog endpoint can do this.</b></p>")
    elif plan.status == "clarify":
        parts.append(f"<p><b>{html.escape(plan.question)}</b><br><span style='color:{muted}'>Pick an answer below.</span></p>")
    if endpoint is not None and plan.status == "plan":
        parts.append(
            f"<p><span style='color:{theme.method_color(endpoint.method)};font-weight:700'>{endpoint.method}</span>"
            f"&nbsp;<code>{html.escape(endpoint.path)}</code>"
            f"<br><span style='color:{muted}'>{html.escape(endpoint.service)} · "
            f"{html.escape(endpoint.controller)}.{html.escape(endpoint.action)}</span></p>"
        )
        rows = [(name, value) for name, value in plan.parameters if value]
        if rows:
            parts.append("<p><b>Parameters</b></p>" + _list(f"{name} = {value}" for name, value in rows))
        if plan.filters:
            join = "any may match" if plan.filter_join == "or" else "all must match"
            readable = [
                f"{item.field} {OPERATOR_WORDS.get(item.op, item.op)} {' and '.join(item.values) if item.op == 'bt' else ', '.join(item.values)}"
                for item in plan.filters
            ]
            parts.append(f"<p><b>Filters</b> <span style='color:{muted}'>({join})</span></p>" + _list(readable))
        if plan.sort:
            parts.append(
                "<p><b>Sort</b></p>"
                + _list(f"{item.field} {'descending' if item.descending else 'ascending'}" for item in plan.sort)
            )
        if request is not None:
            encoded = [
                f"{key.split(':', 1)[1]}={value}"
                for key, value in request.values.items()
                if key in {"query:Filter", "query:Sort"} and value
            ]
            if encoded:
                parts.append(f"<p style='color:{muted}'>Encoded: <code>{html.escape('  '.join(encoded))}</code></p>")
        if plan.payload is not None:
            parts.append(
                "<p><b>Body</b></p><pre style='margin:0'>"
                + html.escape(json.dumps(plan.payload, indent=2))
                + "</pre>"
            )
        expected = request.expected_status if request is not None else plan.expected_status
        parts.append(f"<p><b>Expected status</b> {html.escape(expected or '200-299')}</p>")
    if plan.assumptions:
        parts.append("<p><b>Assumptions</b></p>" + _list(plan.assumptions, muted))
    if plan.warnings:
        parts.append("<p><b>Warnings</b></p>" + _list(plan.warnings, theme.WARN))
    if outcome.issues:
        parts.append(
            "<p><b>Problems the assistant could not fix</b></p>"
            + _list((issue.as_text() for issue in outcome.issues), theme.FAIL)
        )
    return "".join(parts)


def render_suite_outcome(outcome: SuiteOutcome, index: CatalogIndex, conversion_error: str = "") -> str:
    plan = outcome.plan
    muted = theme.TEXT_MUTED
    parts = [f"<h3 style='margin:0'>{html.escape(plan.name or 'Suite')}</h3>"]
    if plan.description:
        parts.append(f"<p>{html.escape(plan.description)}</p>")
    if plan.status == "unsupported":
        parts.append(f"<p style='color:{theme.WARN}'><b>The catalog cannot support this suite.</b></p>")
    elif plan.status == "clarify":
        parts.append(f"<p><b>{html.escape(plan.question)}</b><br><span style='color:{muted}'>Pick an answer below.</span></p>")
    if plan.status == "plan" and plan.cases:
        cleanup = sum(1 for case in plan.cases if case.phase == "cleanup")
        summary = f"{len(plan.cases)} case{'s' if len(plan.cases) != 1 else ''}"
        if cleanup:
            summary += f" · {cleanup} cleanup"
        if plan.stop_on_failure:
            summary += " · stops on first failure"
        parts.append(f"<p style='color:{muted}'>{summary}</p>")
        names = {case.key: case.name or case.key for case in plan.cases}
        for number, case in enumerate(plan.cases, start=1):
            endpoint = index.get(case.endpoint_id)
            method = endpoint.method if endpoint is not None else "?"
            path = endpoint.path if endpoint is not None else case.endpoint_id
            badge = (
                f" <span style='color:{theme.WARN}'>cleanup{' · always runs' if case.always_run else ''}</span>"
                if case.phase == "cleanup"
                else ""
            )
            parts.append(
                f"<p style='margin-bottom:0'><b>{number}. {html.escape(case.name or case.key)}</b>{badge}<br>"
                f"<span style='color:{theme.method_color(method)};font-weight:700'>{method}</span>"
                f"&nbsp;<code>{html.escape(path)}</code>"
                f"&nbsp;<span style='color:{muted}'>expects {html.escape(case.expected_status or (endpoint.expected_status if endpoint else '200-299'))}</span></p>"
            )
            details: list[str] = []
            if case.depends_on:
                details.append("after " + ", ".join(names.get(key, key) for key in case.depends_on))
            details.extend(f"{name} = {value}" for name, value in case.parameters if value)
            for item in case.filters:
                values = " and ".join(item.values) if item.op == "bt" else ", ".join(item.values)
                details.append(f"filter: {item.field} {OPERATOR_WORDS.get(item.op, item.op)} {values}")
            if case.sort:
                details.append(
                    "sort: " + ", ".join(f"{item.field} {'descending' if item.descending else 'ascending'}" for item in case.sort)
                )
            if case.payload is not None:
                details.append("body: " + json.dumps(case.payload, separators=(", ", ": ")))
            for item in case.assertions:
                meta = ASSERTION_KINDS.get(item.kind, {})
                text = str(meta.get("label", item.kind))
                if meta.get("path"):
                    text += f" [{item.path}]"
                if meta.get("value"):
                    text += f": {item.value}"
                details.append("check: " + text)
            for item in case.captures:
                source = item.path if item.source != "status" else "status code"
                details.append(f"capture: {{{{{item.name}}}}} \u2190 {source}")
            if details:
                parts.append(_list(details, muted))
        if plan.variables:
            parts.append(
                "<p><b>Variables</b></p>"
                + _list(f"{name} = {value or '(fill in before running)'}" for name, value in plan.variables)
            )
    if plan.assumptions:
        parts.append("<p><b>Assumptions</b></p>" + _list(plan.assumptions, muted))
    if plan.warnings:
        parts.append("<p><b>Warnings</b></p>" + _list(plan.warnings, theme.WARN))
    if outcome.issues:
        parts.append(
            "<p><b>Problems the assistant could not fix</b></p>"
            + _list((issue.as_text() for issue in outcome.issues), theme.FAIL)
        )
    if conversion_error:
        parts.append(f"<p style='color:{theme.FAIL}'><b>Cannot open this suite:</b> {html.escape(conversion_error)}</p>")
    return "".join(parts)