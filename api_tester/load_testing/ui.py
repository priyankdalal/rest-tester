"""Load Testing Studio UI: scenario authoring -> pre-run summary -> live
dashboard -> error explorer -> final report.

See ``platform architecture guide: load-testing-and-data-runner.md``
section 18 ("Start here next session") for the Phase 6 plan this implements.
This module mirrors ``api_tester.data_runner.ui``'s background QThread /
``EventBus``-draining pattern (see ``DataRunnerWorker``/``_drain_events``
there) rather than reinventing it, but never renders a per-request row: at
load-testing concurrency, the engine's periodic ``metric_snapshot`` events
are the only thing the live dashboard reacts to (see ``LoadEngine`` docs).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QObject, QSize, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from api_tester import theme
from api_tester.catalog import Catalog, Endpoint
from api_tester.client import parameter_enabled_key
from api_tester.data_runner.charts import MultiSeriesLineChart, OutcomeBreakdownChart, SparklineChart
from api_tester.data_runner.dashboard_widgets import ContextCard, StatCard, WizardStepper
from api_tester.execution.cancellation import CancellationController
from api_tester.execution.errors import ERROR_CATEGORIES
from api_tester.execution.events import EventBus
from api_tester.execution.models import ExecutionEnvironmentSnapshot, RequestTemplate
from api_tester.execution.persistence import RunStore, store_file_size
from api_tester.icons import icon, solid_icon
from api_tester.seeding import refresh_payload, seed_parameter
from api_tester.viewers import JsonTextEdit
from api_tester.widgets import (
    SEARCH_TEXT_ROLE,
    AccordionScrollArea,
    ResponsiveTwoColumn,
    SearchableComboBox,
    attach_table_empty_state,
    fit_combo_column,
)

from .diagnostics import Finding, diagnose, executive_summary
from .engine import LoadEngine, LoadRunOptions, LoadRunSummary
from .export_dialog import PdfExportDialog
from .pdf_export import PdfExportOptions, PdfExportResult, export_pdf
from .planner import LoadPlan, build_plan
from .report_data import LoadReportData, SampleAggregates, build_report_data, load_sample_aggregates
from .report_view import LoadRunReportView
from .run_stats import RunStatistics, downsample
from .saved_runs import (
    LoadedRun,
    SavedRunError,
    SavedRunPickerDialog,
    describe_store,
    format_bytes,
    format_timestamp,
    list_saved_runs,
    load_saved_run,
    run_duration_seconds,
)
from .scenario import (
    MVP_STAGE_KINDS,
    THRESHOLD_METRICS,
    THRESHOLD_OPERATORS,
    WRITE_METHODS,
    LoadScenario,
    LoadStage,
    SafetyLimits,
    ThresholdDefinition,
)

_OUTCOME_COLORS = {
    "PASS": theme.PASS,
    "FAIL": theme.FAIL,
    "STOPPED": theme.WARN,
    "ABORTED": theme.FAIL,
    "INCONCLUSIVE": theme.WARN,
    "RUNNING": theme.TEXT,
}

#: Stat-card titles while a run is live, and once it has completed (when
#: the cards switch to min/avg/max statistics).
_LIVE_CARD_TITLES = {
    "throughput": "Throughput",
    "latency": "P95 latency",
    "p99": "P99 latency",
    "error_rate": "Error rate",
    "completed": "Completed",
    "active_users": "Active users",
}
_COMPLETED_CARD_TITLES = {
    "throughput": "Throughput · peak",
    "latency": "Avg latency",
    "p99": "P99 · avg",
    "active_users": "Users · peak",
}

#: Charts keep this many points, so a reopened run is thinned to fit.
_CHART_POINTS = 120
_SAFETY_MIN_PANE_WIDTH = 520

#: Status of the "Run persistence" rail card.
_PERSIST_STATES = {
    "off": ("Not persisted", "Results are kept in memory only.", "TEXT_MUTED"),
    "ready": ("Will be persisted", "The next run is saved to this file.", "TEXT_MUTED"),
    "saving": ("Saving…", "Writing samples, snapshots and errors.", "ACCENT"),
    "persisted": ("Persisted", "Reopen it any time with Open saved run.", "PASS"),
    "loaded": ("Loaded from file", "Viewing a saved run.", "ACCENT"),
}

#: Stage-table column widths for Kind, Duration, Start users, End users and
#: Think time. Label is the stretch column and so is omitted.
_STAGE_COLUMN_WIDTHS = (130, 120, 120, 120, 140)


def _icon_button(
    icon_name: str, tooltip: str, *, danger: bool = False, text: str = ""
) -> QPushButton:
    """A toolbar button; icon-only by default, icon + label when ``text`` is set."""
    button = QPushButton(text)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setIcon(icon(icon_name, theme.TEXT, 18))
    button.setIconSize(QSize(18, 18))
    if text:
        # Size to the label instead of stretching across the toolbar row.
        button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    else:
        button.setFixedSize(30, 30)
    if danger:
        button.setProperty("danger", True)
    return button


def _paint_icon(button: QPushButton, icon_name: str) -> None:
    """Theme-aware icon: white on filled accent buttons (even disabled)."""
    if button.property("accent"):
        button.setIcon(solid_icon(icon_name, theme.TEXT_INVERSE, 18))
    else:
        button.setIcon(icon(icon_name, theme.TEXT, 18))


class LoadEngineWorker(QObject):
    """Runs a :class:`~api_tester.load_testing.engine.LoadEngine` off the UI thread."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, engine: LoadEngine) -> None:
        super().__init__()
        self._engine = engine

    def run(self) -> None:
        try:
            summary = self._engine.run()
            self.finished.emit(summary)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
            self.failed.emit(str(exc))


class LoadTestingTab(QWidget):
    """The Load Testing Studio workspace tab.

    Follows the Data Runner's dependency-injected shape (a
    :class:`~api_tester.catalog.Catalog` and an ``environment_provider``
    callable) so it is unit testable in isolation and wired into the main
    shell with a couple of lines.
    """

    #: Emitted when a load run stops, as ``(title, detail, ok)``.
    run_completed = pyqtSignal(str, str, bool)

    def __init__(
        self,
        catalog: Catalog,
        environment_provider: Callable[[], ExecutionEnvironmentSnapshot],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.environment_provider = environment_provider
        self.current_endpoint: Endpoint | None = None

        self.scenario: LoadScenario | None = None
        self.plan: LoadPlan | None = None
        self.engine: LoadEngine | None = None
        self._thread: QThread | None = None
        self._worker: LoadEngineWorker | None = None
        self._event_bus: EventBus | None = None
        self._event_timer = QTimer(self)
        self._event_timer.setInterval(200)
        self._event_timer.timeout.connect(self._drain_events)
        self._run_store: RunStore | None = None
        self._error_entries: list[dict[str, Any]] = []
        self._last_summary: LoadRunSummary | None = None
        self._stage_progress_bars: list[QProgressBar] = []
        self._snapshots: list[dict[str, Any]] = []
        self._final_snapshot: dict[str, Any] = {}
        self._loaded_run: LoadedRun | None = None
        self._report_findings: list[Finding] = []
        self._sample_cache_key: tuple[str, str] | None = None
        self._sample_cache: SampleAggregates | None = None
        self._last_export_dir = ""
        self._last_export_author = ""
        self._persist_state = "ready"
        self._persist_run_id = ""
        self._persist_file = ""
        self._run_started_at = ""
        self._run_finished_at = ""
        self._recent_run_files: list[str] = []
        self._size_refresh_ticks = 0
        # What the Live & Results page describes: the built plan, or a reopened run.
        self._view_stages: tuple[LoadStage, ...] = ()
        self._view_total_duration = 0.0
        self._view_endpoint_text = "—"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.endpoint_bar_widget = QWidget()
        endpoint_bar = QHBoxLayout(self.endpoint_bar_widget)
        endpoint_bar.setContentsMargins(0, 0, 0, 0)
        endpoint_bar.addWidget(QLabel("Service"))
        self.service_combo = SearchableComboBox(placeholder="Search services…")
        self.service_combo.setToolTip("Type to filter services")
        self.service_combo.setMinimumWidth(200)
        self.service_combo.currentIndexChanged.connect(self._service_index_changed)
        endpoint_bar.addWidget(self.service_combo)
        endpoint_bar.addWidget(QLabel("Endpoint"))
        self.endpoint_combo = SearchableComboBox(
            placeholder="Search by method, route, controller or action…"
        )
        self.endpoint_combo.setToolTip("Type to filter endpoints, e.g. 'get brand'")
        self.endpoint_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.endpoint_combo.currentIndexChanged.connect(self._endpoint_changed)
        endpoint_bar.addWidget(self.endpoint_combo, 1)
        layout.addWidget(self.endpoint_bar_widget)

        self._step_labels = ["Scenario", "Safety & Thresholds", "Pre-run summary", "Live & Results"]
        self.stepper = WizardStepper(self._step_labels)
        self.stepper.stepClicked.connect(self._stepper_clicked)
        layout.addWidget(self.stepper)

        self.steps = QTabWidget()
        self.steps.tabBar().hide()
        self.steps.addTab(self._build_scenario_page(), "1. Scenario")
        self.steps.addTab(self._build_safety_page(), "2. Safety && Thresholds")
        self.steps.addTab(self._build_summary_page(), "3. Pre-run summary")
        self.steps.addTab(self._build_run_page(), "4. Live && Results")
        self.steps.currentChanged.connect(self._step_changed)
        layout.addWidget(self.steps, 1)

        self.refresh_catalog(catalog)
        self._add_default_ramp()
        self._reset_live_metrics()
        self._persist_settings_changed()

    # ------------------------------------------------------------------ setup

    def refresh_catalog(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.service_combo.blockSignals(True)
        self.service_combo.clear()
        for service in catalog.services:
            self.service_combo.addItem(service.name)
        self.service_combo.blockSignals(False)
        if catalog.services:
            self._service_changed(catalog.services[0].name)

    def refresh_theme(self) -> None:
        for button, name in self._icon_buttons():
            _paint_icon(button, name)
        if hasattr(self, "results_tabs"):
            self._refresh_live_icons()
        if hasattr(self, "live_status_label"):
            self._set_live_status(
                self.live_status_label.text(),
                str(self.live_status_label.property("status") or "ready"),
            )

    def _icon_buttons(self) -> list[tuple[QPushButton, str]]:
        return [
            (self.add_stage_button, "add"),
            (self.remove_stage_button, "trash"),
            (self.move_stage_up_button, "move-up"),
            (self.move_stage_down_button, "move-down"),
            (self.add_threshold_button, "add"),
            (self.remove_threshold_button, "trash"),
            (self.build_summary_button, "verify"),
            (self.start_button, "play"),
            (self.stop_button, "stop"),
        ]

    def _refresh_live_icons(self) -> None:
        self.configure_button.setIcon(icon("settings", theme.TEXT, 16))
        self.live_stop_button.setIcon(icon("stop", theme.FAIL, 16))
        self.open_saved_run_button.setIcon(icon("open", theme.TEXT, 16))
        if hasattr(self, "report_export_button"):
            self.report_export_button.setIcon(icon("export", theme.TEXT, 16))
            self.report_copy_button.setIcon(icon("copy", theme.TEXT, 16))
        self.saved_run_close_button.setIcon(icon("close", theme.TEXT_MUTED, 12))
        self.saved_run_chip_icon.setPixmap(icon("save", theme.ACCENT, 14).pixmap(14, 14))
        if hasattr(self, "persistence_dot"):
            self._refresh_persistence_card()
        tab_icons = ("chart", "api-explorer", "warning", "warning", "verify")
        for index, icon_name in enumerate(tab_icons):
            self.results_tabs.setTabIcon(index, icon(icon_name, theme.TEXT_MUTED, 15))

    def show_results(self) -> None:
        """Brings the "Live & Results" step forward.

        Used when a bell notification routes the user back to a load run
        that finished while they were working somewhere else.
        """
        self.steps.setCurrentIndex(3)

    def _stepper_clicked(self, index: int) -> None:
        self.steps.setCurrentIndex(index)

    def _step_changed(self, index: int) -> None:
        self.stepper.set_current(index)
        live_mode = index == 3
        self.endpoint_bar_widget.setVisible(not live_mode)
        self.stepper.setVisible(not live_mode)

    def _service_index_changed(self, index: int) -> None:
        self._service_changed(self.service_combo.itemText(index) if index >= 0 else "")

    def _service_changed(self, service_name: str) -> None:
        service = next((s for s in self.catalog.services if s.name == service_name), None)
        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        if service is not None:
            for endpoint in service.endpoints:
                self.endpoint_combo.addItem(f"{endpoint.method} {endpoint.path}", endpoint.id)
                self.endpoint_combo.setItemData(
                    self.endpoint_combo.count() - 1,
                    f"{endpoint.method} {endpoint.path} {endpoint.controller} {endpoint.action}",
                    SEARCH_TEXT_ROLE,
                )
        self.endpoint_combo.blockSignals(False)
        if self.endpoint_combo.count():
            self.endpoint_combo.setCurrentIndex(0)
            self._endpoint_changed(0)

    def _endpoint_changed(self, index: int) -> None:
        if index < 0:
            self.current_endpoint = None
            return
        endpoint_id = self.endpoint_combo.itemData(index)
        endpoint = next(
            (e for service in self.catalog.services for e in service.endpoints if e.id == endpoint_id),
            None,
        )
        self.current_endpoint = endpoint
        self._draft_expected_status = endpoint.expected_status if endpoint is not None else "200-299"
        self._render_parameters_table()
        self._render_payload_editor()
        self._refresh_write_method_hint()

    def load_request(
        self,
        endpoint: Endpoint,
        values: dict[str, str] | None = None,
        payload: Any = None,
    ) -> bool:
        """Pre-selects ``endpoint`` and fills its parameter values and payload.

        Used by API Explorer so a request can be load tested without hunting
        through the service/endpoint selectors. Returns ``False`` when the
        endpoint is not part of the loaded catalog.
        """
        service_index = next(
            (
                index
                for index, service in enumerate(self.catalog.services)
                if any(item.id == endpoint.id for item in service.endpoints)
            ),
            -1,
        )
        if service_index < 0:
            return False
        service_row = self.service_combo.findText(
            self.catalog.services[service_index].name, Qt.MatchFlag.MatchExactly
        )
        if service_row < 0:
            return False
        if service_row != self.service_combo.currentIndex():
            self.service_combo.setCurrentIndex(service_row)
        endpoint_row = self.endpoint_combo.findData(endpoint.id)
        if endpoint_row < 0:
            return False
        if endpoint_row != self.endpoint_combo.currentIndex():
            self.endpoint_combo.setCurrentIndex(endpoint_row)
        else:
            self._endpoint_changed(endpoint_row)
        values = values or {}
        for row in range(self.parameters_table.rowCount()):
            source_item = self.parameters_table.item(row, 0)
            name_item = self.parameters_table.item(row, 1)
            if source_item is None or name_item is None:
                continue
            source, name = source_item.text(), name_item.text()
            key = f"{source}:{name}"
            if key not in values:
                continue
            value = values[key]
            # API Explorer keeps unticked optional query values; they are not sent.
            if values.get(parameter_enabled_key(source, name)) == "false":
                value = ""
            self.parameters_table.setItem(row, 4, QTableWidgetItem(value))
        if payload is not None:
            self.payload_editor.setPlainText(json.dumps(payload, indent=2))
        self._refresh_scenario_section_summaries()
        self.steps.setCurrentIndex(0)
        return True

    # ------------------------------------------------------------------ 1. scenario page

    def load_ai_draft(
        self, endpoint: Endpoint, values: dict[str, str], payload: Any,
        *, name: str, stages: tuple[LoadStage, ...],
        thresholds: tuple[ThresholdDefinition, ...], expected_status: str,
    ) -> bool:
        """Open a load configuration without granting permissions or starting it."""
        if self._thread is not None and self._thread.isRunning():
            raise ValueError("Stop the current load test before opening an AI draft.")
        if not self.load_request(endpoint, values, payload):
            return False
        self.payload_editor.setPlainText("" if payload is None else json.dumps(payload, indent=2))
        self._draft_expected_status = expected_status
        self.scenario_name_edit.setText(name)
        self.stages_table.setRowCount(0)
        for stage in stages:
            self._add_stage_row(
                kind=stage.kind, duration_seconds=stage.duration_seconds,
                start_users=stage.start_users, end_users=stage.end_users,
                think_time_ms=stage.think_time_ms, label=stage.label,
            )
        self.thresholds_table.setRowCount(0)
        for threshold in thresholds:
            self._add_threshold_row(
                metric=threshold.metric, operator=threshold.operator,
                target=threshold.target, label=threshold.label,
            )
        self.environment_permits_checkbox.setChecked(False)
        self.confirmed_write_checkbox.setChecked(False)
        self.confirm_checkbox.setChecked(False)
        self.auth_failure_stop_checkbox.setChecked(True)
        self.plan = None
        self.scenario = None
        self.start_button.setEnabled(False)
        self._refresh_scenario_section_summaries()
        self.steps.setCurrentIndex(0)
        return True

    def _build_scenario_page(self) -> QWidget:
        accordion = AccordionScrollArea(content_margins=(4, 4, 4, 4))

        details_content = QWidget()
        details_layout = QVBoxLayout(details_content)
        details_layout.setContentsMargins(0, 0, 0, 0)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Scenario name"))
        self.scenario_name_edit = QLineEdit()
        self.scenario_name_edit.setPlaceholderText("Optional — defaults to the endpoint id")
        self.scenario_name_edit.textChanged.connect(self._refresh_scenario_section_summaries)
        name_row.addWidget(self.scenario_name_edit, 1)
        details_layout.addLayout(name_row)

        self.write_method_hint = QLabel("")
        self.write_method_hint.setObjectName("dataRunnerStatCardSubtitle")
        self.write_method_hint.setWordWrap(True)
        details_layout.addWidget(self.write_method_hint)
        self.scenario_details_section = accordion.add_section(
            "Scenario details",
            details_content,
            expanded=False,
            summary="Endpoint defaults",
        )

        params_content = QWidget()
        params_layout = QVBoxLayout(params_content)
        params_layout.setContentsMargins(0, 0, 0, 0)
        header_row = QHBoxLayout()
        header_row.addWidget(QLabel("Path, query, header, and form parameters for the target endpoint."))
        header_row.addStretch()
        self.seed_parameters_button = QPushButton("Seed parameter values")
        self.seed_parameters_button.setIcon(icon("seed", theme.TEXT, 16))
        self.seed_parameters_button.clicked.connect(self._seed_parameters)
        header_row.addWidget(self.seed_parameters_button)
        params_layout.addLayout(header_row)
        self.parameters_table = QTableWidget(0, 5)
        self.parameters_table.setHorizontalHeaderLabels(["Source", "Name", "Type", "Required", "Value"])
        self.parameters_table.verticalHeader().setVisible(False)
        self.parameters_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.parameters_table.setMinimumHeight(180)
        attach_table_empty_state(
            self.parameters_table,
            icon_name="fields",
            title="No request values",
            guidance="Choose an endpoint that takes path, query or form parameters.",
        )
        params_layout.addWidget(self.parameters_table)
        self.request_values_section = accordion.add_section(
            "Request values",
            params_content,
            expanded=False,
            summary="No parameters",
        )

        payload_content = QWidget()
        payload_layout = QVBoxLayout(payload_content)
        payload_layout.setContentsMargins(0, 0, 0, 0)
        payload_actions = QHBoxLayout()
        payload_actions.addStretch()
        self.seed_payload_button = QPushButton("Seed payload")
        self.seed_payload_button.setIcon(icon("seed", theme.TEXT, 16))
        self.seed_payload_button.clicked.connect(self._seed_payload)
        payload_actions.addWidget(self.seed_payload_button)
        payload_layout.addLayout(payload_actions)
        self.payload_editor = JsonTextEdit()
        self.payload_editor.setMinimumHeight(180)
        self.payload_editor.textChanged.connect(self._refresh_scenario_section_summaries)
        payload_layout.addWidget(self.payload_editor)
        self.payload_section = accordion.add_section(
            "Payload (JSON body)",
            payload_content,
            expanded=False,
            summary="No payload",
        )

        stages_content = QWidget()
        stages_content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        stages_layout = QVBoxLayout(stages_content)
        stages_layout.setContentsMargins(0, 0, 0, 0)
        stage_actions = QHBoxLayout()
        self.add_stage_button = _icon_button("add", "Add a stage")
        self.add_stage_button.clicked.connect(lambda: self._add_stage_row())
        stage_actions.addWidget(self.add_stage_button)
        self.remove_stage_button = _icon_button("trash", "Remove the selected stage", danger=True)
        self.remove_stage_button.clicked.connect(self._remove_selected_stage_row)
        stage_actions.addWidget(self.remove_stage_button)
        self.move_stage_up_button = _icon_button("move-up", "Move the selected stage up")
        self.move_stage_up_button.clicked.connect(lambda: self._move_selected_stage_row(-1))
        stage_actions.addWidget(self.move_stage_up_button)
        self.move_stage_down_button = _icon_button("move-down", "Move the selected stage down")
        self.move_stage_down_button.clicked.connect(lambda: self._move_selected_stage_row(1))
        stage_actions.addWidget(self.move_stage_down_button)
        self.default_ramp_button = QPushButton("Reset to default ramp")
        self.default_ramp_button.clicked.connect(self._add_default_ramp)
        stage_actions.addWidget(self.default_ramp_button)
        stage_actions.addStretch()
        stages_layout.addLayout(stage_actions)

        self.stages_table = QTableWidget(0, 6)
        self.stages_table.setHorizontalHeaderLabels(
            ["Kind", "Duration (s)", "Start users", "End users", "Think time (ms)", "Label"]
        )
        self.stages_table.verticalHeader().setVisible(False)
        header = self.stages_table.horizontalHeader()
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        # Qt's 100px default clipped the Kind combo and elided the numeric
        # headers; these sizes come from the header and editor content widths.
        for column, width in enumerate(_STAGE_COLUMN_WIDTHS):
            self.stages_table.setColumnWidth(column, width)
        self.stages_table.setObjectName("roomyEditorTable")
        self.stages_table.verticalHeader().setDefaultSectionSize(theme.ROOMY_ROW_HEIGHT)
        self.stages_table.setMinimumHeight(330)
        self.stages_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        attach_table_empty_state(
            self.stages_table,
            icon_name="chart",
            title="No stages yet",
            guidance="Add a stage, or reset to the default ramp, to shape the load.",
        )
        stages_layout.addWidget(self.stages_table)

        self.stage_totals_label = QLabel("")
        self.stage_totals_label.setObjectName("dataRunnerStatCardSubtitle")
        stages_layout.addWidget(self.stage_totals_label)
        self.load_schedule_section = accordion.add_section(
            "Load schedule",
            stages_content,
            expanded=True,
            summary="No stages",
        )

        return accordion

    def _render_parameters_table(self) -> None:
        self.parameters_table.setRowCount(0)
        if self.current_endpoint is None:
            return
        for parameter in self.current_endpoint.parameters:
            row = self.parameters_table.rowCount()
            self.parameters_table.insertRow(row)
            self.parameters_table.setItem(row, 0, QTableWidgetItem(parameter.source))
            self.parameters_table.setItem(row, 1, QTableWidgetItem(parameter.name))
            self.parameters_table.setItem(row, 2, QTableWidgetItem(parameter.type))
            self.parameters_table.setItem(row, 3, QTableWidgetItem("Yes" if parameter.required else "No"))
            self.parameters_table.setItem(row, 4, QTableWidgetItem(seed_parameter(parameter) if parameter.required else ""))
        self._refresh_scenario_section_summaries()

    def _seed_parameters(self) -> None:
        if self.current_endpoint is None:
            return
        for row, parameter in enumerate(self.current_endpoint.parameters):
            self.parameters_table.setItem(row, 4, QTableWidgetItem(seed_parameter(parameter)))

    def _render_payload_editor(self) -> None:
        if self.current_endpoint is not None and self.current_endpoint.payload is not None:
            self.payload_editor.setPlainText(json.dumps(self.current_endpoint.payload, indent=2))
        else:
            self.payload_editor.setPlainText("")
        self._refresh_scenario_section_summaries()

    def _seed_payload(self) -> None:
        text = self.payload_editor.toPlainText().strip()
        if not text:
            if self.current_endpoint is not None and self.current_endpoint.payload is not None:
                self.payload_editor.setPlainText(json.dumps(refresh_payload(self.current_endpoint.payload), indent=2))
            return
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            QMessageBox.warning(self, "Invalid payload", f"Payload is not valid JSON: {exc}")
            return
        self.payload_editor.setPlainText(json.dumps(refresh_payload(payload), indent=2))

    def _refresh_write_method_hint(self) -> None:
        if self.current_endpoint is None:
            self.write_method_hint.setText("")
            return
        method = self.current_endpoint.method.upper()
        if method in WRITE_METHODS:
            self.write_method_hint.setText(
                f"{method} is a write method — load testing it needs 'Confirm write endpoints' "
                "enabled in Safety limits."
            )
        else:
            self.write_method_hint.setText(f"{method} is a read-only method.")
        self._refresh_scenario_section_summaries()

    def _refresh_scenario_section_summaries(self, *_args: Any) -> None:
        if not hasattr(self, "scenario_details_section"):
            return
        scenario_name = self.scenario_name_edit.text().strip()
        endpoint = self.current_endpoint
        endpoint_summary = f"{endpoint.method} {endpoint.path}" if endpoint is not None else "Choose an endpoint"
        self.scenario_details_section.set_summary(scenario_name or endpoint_summary)

        parameter_count = self.parameters_table.rowCount()
        required_count = 0
        for row in range(parameter_count):
            required = self.parameters_table.item(row, 3)
            if required is not None and required.text() == "Yes":
                required_count += 1
        self.request_values_section.set_summary(
            f"{parameter_count} parameter(s) · {required_count} required"
            if parameter_count
            else "No parameters"
        )

        payload_text = self.payload_editor.toPlainText().strip()
        if not payload_text:
            payload_summary = "No payload"
        else:
            try:
                payload = json.loads(payload_text)
                field_count = len(payload) if isinstance(payload, dict) else 1
                payload_summary = f"JSON · {field_count} top-level field(s)"
            except json.JSONDecodeError:
                payload_summary = "Invalid JSON"
        self.payload_section.set_summary(payload_summary)

    def _add_stage_row(
        self,
        *,
        kind: str = "steady",
        duration_seconds: float = 30.0,
        start_users: int = 5,
        end_users: int = 5,
        think_time_ms: int = 200,
        label: str = "",
    ) -> None:
        row = self.stages_table.rowCount()
        self.stages_table.insertRow(row)
        kind_combo = QComboBox()
        kind_combo.addItems(MVP_STAGE_KINDS)
        kind_combo.setCurrentText(kind)
        kind_combo.currentTextChanged.connect(self._refresh_stage_totals)
        self.stages_table.setCellWidget(row, 0, kind_combo)
        fit_combo_column(self.stages_table, 0, kind_combo)

        duration_spin = QDoubleSpinBox()
        duration_spin.setRange(0.1, 86400.0)
        duration_spin.setDecimals(1)
        duration_spin.setValue(duration_seconds)
        duration_spin.valueChanged.connect(self._refresh_stage_totals)
        self.stages_table.setCellWidget(row, 1, duration_spin)

        start_spin = QSpinBox()
        start_spin.setRange(0, 100000)
        start_spin.setValue(start_users)
        start_spin.valueChanged.connect(self._refresh_stage_totals)
        self.stages_table.setCellWidget(row, 2, start_spin)

        end_spin = QSpinBox()
        end_spin.setRange(0, 100000)
        end_spin.setValue(end_users)
        end_spin.valueChanged.connect(self._refresh_stage_totals)
        self.stages_table.setCellWidget(row, 3, end_spin)

        think_spin = QSpinBox()
        think_spin.setRange(0, 600000)
        think_spin.setValue(think_time_ms)
        self.stages_table.setCellWidget(row, 4, think_spin)

        self.stages_table.setItem(row, 5, QTableWidgetItem(label))
        self._refresh_stage_totals()

    def _remove_selected_stage_row(self) -> None:
        row = self.stages_table.currentRow()
        if row >= 0:
            self.stages_table.removeRow(row)
            self._refresh_stage_totals()

    def _move_selected_stage_row(self, direction: int) -> None:
        row = self.stages_table.currentRow()
        target = row + direction
        if row < 0 or not (0 <= target < self.stages_table.rowCount()):
            return
        stage = self._stage_from_row(row)
        self.stages_table.removeRow(row)
        self._add_stage_row(
            kind=stage.kind,
            duration_seconds=stage.duration_seconds,
            start_users=stage.start_users,
            end_users=stage.end_users,
            think_time_ms=stage.think_time_ms,
            label=stage.label,
        )
        # _add_stage_row always appends, so relocate the freshly added row to `target`.
        last_row = self.stages_table.rowCount() - 1
        widgets = [self.stages_table.cellWidget(last_row, col) for col in range(5)]
        label_text = self.stages_table.item(last_row, 5).text()
        self.stages_table.removeRow(last_row)
        self.stages_table.insertRow(target)
        for col, widget in enumerate(widgets):
            self.stages_table.setCellWidget(target, col, widget)
        self.stages_table.setItem(target, 5, QTableWidgetItem(label_text))
        self.stages_table.selectRow(target)
        self._refresh_stage_totals()

    def _add_default_ramp(self) -> None:
        self.stages_table.setRowCount(0)
        self._add_stage_row(kind="warm_up", duration_seconds=10.0, start_users=1, end_users=1, think_time_ms=500, label="Warm up")
        self._add_stage_row(kind="ramp_up", duration_seconds=30.0, start_users=1, end_users=10, think_time_ms=200, label="Ramp up")
        self._add_stage_row(kind="steady", duration_seconds=60.0, start_users=10, end_users=10, think_time_ms=200, label="Steady state")
        self._add_stage_row(kind="ramp_down", duration_seconds=15.0, start_users=10, end_users=0, think_time_ms=200, label="Ramp down")

    def _stage_from_row(self, row: int) -> LoadStage:
        kind_combo: QComboBox = self.stages_table.cellWidget(row, 0)
        duration_spin: QDoubleSpinBox = self.stages_table.cellWidget(row, 1)
        start_spin: QSpinBox = self.stages_table.cellWidget(row, 2)
        end_spin: QSpinBox = self.stages_table.cellWidget(row, 3)
        think_spin: QSpinBox = self.stages_table.cellWidget(row, 4)
        label_item = self.stages_table.item(row, 5)
        return LoadStage(
            kind=kind_combo.currentText(),
            duration_seconds=duration_spin.value(),
            start_users=start_spin.value(),
            end_users=end_spin.value(),
            think_time_ms=think_spin.value(),
            label=label_item.text() if label_item is not None else "",
        )

    def _refresh_stage_totals(self, *_args: Any) -> None:
        try:
            stages = [self._stage_from_row(row) for row in range(self.stages_table.rowCount())]
        except Exception:  # noqa: BLE001 - a row mid-edit; skip this refresh
            return
        total_duration = sum(stage.duration_seconds for stage in stages)
        peak_users = max((stage.peak_users() for stage in stages), default=0)
        summary = (
            f"{len(stages)} stage(s) · {total_duration:.0f}s total duration · "
            f"{peak_users} peak virtual users"
        )
        self.stage_totals_label.setText(summary)
        if hasattr(self, "load_schedule_section"):
            self.load_schedule_section.set_summary(summary)

    # ------------------------------------------------------------------ 2. safety & thresholds page

    def _build_safety_page(self) -> QWidget:
        page = QWidget()
        page.setProperty("transparentPane", True)
        layout = QVBoxLayout(page)

        limits_box = QGroupBox("Safety limits")
        form = QFormLayout(limits_box)
        self.environment_permits_checkbox = QCheckBox("This environment is allow-listed for load testing")
        form.addRow(self.environment_permits_checkbox)
        self.confirmed_write_checkbox = QCheckBox("Confirm write (POST/PUT/PATCH/DELETE) endpoints")
        form.addRow(self.confirmed_write_checkbox)
        self.get_head_only_checkbox = QCheckBox("Restrict to GET/HEAD endpoints only")
        form.addRow(self.get_head_only_checkbox)

        self.max_concurrency_spin = QSpinBox()
        self.max_concurrency_spin.setRange(1, 100000)
        self.max_concurrency_spin.setValue(50)
        form.addRow("Max concurrency (peak virtual users)", self.max_concurrency_spin)

        self.max_duration_spin = QDoubleSpinBox()
        self.max_duration_spin.setRange(1.0, 86400.0)
        self.max_duration_spin.setValue(600.0)
        form.addRow("Max scenario duration (s)", self.max_duration_spin)

        max_requests_row = QHBoxLayout()
        self.max_requests_checkbox = QCheckBox("Limit total requests")
        max_requests_row.addWidget(self.max_requests_checkbox)
        self.max_requests_spin = QSpinBox()
        self.max_requests_spin.setRange(1, 100000000)
        self.max_requests_spin.setValue(10000)
        self.max_requests_spin.setEnabled(False)
        self.max_requests_checkbox.toggled.connect(self.max_requests_spin.setEnabled)
        max_requests_row.addWidget(self.max_requests_spin)
        form.addRow(max_requests_row)

        self.request_timeout_spin = QDoubleSpinBox()
        self.request_timeout_spin.setRange(0.1, 600.0)
        self.request_timeout_spin.setValue(30.0)
        form.addRow("Request timeout (s)", self.request_timeout_spin)

        error_rate_row = QHBoxLayout()
        self.error_rate_stop_checkbox = QCheckBox("Stop on error rate above")
        self.error_rate_stop_checkbox.setChecked(True)
        error_rate_row.addWidget(self.error_rate_stop_checkbox)
        self.error_rate_stop_spin = QDoubleSpinBox()
        self.error_rate_stop_spin.setRange(0.0, 100.0)
        self.error_rate_stop_spin.setSuffix(" %")
        self.error_rate_stop_spin.setValue(50.0)
        self.error_rate_stop_checkbox.toggled.connect(self.error_rate_stop_spin.setEnabled)
        error_rate_row.addWidget(self.error_rate_stop_spin)
        form.addRow(error_rate_row)

        latency_row = QHBoxLayout()
        self.latency_stop_checkbox = QCheckBox("Stop on P95 latency above")
        latency_row.addWidget(self.latency_stop_checkbox)
        self.latency_stop_spin = QDoubleSpinBox()
        self.latency_stop_spin.setRange(1.0, 600000.0)
        self.latency_stop_spin.setSuffix(" ms")
        self.latency_stop_spin.setValue(5000.0)
        self.latency_stop_spin.setEnabled(False)
        self.latency_stop_checkbox.toggled.connect(self.latency_stop_spin.setEnabled)
        latency_row.addWidget(self.latency_stop_spin)
        form.addRow(latency_row)

        self.auth_failure_stop_checkbox = QCheckBox("Stop immediately on any authentication failure")
        self.auth_failure_stop_checkbox.setChecked(True)
        form.addRow(self.auth_failure_stop_checkbox)

        thresholds_box = QGroupBox("Pass/fail thresholds (evaluated against the final metrics snapshot)")
        thresholds_layout = QVBoxLayout(thresholds_box)
        threshold_actions = QHBoxLayout()
        self.add_threshold_button = _icon_button("add", "Add a threshold")
        self.add_threshold_button.clicked.connect(lambda: self._add_threshold_row())
        threshold_actions.addWidget(self.add_threshold_button)
        self.remove_threshold_button = _icon_button("trash", "Remove the selected threshold", danger=True)
        self.remove_threshold_button.clicked.connect(self._remove_selected_threshold_row)
        threshold_actions.addWidget(self.remove_threshold_button)
        threshold_actions.addStretch()
        thresholds_layout.addLayout(threshold_actions)

        self.thresholds_table = QTableWidget(0, 4)
        self.thresholds_table.setHorizontalHeaderLabels(["Metric", "Operator", "Target", "Label"])
        self.thresholds_table.verticalHeader().setVisible(False)
        self.thresholds_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        # Every row but the last holds an inline editor, so it needs the roomy
        # row height or the combo boxes and spin box clip against the grid line.
        self.thresholds_table.setObjectName("roomyEditorTable")
        self.thresholds_table.verticalHeader().setDefaultSectionSize(theme.ROOMY_ROW_HEIGHT)
        attach_table_empty_state(
            self.thresholds_table,
            icon_name="verify",
            title="No thresholds yet",
            guidance="Add a threshold to make the run pass or fail on a metric.",
        )
        thresholds_layout.addWidget(self.thresholds_table)

        # 520 px panes put the breakpoint at ~1050 px, matching the Data Runner
        # source page (1040). Lower values never trigger inside the main window,
        # whose minimum width keeps this page around 900 px.
        self.safety_columns = ResponsiveTwoColumn(
            limits_box, thresholds_box, min_pane_width=_SAFETY_MIN_PANE_WIDTH
        )
        layout.addWidget(self.safety_columns, 1)

        # Stacked, the two boxes are taller than the step page, so the page
        # scrolls rather than clipping the lower one.
        scroller = QScrollArea()
        scroller.setObjectName("accordionScroll")
        scroller.setWidgetResizable(True)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroller.setWidget(page)
        return scroller

    def _add_threshold_row(
        self, *, metric: str = "p95_ms", operator: str = "<=", target: float = 1000.0, label: str = ""
    ) -> None:
        row = self.thresholds_table.rowCount()
        self.thresholds_table.insertRow(row)
        metric_combo = QComboBox()
        metric_combo.addItems(THRESHOLD_METRICS)
        metric_combo.setCurrentText(metric)
        self.thresholds_table.setCellWidget(row, 0, metric_combo)

        operator_combo = QComboBox()
        operator_combo.addItems(THRESHOLD_OPERATORS)
        operator_combo.setCurrentText(operator)
        self.thresholds_table.setCellWidget(row, 1, operator_combo)

        target_spin = QDoubleSpinBox()
        target_spin.setRange(-1000000.0, 1000000.0)
        target_spin.setDecimals(3)
        target_spin.setValue(target)
        self.thresholds_table.setCellWidget(row, 2, target_spin)

        self.thresholds_table.setItem(row, 3, QTableWidgetItem(label))

    def _remove_selected_threshold_row(self) -> None:
        row = self.thresholds_table.currentRow()
        if row >= 0:
            self.thresholds_table.removeRow(row)

    def _threshold_from_row(self, row: int) -> ThresholdDefinition:
        metric_combo: QComboBox = self.thresholds_table.cellWidget(row, 0)
        operator_combo: QComboBox = self.thresholds_table.cellWidget(row, 1)
        target_spin: QDoubleSpinBox = self.thresholds_table.cellWidget(row, 2)
        label_item = self.thresholds_table.item(row, 3)
        return ThresholdDefinition(
            metric=metric_combo.currentText(),
            operator=operator_combo.currentText(),
            target=target_spin.value(),
            label=label_item.text() if label_item is not None else "",
        )

    def _safety_limits_from_form(self) -> SafetyLimits:
        return SafetyLimits(
            environment_permits_load_test=self.environment_permits_checkbox.isChecked(),
            confirmed_write_endpoints=self.confirmed_write_checkbox.isChecked(),
            get_head_only=self.get_head_only_checkbox.isChecked(),
            max_concurrency=self.max_concurrency_spin.value(),
            max_duration_seconds=self.max_duration_spin.value(),
            max_total_requests=self.max_requests_spin.value() if self.max_requests_checkbox.isChecked() else None,
            request_timeout_seconds=self.request_timeout_spin.value(),
            error_rate_stop_threshold=(
                self.error_rate_stop_spin.value() / 100.0 if self.error_rate_stop_checkbox.isChecked() else None
            ),
            latency_stop_ms=self.latency_stop_spin.value() if self.latency_stop_checkbox.isChecked() else None,
            auth_failure_stop=self.auth_failure_stop_checkbox.isChecked(),
        )

    # ------------------------------------------------------------------ 3. pre-run summary page

    def _build_summary_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        actions_row = QHBoxLayout()
        self.build_summary_button = _icon_button(
            "verify", "Build the load plan and show the pre-run summary",
            text="Build load plan",
        )
        self.build_summary_button.clicked.connect(self._build_and_render_plan)
        actions_row.addWidget(self.build_summary_button)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        summary_box = QGroupBox("Pre-run summary")
        summary_form = QFormLayout(summary_box)
        self.summary_environment_label = QLabel("–")
        summary_form.addRow("Environment", self.summary_environment_label)
        self.summary_endpoint_label = QLabel("–")
        summary_form.addRow("Endpoint", self.summary_endpoint_label)
        self.summary_write_label = QLabel("–")
        summary_form.addRow("Write endpoint", self.summary_write_label)
        self.summary_duration_label = QLabel("–")
        summary_form.addRow("Planned duration", self.summary_duration_label)
        self.summary_peak_users_label = QLabel("–")
        summary_form.addRow("Peak virtual users", self.summary_peak_users_label)
        self.summary_estimate_label = QLabel("–")
        summary_form.addRow("Estimated request count", self.summary_estimate_label)
        self.summary_thresholds_label = QLabel("–")
        summary_form.addRow("Thresholds configured", self.summary_thresholds_label)
        layout.addWidget(summary_box)

        self.plan_issues_list = QListWidget()
        self.plan_issues_list.setMaximumHeight(140)
        layout.addWidget(self.plan_issues_list)

        self.confirm_checkbox = QCheckBox(
            "I understand this sends real requests against the selected environment and endpoint."
        )
        self.confirm_checkbox.toggled.connect(self._refresh_start_enabled)
        layout.addWidget(self.confirm_checkbox)

        persist_box = QGroupBox("Run persistence")
        persist_layout = QVBoxLayout(persist_box)
        self.persist_checkbox = QCheckBox("Persist this run")
        self.persist_checkbox.setToolTip("Save this run to a SQLite file so it can be reopened later")
        self.persist_checkbox.setChecked(True)
        persist_layout.addWidget(self.persist_checkbox)
        persist_description = QLabel(
            "Saves the scenario, every request sample, metric snapshots, error signatures and the final "
            "report, so the run can be reopened from Live & Results → Open saved run. Required for the "
            "Errors tab. Header values are never saved."
        )
        persist_description.setObjectName("dataRunnerStatCardSubtitle")
        persist_description.setWordWrap(True)
        persist_layout.addWidget(persist_description)
        persist_row = QHBoxLayout()
        self.persist_path = QLineEdit(self._default_persist_path())
        self.persist_path.setToolTip("Path to the .db file that stores load runs; one file can hold many runs")
        persist_row.addWidget(self.persist_path, 1)
        self.persist_browse_button = QPushButton("Browse…")
        self.persist_browse_button.setToolTip("Choose where to store load runs")
        self.persist_browse_button.clicked.connect(self._browse_persist_path)
        persist_row.addWidget(self.persist_browse_button)
        persist_layout.addLayout(persist_row)
        self.persist_size_label = QLabel("")
        self.persist_size_label.setObjectName("dataRunnerStatCardSubtitle")
        persist_layout.addWidget(self.persist_size_label)
        self.persist_checkbox.toggled.connect(self._persist_settings_changed)
        self.persist_path.textChanged.connect(self._persist_settings_changed)
        layout.addWidget(persist_box)

        run_actions = QHBoxLayout()
        self.start_button = _icon_button("play", "Start the load test")
        self.start_button.setProperty("accent", True)
        _paint_icon(self.start_button, "play")
        self.start_button.setEnabled(False)
        self.start_button.clicked.connect(self._start_run)
        run_actions.addWidget(self.start_button)
        self.stop_button = _icon_button("stop", "Stop the load test", danger=True)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_run)
        run_actions.addWidget(self.stop_button)
        self.run_status_label = QLabel("Not run yet.")
        run_actions.addWidget(self.run_status_label, 1)
        layout.addLayout(run_actions)
        layout.addStretch()

        return page

    def _default_persist_path(self) -> str:
        return str(Path.cwd() / "load_test_runs.db")

    def _browse_persist_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Choose a run store file", self.persist_path.text(), "SQLite DB (*.db)")
        if path:
            self.persist_path.setText(path)

    def _persist_settings_changed(self, *_args: Any) -> None:
        enabled = self.persist_checkbox.isChecked()
        self.persist_path.setEnabled(enabled)
        self.persist_browse_button.setEnabled(enabled)
        self._refresh_persist_size_label()
        if self._thread is None and self._persist_state in ("off", "ready"):
            self._persist_state = "ready" if enabled else "off"
            if hasattr(self, "persistence_dot"):
                self._refresh_persistence_card()

    def _refresh_persist_size_label(self) -> None:
        if not self.persist_checkbox.isChecked():
            self.persist_size_label.setText("Off — results are kept in memory only.")
            return
        path = self.persist_path.text().strip() or self._default_persist_path()
        self.persist_size_label.setText(f"Size · {describe_store(path)}")

    def _build_scenario(self) -> LoadScenario:
        if self.current_endpoint is None:
            raise ValueError("Select a service and endpoint first.")
        environment = self.environment_provider()

        values: dict[str, str] = {}
        for row in range(self.parameters_table.rowCount()):
            source = self.parameters_table.item(row, 0).text()
            name = self.parameters_table.item(row, 1).text()
            value_item = self.parameters_table.item(row, 4)
            values[f"{source}:{name}"] = value_item.text() if value_item is not None else ""

        payload_text = self.payload_editor.toPlainText().strip()
        payload: Any = None
        if payload_text:
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Payload is not valid JSON: {exc}") from exc

        template = RequestTemplate(
            endpoint_id=self.current_endpoint.id,
            service=self.current_endpoint.service,
            method=self.current_endpoint.method,
            path=self.current_endpoint.path,
            values=values,
            payload=payload,
            expected_status=self._draft_expected_status,
        )

        if self.stages_table.rowCount() == 0:
            raise ValueError("Add at least one load stage first.")
        stages = tuple(self._stage_from_row(row) for row in range(self.stages_table.rowCount()))
        thresholds = tuple(self._threshold_from_row(row) for row in range(self.thresholds_table.rowCount()))

        return LoadScenario(
            environment=environment,
            endpoint=self.current_endpoint,
            template=template,
            stages=stages,
            limits=self._safety_limits_from_form(),
            thresholds=thresholds,
            name=self.scenario_name_edit.text().strip(),
        )

    def _build_and_render_plan(self) -> LoadPlan | None:
        try:
            scenario = self._build_scenario()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot build scenario", str(exc))
            return None
        self.scenario = scenario
        plan = build_plan(scenario)
        self.plan = plan
        self._render_plan(plan)
        return plan

    def _render_plan(self, plan: LoadPlan) -> None:
        scenario = plan.scenario
        endpoint = scenario.endpoint
        base_url = scenario.environment.base_url(endpoint.service)
        estimate = plan.estimated_request_count()

        self.summary_environment_label.setText(scenario.environment.environment_name)
        self.summary_endpoint_label.setText(f"{endpoint.method} {base_url}{endpoint.path}")
        self.summary_write_label.setText("Yes" if endpoint.method.upper() in WRITE_METHODS else "No")
        self.summary_duration_label.setText(f"{plan.total_duration_seconds:.0f}s")
        self.summary_peak_users_label.setText(str(plan.peak_users))
        self.summary_estimate_label.setText(f"~{estimate:,}" if estimate is not None else "Not computable (zero think time)")
        self.summary_thresholds_label.setText(str(len(scenario.thresholds)))

        self.plan_issues_list.clear()
        for issue in plan.issues:
            item = QListWidgetItem(f"[{issue.severity.upper()}] {issue.message}")
            item.setForeground(QColor(theme.FAIL if issue.severity == "error" else theme.WARN))
            self.plan_issues_list.addItem(item)
        if not plan.issues:
            self.plan_issues_list.addItem("No issues — ready to start.")

        self._refresh_live_context(plan)
        self._refresh_start_enabled()

    def _refresh_live_context(self, plan: LoadPlan) -> None:
        if self._loaded_run is not None:
            # A reopened run owns the page until it is closed or a new run starts.
            return
        scenario = plan.scenario
        endpoint = scenario.endpoint
        self._view_stages = tuple(scenario.stages)
        self._view_total_duration = plan.total_duration_seconds
        self._view_endpoint_text = f"{endpoint.method} {endpoint.path}"
        scenario_name = scenario.name or endpoint.id
        self.live_title_label.setText(f"{scenario_name} — {scenario.environment.environment_name}")
        self.live_subtitle_label.setText(
            f"Single endpoint · Closed workload · {plan.peak_users} peak virtual users"
        )
        self.live_method_label.setText(endpoint.method.upper())
        self.live_endpoint_label.setText(endpoint.path)
        self.live_service_label.setText(endpoint.service)
        think_times = [stage.think_time_ms for stage in scenario.stages]
        think_time = max(think_times) if think_times else 0
        self.live_workload_values["users"].setText(str(plan.peak_users))
        self.live_workload_values["think_time"].setText(f"{think_time:,} ms")
        self.live_workload_values["duration"].setText(f"{plan.total_duration_seconds:.0f}s")
        self.live_workload_values["timeout"].setText(f"{scenario.limits.request_timeout_seconds:g}s")
        self._rebuild_stage_progress(scenario.stages)

    def _rebuild_stage_progress(self, stages: tuple[LoadStage, ...]) -> None:
        while self.stage_progress_layout.count():
            item = self.stage_progress_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._stage_progress_bars = []
        for stage in stages:
            stage_widget = QWidget()
            stage_layout = QVBoxLayout(stage_widget)
            stage_layout.setContentsMargins(0, 0, 0, 0)
            stage_layout.setSpacing(2)
            progress = QProgressBar()
            progress.setObjectName("loadTestingStageProgress")
            progress.setRange(0, 1000)
            progress.setValue(0)
            progress.setTextVisible(False)
            stage_layout.addWidget(progress)
            label = QLabel(stage.label or stage.kind.replace("_", " ").title())
            label.setObjectName("loadTestingRunSubtitle")
            stage_layout.addWidget(label)
            self.stage_progress_layout.addWidget(stage_widget, max(1, int(stage.duration_seconds * 10)))
            self._stage_progress_bars.append(progress)

    def _update_stage_progress(self, elapsed_seconds: float) -> None:
        stages = self._view_stages
        if not stages:
            return
        remaining = elapsed_seconds
        current_index: int | None = None
        for index, stage in enumerate(stages):
            if remaining >= stage.duration_seconds:
                fraction = 1.0
                remaining -= stage.duration_seconds
            else:
                fraction = max(0.0, remaining / stage.duration_seconds)
                if current_index is None:
                    current_index = index
                remaining = 0.0
            if index < len(self._stage_progress_bars):
                self._stage_progress_bars[index].setValue(int(fraction * 1000))
        if current_index is None:
            current_index = len(stages) - 1
        stage = stages[current_index]
        self.live_stage_label.setText(
            f"Stage {current_index + 1} of {len(stages)} · "
            f"{stage.label or stage.kind.replace('_', ' ').title()}"
        )

    def _set_live_status(self, text: str, status: str) -> None:
        self.live_status_label.setText(text)
        for widget in (self.live_status_pill, self.live_status_label):
            widget.setProperty("status", status)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        status_color = {
            "running": theme.PASS,
            "pass": theme.PASS,
            "fail": theme.FAIL,
            "aborted": theme.FAIL,
            "stopped": theme.WARN,
            "inconclusive": theme.WARN,
        }.get(status, theme.TEXT_MUTED)
        self.live_status_icon.setPixmap(icon("status-dot", status_color, 14).pixmap(14, 14))

    def _refresh_start_enabled(self, *_args: Any) -> None:
        can_start = self.plan is not None and self.plan.is_executable and self.confirm_checkbox.isChecked()
        self.start_button.setEnabled(can_start and self._thread is None)

    # ------------------------------------------------------------------ 4. live & results page

    def _build_run_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QFrame()
        header.setObjectName("loadTestingRunHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 10, 12, 10)
        hero_icon = QLabel("↯")
        hero_icon.setObjectName("loadTestingHeroIcon")
        hero_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_icon.setFixedSize(34, 34)
        hero_icon.setText("")
        hero_icon.setPixmap(icon("load-testing", theme.PRIMARY, 20).pixmap(20, 20))
        header_layout.addWidget(hero_icon)
        title_column = QVBoxLayout()
        title_column.setSpacing(1)
        self.live_title_label = QLabel("Load test not started")
        self.live_title_label.setObjectName("loadTestingRunTitle")
        title_column.addWidget(self.live_title_label)
        self.live_subtitle_label = QLabel("Build and confirm a plan to begin.")
        self.live_subtitle_label.setObjectName("loadTestingRunSubtitle")
        title_column.addWidget(self.live_subtitle_label)
        header_layout.addLayout(title_column, 1)
        self.saved_run_chip = QFrame()
        self.saved_run_chip.setObjectName("loadTestingSavedRunChip")
        chip_layout = QHBoxLayout(self.saved_run_chip)
        chip_layout.setContentsMargins(8, 2, 4, 2)
        chip_layout.setSpacing(6)
        self.saved_run_chip_icon = QLabel()
        self.saved_run_chip_icon.setFixedSize(14, 14)
        chip_layout.addWidget(self.saved_run_chip_icon)
        self.saved_run_chip_label = QLabel("")
        self.saved_run_chip_label.setObjectName("loadTestingSavedRunChipText")
        chip_layout.addWidget(self.saved_run_chip_label)
        self.saved_run_close_button = QToolButton()
        self.saved_run_close_button.setObjectName("loadTestingSavedRunChipClose")
        self.saved_run_close_button.setToolTip("Close the saved run")
        self.saved_run_close_button.setAccessibleName("Close the saved run")
        self.saved_run_close_button.setIconSize(QSize(12, 12))
        self.saved_run_close_button.clicked.connect(self.close_saved_run)
        chip_layout.addWidget(self.saved_run_close_button)
        self.saved_run_chip.hide()
        header_layout.addWidget(self.saved_run_chip)
        self.open_saved_run_button = QToolButton()
        self.open_saved_run_button.setObjectName("endpointSplitButton")
        self.open_saved_run_button.setText("Open saved run")
        self.open_saved_run_button.setToolTip("Open a load run saved to a SQLite file")
        self.open_saved_run_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.open_saved_run_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.open_saved_run_button.setIconSize(QSize(16, 16))
        self.open_saved_run_menu = QMenu(self.open_saved_run_button)
        self.open_saved_run_menu.aboutToShow.connect(self._populate_open_saved_run_menu)
        self.open_saved_run_button.setMenu(self.open_saved_run_menu)
        self.open_saved_run_button.clicked.connect(self._browse_saved_run)
        header_layout.addWidget(self.open_saved_run_button)
        self.live_status_pill = QFrame()
        self.live_status_pill.setObjectName("loadTestingStatusPill")
        self.live_status_pill.setProperty("status", "ready")
        pill_layout = QHBoxLayout(self.live_status_pill)
        pill_layout.setContentsMargins(6, 3, 10, 3)
        pill_layout.setSpacing(2)
        self.live_status_icon = QLabel()
        self.live_status_icon.setFixedSize(14, 14)
        pill_layout.addWidget(self.live_status_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.live_status_label = QLabel("READY")
        self.live_status_label.setObjectName("loadTestingStatusPillText")
        self.live_status_label.setProperty("status", "ready")
        pill_layout.addWidget(self.live_status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.live_status_pill)
        self.configure_button = QPushButton("Configure")
        self.configure_button.setIcon(icon("settings", theme.TEXT, 16))
        self.configure_button.clicked.connect(self._configure_clicked)
        header_layout.addWidget(self.configure_button)
        self.live_stop_button = QPushButton("Stop")
        self.live_stop_button.setIcon(icon("stop", theme.FAIL, 16))
        self.live_stop_button.setProperty("danger", True)
        self.live_stop_button.setEnabled(False)
        self.live_stop_button.clicked.connect(self._stop_run)
        header_layout.addWidget(self.live_stop_button)
        layout.addWidget(header)

        stage_strip = QFrame()
        stage_strip.setObjectName("loadTestingStageStrip")
        stage_layout = QVBoxLayout(stage_strip)
        stage_layout.setContentsMargins(10, 7, 10, 8)
        stage_header = QHBoxLayout()
        self.live_stage_label = QLabel("Stage —")
        self.live_stage_label.setObjectName("loadTestingSectionTitle")
        stage_header.addWidget(self.live_stage_label)
        stage_header.addStretch()
        self.live_elapsed_label = QLabel("00:00")
        self.live_elapsed_label.setObjectName("loadTestingContextValue")
        stage_header.addWidget(self.live_elapsed_label)
        stage_layout.addLayout(stage_header)
        self.stage_progress_layout = QHBoxLayout()
        self.stage_progress_layout.setSpacing(4)
        stage_layout.addLayout(self.stage_progress_layout)
        layout.addWidget(stage_strip)

        body = QHBoxLayout()
        body.setSpacing(8)
        context_rail = QWidget()
        context_rail.setFixedWidth(230)
        context_layout = QVBoxLayout(context_rail)
        context_layout.setContentsMargins(0, 0, 0, 0)
        context_layout.setSpacing(8)

        request_card = ContextCard("Request mix")
        self.live_method_label = QLabel("—")
        self.live_method_label.setObjectName("loadTestingEndpointMethod")
        request_card.body.addWidget(self.live_method_label)
        self.live_endpoint_label = QLabel("Choose an endpoint")
        self.live_endpoint_label.setObjectName("loadTestingRequestPath")
        self.live_endpoint_label.setWordWrap(True)
        request_card.body.addWidget(self.live_endpoint_label)
        self.live_service_label = QLabel("No service selected")
        self.live_service_label.setObjectName("loadTestingRunSubtitle")
        request_card.body.addWidget(self.live_service_label)
        context_layout.addWidget(request_card)

        workload_card = ContextCard("Workload profile")
        self.live_workload_values: dict[str, QLabel] = {}
        for key, title in (
            ("users", "Peak users"),
            ("think_time", "Think time"),
            ("duration", "Max duration"),
            ("timeout", "Request timeout"),
        ):
            row = QHBoxLayout()
            label = QLabel(title)
            label.setObjectName("loadTestingRunSubtitle")
            row.addWidget(label)
            row.addStretch()
            value = QLabel("—")
            value.setObjectName("loadTestingContextValue")
            row.addWidget(value)
            workload_card.body.addLayout(row)
            self.live_workload_values[key] = value
        context_layout.addWidget(workload_card)

        outcome_card = ContextCard("Outcome mix")
        self.outcome_chart = OutcomeBreakdownChart()
        outcome_card.body.addWidget(self.outcome_chart)
        context_layout.addWidget(outcome_card)
        context_layout.addWidget(self._build_persistence_card())
        self.amplification_warning_label = QLabel(
            "Downstream amplification\nReporting requests may fan out to other Rest Tester services."
        )
        self.amplification_warning_label.setObjectName("loadTestingWarning")
        self.amplification_warning_label.setWordWrap(True)
        context_layout.addWidget(self.amplification_warning_label)
        context_layout.addStretch()
        body.addWidget(context_rail)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(8)
        stats_row = QGridLayout()
        stats_row.setHorizontalSpacing(6)
        stats_row.setVerticalSpacing(6)
        self.stat_cards: dict[str, StatCard] = {}
        for key, title in _LIVE_CARD_TITLES.items():
            card = StatCard(title)
            self.stat_cards[key] = card
            position = len(self.stat_cards) - 1
            stats_row.addWidget(card, position // 6, position % 6)
        for key, title in (("passed", "Passed"), ("failed", "Failed")):
            card = StatCard(title, page)
            card.hide()
            self.stat_cards[key] = card
        workspace_layout.addLayout(stats_row)

        self.run_progress_bar = QProgressBar()
        self.run_progress_bar.setObjectName("suiteRunProgress")
        self.run_progress_bar.setTextVisible(False)
        workspace_layout.addWidget(self.run_progress_bar)
        self.progress_left_label = QLabel("Not run yet.")
        self.progress_left_label.setObjectName("dataRunnerStatCardSubtitle")
        workspace_layout.addWidget(self.progress_left_label)

        self.results_tabs = QTabWidget()
        self.results_tabs.setObjectName("loadTestingResultsTabs")
        self.results_tabs.addTab(self._build_dashboard_tab(), "Live dashboard")
        self.results_tabs.addTab(self._build_endpoint_tab(), "Endpoint")
        self.results_tabs.addTab(self._build_errors_tab(), "Errors")
        self.results_tabs.addTab(self._build_warnings_tab(), "Warnings")
        self.results_tabs.addTab(self._build_report_tab(), "Final report")
        self._refresh_live_icons()
        workspace_layout.addWidget(self.results_tabs, 1)
        body.addWidget(workspace, 1)
        layout.addLayout(body, 1)

        return page

    def _build_dashboard_tab(self) -> QWidget:
        tab = QWidget()
        layout = QGridLayout(tab)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        throughput_box = QFrame()
        throughput_box.setObjectName("loadTestingSectionCard")
        throughput_layout = QVBoxLayout(throughput_box)
        throughput_title = QLabel("Throughput over time")
        throughput_title.setObjectName("loadTestingSectionTitle")
        throughput_layout.addWidget(throughput_title)
        self.throughput_chart = MultiSeriesLineChart(
            (("Completed", theme.ACCENT), ("Target", theme.PATCH)),
            unit="",
        )
        throughput_layout.addWidget(self.throughput_chart, 1)
        layout.addWidget(throughput_box, 0, 0)

        latency_box = QFrame()
        latency_box.setObjectName("loadTestingSectionCard")
        latency_layout = QVBoxLayout(latency_box)
        latency_title = QLabel("Latency percentiles")
        latency_title.setObjectName("loadTestingSectionTitle")
        latency_layout.addWidget(latency_title)
        self.latency_chart = MultiSeriesLineChart(
            (("P50", theme.ACCENT), ("P95", theme.WARN), ("P99", theme.FAIL)),
            unit=" ms",
        )
        latency_layout.addWidget(self.latency_chart, 1)
        self.latency_summary_label = QLabel("min – / mean – / p50 – / p95 – / p99 – / max –")
        self.latency_summary_label.setObjectName("loadTestingRunSubtitle")
        latency_layout.addWidget(self.latency_summary_label)
        layout.addWidget(latency_box, 0, 1)

        users_box = QFrame()
        users_box.setObjectName("loadTestingSectionCard")
        users_layout = QVBoxLayout(users_box)
        users_title = QLabel("Active virtual users")
        users_title.setObjectName("loadTestingSectionTitle")
        users_layout.addWidget(users_title)
        self.active_users_chart = SparklineChart(mode="line", color=theme.ACCENT)
        users_layout.addWidget(self.active_users_chart)
        layout.addWidget(users_box, 1, 0)

        recent_box = QFrame()
        recent_box.setObjectName("loadTestingSectionCard")
        recent_layout = QVBoxLayout(recent_box)
        recent_title = QLabel("Recent error signatures")
        recent_title.setObjectName("loadTestingSectionTitle")
        recent_layout.addWidget(recent_title)
        self.recent_errors_table = QTableWidget(0, 4)
        self.recent_errors_table.setHorizontalHeaderLabels(["Count", "Endpoint", "Category", "Message"])
        self.recent_errors_table.verticalHeader().setVisible(False)
        self.recent_errors_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        recent_layout.addWidget(self.recent_errors_table)
        attach_table_empty_state(
            self.recent_errors_table,
            icon_name="verify",
            title="No errors recorded",
            guidance="Every request in this run has succeeded so far.",
        )
        layout.addWidget(recent_box, 1, 1)
        layout.setRowStretch(0, 3)
        layout.setRowStretch(1, 2)

        return tab

    def _build_endpoint_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.endpoint_metrics_table = QTableWidget(0, 8)
        self.endpoint_metrics_table.setHorizontalHeaderLabels(
            ["Endpoint", "Requests", "Passed", "Failed", "P50", "P95", "P99", "Throughput"]
        )
        self.endpoint_metrics_table.verticalHeader().setVisible(False)
        self.endpoint_metrics_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        attach_table_empty_state(
            self.endpoint_metrics_table,
            icon_name="chart",
            title="No endpoint metrics yet",
            guidance="Run a load test to see this endpoint's latency and throughput.",
        )
        layout.addWidget(self.endpoint_metrics_table)
        return tab

    def _build_warnings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("Advisory warnings published by the engine (e.g. amplification risk)."))
        self.run_warnings_list = QListWidget()
        layout.addWidget(self.run_warnings_list)
        return tab

    def _build_errors_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel("Categorized, de-duplicated failures from the run store (requires persistence)."))

        filter_row = QHBoxLayout()
        self.errors_search_box = QLineEdit()
        self.errors_search_box.setPlaceholderText("Search category, endpoint, or message...")
        self.errors_search_box.textChanged.connect(self._apply_errors_filter)
        filter_row.addWidget(self.errors_search_box, 1)
        self.errors_category_filter = QComboBox()
        self.errors_category_filter.addItem("All categories")
        self.errors_category_filter.addItems(ERROR_CATEGORIES)
        self.errors_category_filter.currentTextChanged.connect(self._apply_errors_filter)
        filter_row.addWidget(self.errors_category_filter)
        layout.addLayout(filter_row)

        self.run_errors_list = QListWidget()
        layout.addWidget(self.run_errors_list, 1)
        self.errors_filter_summary_label = QLabel("")
        self.errors_filter_summary_label.setObjectName("dataRunnerStatCardSubtitle")
        layout.addWidget(self.errors_filter_summary_label)
        return tab

    def _build_report_tab(self) -> QWidget:
        self.report_view = LoadRunReportView()
        self.report_outcome_label = self.report_view.outcome_label
        self.report_stop_reason_label = self.report_view.stop_reason_label
        self.report_totals_label = self.report_view.totals_label
        self.thresholds_result_table = self.report_view.thresholds_table
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(4)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 8, 0)
        actions.setSpacing(8)
        self.report_actions_label = QLabel("")
        self.report_actions_label.setObjectName("loadTestingRunSubtitle")
        actions.addWidget(self.report_actions_label, 1)
        self.report_copy_button = QPushButton("Copy summary")
        self.report_copy_button.setToolTip("Copy the executive summary and findings as plain text")
        self.report_copy_button.setEnabled(False)
        self.report_copy_button.clicked.connect(self._copy_report_summary)
        actions.addWidget(self.report_copy_button)
        self.report_export_button = QPushButton("Export PDF")
        self.report_export_button.setObjectName("loadTestingExportPdfButton")
        self.report_export_button.setToolTip("Export the detailed report with charts and findings to PDF")
        self.report_export_button.setEnabled(False)
        self.report_export_button.clicked.connect(self._export_pdf_clicked)
        actions.addWidget(self.report_export_button)
        layout.addLayout(actions)
        layout.addWidget(self.report_view, 1)
        return tab

    def _build_persistence_card(self) -> ContextCard:
        """Rail card saying whether the run on screen is persisted, where, and how big the file is."""
        card = ContextCard("Run persistence")
        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self.persistence_dot = QLabel()
        self.persistence_dot.setFixedSize(10, 10)
        status_row.addWidget(self.persistence_dot)
        self.persistence_status_label = QLabel("")
        self.persistence_status_label.setObjectName("loadTestingContextValue")
        status_row.addWidget(self.persistence_status_label, 1)
        card.body.addLayout(status_row)
        self.persistence_detail_label = QLabel("")
        self.persistence_detail_label.setObjectName("loadTestingRunSubtitle")
        self.persistence_detail_label.setWordWrap(True)
        card.body.addWidget(self.persistence_detail_label)
        self.persistence_file_label = QLabel("")
        self.persistence_file_label.setObjectName("loadTestingRequestPath")
        card.body.addWidget(self.persistence_file_label)
        self.persistence_size_label = QLabel("")
        self.persistence_size_label.setObjectName("loadTestingRunSubtitle")
        card.body.addWidget(self.persistence_size_label)
        return card

    def _refresh_persistence_card(self) -> None:
        state = self._persist_state
        title, detail, color_name = _PERSIST_STATES.get(state, _PERSIST_STATES["off"])
        self.persistence_status_label.setText(title)
        self.persistence_detail_label.setText(detail)
        color = getattr(theme, color_name, theme.TEXT_MUTED)
        self.persistence_dot.setPixmap(icon("status-dot", color, 10).pixmap(10, 10))
        if state == "off":
            path = ""
        elif state == "ready":
            path = self.persist_path.text().strip() or self._default_persist_path()
        else:
            path = self._persist_file
        if not path:
            self.persistence_file_label.setText("")
            self.persistence_file_label.setToolTip("")
            self.persistence_size_label.setText("")
            return
        self.persistence_file_label.setText(Path(path).name)
        self.persistence_file_label.setToolTip(path)
        size = f"Size · {describe_store(path)}"
        if self._persist_run_id and state in ("saving", "persisted", "loaded"):
            size += f"\nRun {self._persist_run_id}"
        self.persistence_size_label.setText(size)

    def _set_persist_state(self, state: str, *, path: str = "", run_id: str = "") -> None:
        self._persist_state = state
        self._persist_file = path
        self._persist_run_id = run_id
        self._refresh_persistence_card()

    # ------------------------------------------------------------------ run lifecycle

    def _reset_live_metrics(self) -> None:
        self._error_entries = []
        self.run_errors_list.clear()
        self.errors_filter_summary_label.setText("")
        self.run_warnings_list.clear()
        self.outcome_chart.set_counts({})
        self.active_users_chart.clear()
        self.throughput_chart.clear()
        self.latency_chart.clear()
        self.recent_errors_table.setRowCount(0)
        self.endpoint_metrics_table.setRowCount(0)
        self.latency_summary_label.setText("min – / mean – / p50 – / p95 – / p99 – / max –")
        for key, card in self.stat_cards.items():
            card.set_value("–")
            card.set_subtitle("")
            if key in _LIVE_CARD_TITLES:
                card.set_title(_LIVE_CARD_TITLES[key])
                card.setToolTip("")
        self._snapshots = []
        self._final_snapshot = {}
        self.run_progress_bar.setRange(0, 1)
        self.run_progress_bar.setValue(0)
        self.progress_left_label.setText("Not run yet.")
        self.live_elapsed_label.setText("00:00")
        self.live_stage_label.setText("Stage —")
        self._set_live_status("READY", "ready")
        self.report_view.clear()
        self._report_findings = []
        self.results_tabs.setTabText(2, "Errors")
        self._refresh_export_enabled()

    def _start_run(self) -> None:
        plan = self._build_and_render_plan()
        if plan is None:
            return
        if not plan.is_executable:
            QMessageBox.warning(self, "Cannot start", "The current plan has blocking safety issues; check Pre-run summary.")
            return
        if not self.confirm_checkbox.isChecked():
            QMessageBox.information(
                self, "Confirmation required", "Confirm you understand this sends real requests before starting."
            )
            return

        if self._loaded_run is not None:
            self._exit_loaded_run_view()
            self._refresh_live_context(plan)
        self._reset_live_metrics()
        self.run_status_label.setText("Running...")
        self.run_status_label.setStyleSheet("")
        self._event_bus = EventBus()
        cancellation = CancellationController()

        store = None
        if self.persist_checkbox.isChecked():
            path = self.persist_path.text().strip() or self._default_persist_path()
            self.persist_path.setText(path)
            if self._run_store is not None:
                self._run_store.close()
            try:
                store = RunStore(path)
            except Exception as exc:  # noqa: BLE001 - an unusable file must not start a run silently
                QMessageBox.warning(self, "Cannot persist this run", f"{path} could not be opened as a run store:\n{exc}")
                self._run_store = None
                self.run_status_label.setText("Not started.")
                return
            self._run_store = store

        self.engine = LoadEngine(plan, store=store, event_bus=self._event_bus, cancellation=cancellation, options=LoadRunOptions())
        self._run_started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._run_finished_at = ""
        self._size_refresh_ticks = 0
        if store is not None:
            self._set_persist_state("saving", path=str(store.path), run_id=self.engine.run_id)
        else:
            self._set_persist_state("off")
        self.open_saved_run_button.setEnabled(False)
        self._refresh_export_enabled()

        self._thread = QThread()
        self._worker = LoadEngineWorker(self.engine)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._run_finished)
        self._worker.failed.connect(self._run_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.live_stop_button.setEnabled(True)
        self._event_timer.start()
        self.results_tabs.setCurrentIndex(0)
        self.steps.setCurrentIndex(3)
        self._set_live_status("RUNNING", "running")

    def _stop_run(self) -> None:
        if self.engine is not None:
            self.engine.cancellation.stop()
            self.stop_button.setEnabled(False)
            self.live_stop_button.setEnabled(False)
            self.run_status_label.setText("Stopping after in-flight requests finish...")
            self._set_live_status("STOPPING", "stopped")

    def _drain_events(self) -> None:
        if self._event_bus is not None:
            for event in self._event_bus.drain():
                if event.kind == "metric_snapshot":
                    self._snapshots.append(
                        {key: value for key, value in event.payload.items() if key != "endpoints"}
                    )
                    self._update_live_metrics(event.payload)
                elif event.kind == "warning":
                    self._append_warning(event.payload)
        self._refresh_errors_from_store()
        if self._persist_state == "saving":
            self._size_refresh_ticks += 1
            # The drain timer ticks every 200 ms; refresh the file size about every 5 s.
            if self._size_refresh_ticks % 25 == 0:
                self._refresh_persistence_card()

    def _update_live_metrics(self, payload: dict[str, Any]) -> None:
        completed = int(payload.get("completed", 0))
        passed = int(payload.get("passed", 0))
        failed = int(payload.get("failed", 0))
        errored = int(payload.get("errored", 0))
        cancelled = int(payload.get("cancelled", 0))
        active_users = int(payload.get("active_users", 0))
        throughput = float(payload.get("throughput_per_second", 0.0))
        error_rate = float(payload.get("error_rate", 0.0))
        p95 = float(payload.get("p95_ms", 0.0))
        p50 = float(payload.get("p50_ms", 0.0))
        p99 = float(payload.get("p99_ms", 0.0))

        self.outcome_chart.set_counts({"passed": passed, "failed": failed, "error": errored, "cancelled": cancelled})

        self.stat_cards["active_users"].set_value(f"{active_users:,}")
        self.stat_cards["completed"].set_value(f"{completed:,}")
        self.stat_cards["passed"].set_value(f"{passed:,}", color=theme.PASS if passed else None)
        if completed:
            self.stat_cards["passed"].set_subtitle(f"{passed / completed * 100:.0f}% success")
        failed_total = failed + errored
        self.stat_cards["failed"].set_value(f"{failed_total:,}", color=theme.FAIL if failed_total else None)
        if failed_total:
            self.stat_cards["failed"].set_subtitle(f"{failed} HTTP · {errored} network")
        self.stat_cards["throughput"].set_value(f"{throughput:.1f} req/s")
        self.stat_cards["error_rate"].set_value(f"{error_rate * 100:.1f}%", color=theme.FAIL if error_rate > 0 else None)
        self.stat_cards["latency"].set_value(f"{p95:.0f} ms")
        self.stat_cards["p99"].set_value(f"{p99:.0f} ms")
        self.stat_cards["latency"].set_subtitle(
            f"p50 {p50:.0f} / p99 {p99:.0f} ms"
        )

        self.active_users_chart.append(float(active_users))
        self.throughput_chart.append({"Completed": throughput, "Target": throughput})
        self.latency_chart.append({"P50": p50, "P95": p95, "P99": p99})
        self.latency_summary_label.setText(
            f"min {payload.get('min_ms', 0.0):.0f} / mean {payload.get('mean_ms', 0.0):.0f} / "
            f"p50 {payload.get('p50_ms', 0.0):.0f} / p95 {p95:.0f} / "
            f"p99 {payload.get('p99_ms', 0.0):.0f} / max {payload.get('max_ms', 0.0):.0f}"
        )

        elapsed = float(payload.get("elapsed_seconds", 0.0))
        total_duration = self._view_total_duration
        if total_duration > 0:
            self.run_progress_bar.setRange(0, int(total_duration))
            self.run_progress_bar.setValue(min(int(elapsed), int(total_duration)))
        self.progress_left_label.setText(f"{elapsed:.0f}s of {total_duration:.0f}s elapsed · {completed:,} requests completed")
        minutes, seconds = divmod(int(elapsed), 60)
        self.live_elapsed_label.setText(f"{minutes:02d}:{seconds:02d}")
        self._update_stage_progress(elapsed)
        endpoint_text = self._view_endpoint_text
        endpoint_values = (
            endpoint_text,
            f"{completed:,}",
            f"{passed:,}",
            f"{failed_total:,}",
            f"{p50:.0f} ms",
            f"{p95:.0f} ms",
            f"{p99:.0f} ms",
            f"{throughput:.1f} rps",
        )
        if self.endpoint_metrics_table.rowCount() == 0:
            self.endpoint_metrics_table.setRowCount(1)
        for column, value in enumerate(endpoint_values):
            self.endpoint_metrics_table.setItem(0, column, QTableWidgetItem(value))

    def _append_warning(self, payload: dict[str, Any]) -> None:
        severity = str(payload.get("severity", "warning"))
        message = str(payload.get("message", ""))
        item = QListWidgetItem(f"[{severity.upper()}] {message}")
        item.setForeground(QColor(theme.FAIL if severity == "error" else theme.WARN))
        self.run_warnings_list.addItem(item)

    def _refresh_errors_from_store(self) -> None:
        if self._run_store is None or self.engine is None:
            return
        self._error_entries = self._run_store.list_errors(self.engine.run_id)
        self._apply_errors_filter()
        self._render_recent_errors()

    def _apply_errors_filter(self, *_args: Any) -> None:
        search = self.errors_search_box.text().strip().lower()
        category_filter = self.errors_category_filter.currentText()
        self.run_errors_list.clear()
        shown = 0
        for entry in self._error_entries:
            if category_filter != "All categories" and entry["category"] != category_filter:
                continue
            text = f"[{entry['category']}] {entry['endpoint_id']}: {entry['message']} (x{entry['count']})"
            if search and search not in text.lower():
                continue
            self.run_errors_list.addItem(text)
            shown += 1
        total = len(self._error_entries)
        if search or category_filter != "All categories":
            self.errors_filter_summary_label.setText(f"Showing {shown} of {total} distinct errors")
        else:
            self.errors_filter_summary_label.setText(f"{total} distinct errors" if total else "No errors recorded.")
        if hasattr(self, "results_tabs"):
            self.results_tabs.setTabText(2, f"Errors ({total})" if total else "Errors")

    def _render_recent_errors(self) -> None:
        recent = self._error_entries[:8]
        self.recent_errors_table.setRowCount(len(recent))
        for row, entry in enumerate(recent):
            self.recent_errors_table.setItem(row, 0, QTableWidgetItem(str(entry["count"])))
            self.recent_errors_table.setItem(row, 1, QTableWidgetItem(str(entry["endpoint_id"])))
            self.recent_errors_table.setItem(row, 2, QTableWidgetItem(str(entry["category"])))
            self.recent_errors_table.setItem(row, 3, QTableWidgetItem(str(entry["message"])))

    def _render_final_report(self, summary: LoadRunSummary | None = None) -> None:
        """Fills the detailed Final report and switches the stat cards to min/avg/max."""
        data = self._report_data(summary)
        self._report_findings = diagnose(data)
        self.report_view.render(
            summary=summary,
            outcome=data.outcome,
            stats=data.stats,
            snapshots=self._snapshots,
            stages=data.stages,
            errors=self._error_entries,
            warnings=data.warnings,
            definition=data.definition,
            environment_name=data.environment_name,
            started_at=format_timestamp(data.started_at),
            finished_at=format_timestamp(data.finished_at),
            duration_seconds=data.duration_seconds,
            run_id=data.run_id,
            persisted_to=data.persisted_to,
            note=data.note,
            findings=self._report_findings,
        )
        self._apply_completed_cards(data.stats)
        self._refresh_export_enabled()

    def _report_data(self, summary: LoadRunSummary | None = None) -> LoadReportData:
        """Everything the Final report and the PDF need about the run on screen."""
        final = self._final_snapshot or (self._snapshots[-1] if self._snapshots else {})
        loaded = self._loaded_run
        note = ""
        if loaded is not None:
            definition = loaded.definition
            environment_name = str(loaded.run.get("environment_name") or "")
            timing_source = loaded.run
            run_id = loaded.run_id
            persisted_to = str(loaded.path)
            warnings = loaded.warnings
            outcome = loaded.outcome
            if loaded.reconstructed:
                note = "Metrics were rebuilt from request samples; this file predates saved snapshots."
            elif summary is None:
                note = "The run did not finish cleanly, so no final summary was saved."
        else:
            definition = self.engine.run_definition() if self.engine is not None else {}
            environment_name = self.plan.scenario.environment.environment_name if self.plan is not None else ""
            timing_source = (
                {"started_at": summary.started_at, "finished_at": summary.finished_at} if summary is not None else {}
            )
            run_id = summary.run_id if summary is not None else ""
            persisted_to = self._persist_file if self._persist_state == "persisted" else ""
            warnings = self.engine.warnings if self.engine is not None else []
            outcome = summary.outcome if summary is not None else "INCOMPLETE"
        return build_report_data(
            outcome=outcome,
            summary=summary,
            definition=definition,
            snapshots=self._snapshots,
            final_snapshot=final,
            errors=self._error_entries,
            warnings=warnings,
            environment_name=environment_name,
            run_id=run_id,
            started_at=timing_source.get("started_at"),
            finished_at=timing_source.get("finished_at"),
            duration_seconds=run_duration_seconds(timing_source),
            persisted_to=persisted_to,
            note=note,
            samples=self._cached_sample_aggregates(persisted_to, run_id),
        )

    def _cached_sample_aggregates(self, path: str, run_id: str) -> SampleAggregates | None:
        """SQL aggregates over a persisted run's samples, computed once per (file, run)."""
        if not path or not run_id:
            return None
        key = (path, run_id)
        if self._sample_cache_key != key:
            self._sample_cache_key = key
            self._sample_cache = load_sample_aggregates(path, run_id)
        return self._sample_cache

    # ------------------------------------------------------------------ report export

    def _report_available(self) -> bool:
        return self._thread is None and bool(self._snapshots) and (self._last_summary is not None or self._loaded_run is not None)

    def _refresh_export_enabled(self) -> None:
        if not hasattr(self, "report_export_button"):
            return
        available = self._report_available()
        if self._thread is not None:
            tip = "Available once the run finishes."
        elif available:
            tip = "Export the detailed report with charts and findings to PDF"
        else:
            tip = "Run a load test or open a saved run to export its report."
        self.report_export_button.setEnabled(available)
        self.report_export_button.setToolTip(tip)
        self.report_copy_button.setEnabled(available)
        if available:
            persisted = self._loaded_run is not None or self._persist_state == "persisted"
            self.report_actions_label.setText(
                "Full report: this run was persisted, so request samples are included."
                if persisted
                else "Reduced report: this run was not persisted, so per-request detail is not available."
            )
        else:
            self.report_actions_label.setText("")

    def report_summary_text(self) -> str:
        """Executive summary and findings of the run on screen, as plain text."""
        data = self._report_data(self._last_summary)
        findings = self._report_findings or diagnose(data)
        lines = [data.title, ""]
        lines.extend(f"- {sentence}" for _tone, sentence in executive_summary(data, findings))
        for finding in findings:
            lines.extend(["", f"{finding.ref} [{finding.severity}] {finding.title}"])
            lines.extend(f"  Evidence: {line}" for line in finding.evidence)
            lines.extend(f"  Cause: {line}" for line in finding.causes)
            lines.extend(f"  Fix: {line}" for line in finding.fixes)
        return "\n".join(lines)

    def _copy_report_summary(self) -> None:
        if not self._report_available():
            return
        QApplication.clipboard().setText(self.report_summary_text())
        self.report_actions_label.setText("Summary copied to the clipboard.")

    def export_report_pdf(self, path: str | Path, options: PdfExportOptions | None = None) -> PdfExportResult:
        """Writes the report of the run on screen to ``path``; raises when nothing can be exported."""
        if not self._report_available():
            raise RuntimeError("There is no finished or saved run to export.")
        data = self._report_data(self._last_summary)
        findings = diagnose(data)
        return export_pdf(data, findings, path, options or PdfExportOptions())

    def _default_export_path(self) -> str:
        data = self._report_data(self._last_summary)
        stem = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in (data.run_id or "load-run"))
        directory = self._last_export_dir
        if not directory:
            source = self._loaded_run.path if self._loaded_run is not None else (self._persist_file or "")
            directory = str(Path(source).parent) if source else str(Path.home())
        return str(Path(directory) / f"{stem}.pdf")

    def _export_pdf_clicked(self) -> None:
        if not self._report_available():
            return
        data = self._report_data(self._last_summary)
        dialog = PdfExportDialog(
            samples_available=data.samples_available,
            run_label=data.title,
            author=self._last_export_author,
            parent=self,
        )
        if dialog.exec() != PdfExportDialog.DialogCode.Accepted:
            return
        options = dialog.options()
        path, _filter = QFileDialog.getSaveFileName(self, "Export report to PDF", self._default_export_path(), "PDF files (*.pdf)")
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = export_pdf(data, diagnose(data), path, options)
        except Exception as exc:  # noqa: BLE001 - surface any rendering or file error to the user
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, "Export failed", f"The report could not be written to {path}:\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self._last_export_dir = str(Path(path).parent)
        self._last_export_author = options.author
        self.report_actions_label.setText(f"Exported {result.page_count} pages to {Path(path).name}.")
        self.report_actions_label.setToolTip(str(result.path))
        if dialog.open_after:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.path)))

    def _apply_completed_cards(self, stats: RunStatistics) -> None:
        """Completed-run cards: peaks and averages instead of the last interval's values."""
        final = stats.final
        completed = int(final.get("completed", 0) or 0)
        passed = int(final.get("passed", 0) or 0)
        failed = int(final.get("failed", 0) or 0)
        errored = int(final.get("errored", 0) or 0)
        cancelled = int(final.get("cancelled", 0) or 0)
        error_rate = float(final.get("error_rate", 0.0) or 0.0)
        cards = self.stat_cards
        self.outcome_chart.set_counts({"passed": passed, "failed": failed, "error": errored, "cancelled": cancelled})
        cards["completed"].set_value(f"{completed:,}")
        cards["error_rate"].set_value(f"{error_rate * 100:.1f}%", color=theme.FAIL if error_rate > 0 else None)
        cards["passed"].set_value(f"{passed:,}", color=theme.PASS if passed else None)
        failed_total = failed + errored
        cards["failed"].set_value(f"{failed_total:,}", color=theme.FAIL if failed_total else None)

        def complete(key: str, value: str, subtitle: str, tooltip: str) -> None:
            cards[key].set_title(_COMPLETED_CARD_TITLES[key])
            cards[key].set_value(value)
            cards[key].set_subtitle(subtitle)
            cards[key].setToolTip(tooltip)

        throughput = stats.throughput
        if throughput.available:
            complete(
                "throughput",
                f"{throughput.maximum:.1f} req/s",
                f"min {throughput.minimum:.1f} · avg {throughput.average:.1f}",
                "Peak, lowest and average completed requests per second while virtual users were active.",
            )
        latency = stats.latency_mean
        if latency.available:
            complete("latency", f"{latency.average:.0f} ms", "mean of all requests", "Mean latency over every request in the run.")
        p99 = stats.p99
        if p99.available:
            complete(
                "p99",
                f"{p99.average:.0f} ms",
                f"min {p99.minimum:.0f} · max {p99.maximum:.0f} ms",
                "Average of each interval's P99 latency, with the best and worst interval.",
            )
        users = stats.active_users
        if users.available:
            complete(
                "active_users",
                f"{users.maximum:.0f}",
                f"avg {users.average:.1f} · min {users.minimum:.0f}",
                "Peak, average and lowest scheduled virtual users while the run was active.",
            )

    def _run_finished(self, summary: LoadRunSummary) -> None:
        self._last_summary = summary
        self._drain_events()
        self._event_timer.stop()
        if self.engine is not None:
            self._final_snapshot = dict(self.engine.final_snapshot)
        if self._persist_state == "saving":
            self._set_persist_state("persisted", path=self._persist_file, run_id=self._persist_run_id)
            self._remember_run_file(self._persist_file)
        color = _OUTCOME_COLORS.get(summary.outcome, theme.TEXT)
        self.run_status_label.setText(
            f"{summary.outcome} — {summary.total_requests:,} requests, {summary.passed:,} passed, "
            f"{summary.failed:,} failed, {summary.errored:,} errored"
        )
        self.run_status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self._set_live_status(summary.outcome, summary.outcome.lower())
        self._render_final_report(summary)
        self._refresh_persist_size_label()
        self.results_tabs.setCurrentIndex(self.results_tabs.count() - 1)
        self.run_completed.emit(
            "Load test finished",
            f"{summary.outcome} — {summary.total_requests:,} requests, "
            f"{summary.failed:,} failed",
            summary.failed == 0 and summary.errored == 0,
        )

    def _run_failed(self, message: str) -> None:
        self._event_timer.stop()
        if self._persist_state == "saving":
            # Whatever was written before the failure stays in the file.
            self._set_persist_state("persisted", path=self._persist_file, run_id=self._persist_run_id)
            self._remember_run_file(self._persist_file)
        self.run_status_label.setText("Load test failed.")
        self.run_status_label.setStyleSheet(f"color: {theme.FAIL}; font-weight: 600;")
        self._set_live_status("FAILED", "fail")
        self.run_completed.emit("Load test failed", message.strip(), False)
        QMessageBox.warning(self, "Load Testing Studio failed", message)

    def _cleanup_thread(self) -> None:
        self.stop_button.setEnabled(False)
        self.live_stop_button.setEnabled(False)
        self.open_saved_run_button.setEnabled(True)
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        self._refresh_start_enabled()
        self._refresh_export_enabled()

    # ------------------------------------------------------------------ saved runs

    def _remember_run_file(self, path: str) -> None:
        if not path:
            return
        self._recent_run_files = [path, *(item for item in self._recent_run_files if item != path)][:6]

    def _populate_open_saved_run_menu(self) -> None:
        menu = self.open_saved_run_menu
        menu.clear()
        menu.setToolTipsVisible(True)
        browse = menu.addAction(icon("open", theme.TEXT, 16), "Open run file…")
        browse.triggered.connect(self._browse_saved_run)
        current = self.persist_path.text().strip() or self._default_persist_path()
        files: list[str] = []
        for path in (current, *self._recent_run_files):
            if path and path not in files and Path(path).is_file():
                files.append(path)
        if files:
            menu.addSeparator()
            heading = menu.addAction("Recent run files")
            heading.setEnabled(False)
            for path in files[:6]:
                action = menu.addAction(icon("folder", theme.TEXT, 16), f"{Path(path).name} · {describe_store(path)}")
                action.setToolTip(path)
                action.triggered.connect(lambda _checked=False, file_path=path: self.open_saved_run(file_path))

    def _browse_saved_run(self) -> None:
        if not self._can_open_saved_run():
            return
        start = self.persist_path.text().strip() or self._default_persist_path()
        path, _ = QFileDialog.getOpenFileName(
            self, "Open saved load run", start, "SQLite DB (*.db *.sqlite *.sqlite3);;All files (*)"
        )
        if path:
            self.open_saved_run(path, confirmed=True)

    def _can_open_saved_run(self) -> bool:
        if self._thread is not None:
            QMessageBox.information(self, "Load test running", "Stop the current load test before opening a saved run.")
            return False
        unsaved = self._last_summary is not None and self._loaded_run is None and self._persist_state != "persisted"
        if unsaved:
            answer = QMessageBox.question(
                self,
                "Replace results?",
                "The results on screen were not persisted and will be lost. Open a saved run anyway?",
            )
            return answer == QMessageBox.StandardButton.Yes
        return True

    def _choose_saved_run(self, path: str, runs: list[dict[str, Any]]) -> str | None:
        if len(runs) == 1:
            return str(runs[0]["run_id"])
        dialog = SavedRunPickerDialog(path, runs, self)
        if dialog.exec() != SavedRunPickerDialog.DialogCode.Accepted:
            return None
        return dialog.selected_run_id

    def open_saved_run(self, path: str | Path, run_id: str | None = None, *, confirmed: bool = False) -> bool:
        """Opens a persisted load run read-only on the Live & Results page."""
        if not confirmed and not self._can_open_saved_run():
            return False
        path = str(path)
        try:
            runs = list_saved_runs(path)
        except SavedRunError as exc:
            QMessageBox.warning(self, "Cannot open saved run", str(exc))
            return False
        if not runs:
            QMessageBox.information(self, "No saved load runs", f"{Path(path).name} does not contain any load runs.")
            return False
        if run_id is None:
            run_id = self._choose_saved_run(path, runs)
            if not run_id:
                return False
        try:
            loaded = load_saved_run(path, run_id)
        except SavedRunError as exc:
            QMessageBox.warning(self, "Cannot open saved run", str(exc))
            return False
        self.show_loaded_run(loaded)
        self._remember_run_file(path)
        return True

    def show_loaded_run(self, loaded: LoadedRun) -> None:
        self._loaded_run = loaded
        self._reset_live_metrics()
        definition = loaded.definition
        try:
            stages = tuple(LoadStage.from_dict(stage) for stage in definition.get("stages") or [])
        except (KeyError, TypeError, ValueError):
            stages = ()
        self._view_stages = stages
        self._view_total_duration = float(
            definition.get("total_duration_seconds") or sum(stage.duration_seconds for stage in stages)
        )
        method = str(definition.get("method") or "")
        endpoint_path = str(definition.get("path") or definition.get("endpoint_id") or "")
        self._view_endpoint_text = f"{method} {endpoint_path}".strip() or "—"

        name = definition.get("scenario_name") or loaded.run.get("name") or definition.get("endpoint_id") or loaded.run_id
        environment_name = loaded.run.get("environment_name") or "—"
        started = format_timestamp(loaded.run.get("started_at"))
        peak_users = int(definition.get("peak_users") or 0)
        self.live_title_label.setText(f"{name} — {environment_name}")
        self.live_subtitle_label.setText(f"Saved run · started {started} · {peak_users} peak virtual users")
        self.live_method_label.setText(method.upper() or "—")
        self.live_endpoint_label.setText(endpoint_path or "—")
        self.live_service_label.setText(str(definition.get("service") or "—"))
        think_time = max((stage.think_time_ms for stage in stages), default=0)
        limits = definition.get("limits") or {}
        self.live_workload_values["users"].setText(str(peak_users))
        self.live_workload_values["think_time"].setText(f"{think_time:,} ms")
        self.live_workload_values["duration"].setText(f"{self._view_total_duration:.0f}s")
        timeout = limits.get("request_timeout_seconds")
        self.live_workload_values["timeout"].setText(f"{float(timeout):g}s" if timeout is not None else "—")
        self._rebuild_stage_progress(stages)

        for snapshot in downsample(loaded.snapshots, _CHART_POINTS):
            self._update_live_metrics(self._replay_payload(snapshot))
        self._snapshots = list(loaded.snapshots)
        self._final_snapshot = dict(loaded.final_snapshot)

        self._error_entries = list(loaded.errors)
        self._apply_errors_filter()
        self._render_recent_errors()
        for warning in loaded.warnings:
            self._append_warning(warning)

        self._last_summary = loaded.summary
        self._set_live_status(loaded.outcome, loaded.outcome.lower())
        self._render_final_report(loaded.summary)
        self._set_persist_state("loaded", path=str(loaded.path), run_id=loaded.run_id)
        self.saved_run_chip_label.setText(f"Saved run · {started} · {loaded.path.name}")
        self.saved_run_chip.setToolTip(f"{loaded.path}\nRun {loaded.run_id}")
        self.saved_run_chip.show()
        self.steps.setCurrentIndex(3)
        self.results_tabs.setCurrentIndex(self.results_tabs.count() - 1)

    @staticmethod
    def _replay_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Snapshots rebuilt from samples have only interval values; chart those instead."""
        payload = dict(snapshot)
        for key in ("p50_ms", "p95_ms", "p99_ms"):
            payload.setdefault(key, snapshot.get(f"interval_{key}", 0.0))
        payload.setdefault("throughput_per_second", snapshot.get("interval_throughput", 0.0))
        return payload

    def _exit_loaded_run_view(self) -> None:
        self._loaded_run = None
        self.saved_run_chip.hide()
        self._set_persist_state("ready" if self.persist_checkbox.isChecked() else "off")

    def close_saved_run(self) -> None:
        """Leaves a reopened run and returns the page to the current plan."""
        if self._loaded_run is None:
            return
        self._exit_loaded_run_view()
        self._last_summary = None
        self._reset_live_metrics()
        if self.plan is not None:
            self._refresh_live_context(self.plan)
        else:
            self._view_stages = ()
            self._view_total_duration = 0.0
            self._view_endpoint_text = "—"
            self.live_title_label.setText("Load test not started")
            self.live_subtitle_label.setText("Build and confirm a plan to begin.")
            self.live_method_label.setText("—")
            self.live_endpoint_label.setText("Choose an endpoint")
            self.live_service_label.setText("No service selected")
            for value in self.live_workload_values.values():
                value.setText("—")
            self._rebuild_stage_progress(())
        self.results_tabs.setCurrentIndex(0)

    def _configure_clicked(self) -> None:
        if self._loaded_run is not None:
            choice = self._ask_reuse_scenario()
            if choice == "cancel":
                return
            if choice == "reuse":
                self.reuse_loaded_scenario()
        self.steps.setCurrentIndex(0)

    def _ask_reuse_scenario(self) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("Reuse this scenario?")
        box.setText("Load the saved run's endpoint, parameters, payload, stages, safety limits and thresholds into the editor?")
        box.setInformativeText("Header values are never saved with a run, so re-enter any that the request needs.")
        reuse = box.addButton("Reuse scenario", QMessageBox.ButtonRole.AcceptRole)
        configure = box.addButton("Just configure", QMessageBox.ButtonRole.NoRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(reuse)
        box.exec()
        clicked = box.clickedButton()
        if clicked is reuse:
            return "reuse"
        if clicked is configure:
            return "configure"
        return "cancel"

    def reuse_loaded_scenario(self) -> list[str]:
        """Copies the reopened run's scenario into the editor; returns any notes for the user."""
        if self._loaded_run is None:
            return []
        definition = self._loaded_run.definition
        notes: list[str] = []
        endpoint_id = definition.get("endpoint_id")
        endpoint = next(
            (item for service in self.catalog.services for item in service.endpoints if item.id == endpoint_id),
            None,
        )
        if endpoint is None or not self.load_request(endpoint, dict(definition.get("values") or {}), definition.get("payload")):
            notes.append(f"Endpoint {endpoint_id} is not in the current catalog, so the selected endpoint was kept.")

        stages = definition.get("stages") or []
        if stages:
            self.stages_table.setRowCount(0)
            for stage in stages:
                loaded_stage = LoadStage.from_dict(stage)
                self._add_stage_row(
                    kind=loaded_stage.kind,
                    duration_seconds=loaded_stage.duration_seconds,
                    start_users=loaded_stage.start_users,
                    end_users=loaded_stage.end_users,
                    think_time_ms=loaded_stage.think_time_ms,
                    label=loaded_stage.label,
                )
        if definition.get("limits"):
            self._apply_safety_limits(SafetyLimits.from_dict(definition["limits"]))
        self.thresholds_table.setRowCount(0)
        for threshold in definition.get("thresholds") or []:
            item = ThresholdDefinition.from_dict(threshold)
            self._add_threshold_row(metric=item.metric, operator=item.operator, target=item.target, label=item.label)
        self.scenario_name_edit.setText(str(definition.get("scenario_name") or ""))
        # The plan on the summary page described the previous scenario.
        self.plan = None
        self._refresh_start_enabled()
        if notes:
            QMessageBox.information(self, "Scenario reused with changes", "\n".join(notes))
        return notes

    def _apply_safety_limits(self, limits: SafetyLimits) -> None:
        self.environment_permits_checkbox.setChecked(limits.environment_permits_load_test)
        self.confirmed_write_checkbox.setChecked(limits.confirmed_write_endpoints)
        self.get_head_only_checkbox.setChecked(limits.get_head_only)
        self.max_concurrency_spin.setValue(limits.max_concurrency)
        self.max_duration_spin.setValue(limits.max_duration_seconds)
        self.max_requests_checkbox.setChecked(limits.max_total_requests is not None)
        if limits.max_total_requests is not None:
            self.max_requests_spin.setValue(limits.max_total_requests)
        self.request_timeout_spin.setValue(limits.request_timeout_seconds)
        self.error_rate_stop_checkbox.setChecked(limits.error_rate_stop_threshold is not None)
        if limits.error_rate_stop_threshold is not None:
            self.error_rate_stop_spin.setValue(limits.error_rate_stop_threshold * 100.0)
        self.latency_stop_checkbox.setChecked(limits.latency_stop_ms is not None)
        if limits.latency_stop_ms is not None:
            self.latency_stop_spin.setValue(limits.latency_stop_ms)
        self.auth_failure_stop_checkbox.setChecked(limits.auth_failure_stop)
