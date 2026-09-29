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
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QObject, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
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
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from api_tester import theme
from api_tester.catalog import Catalog, Endpoint
from api_tester.data_runner.charts import MultiSeriesLineChart, OutcomeBreakdownChart, SparklineChart
from api_tester.data_runner.dashboard_widgets import ContextCard, StatCard, WizardStepper
from api_tester.execution.cancellation import CancellationController
from api_tester.execution.errors import ERROR_CATEGORIES
from api_tester.execution.events import EventBus
from api_tester.execution.models import ExecutionEnvironmentSnapshot, RequestTemplate
from api_tester.execution.persistence import RunStore
from api_tester.icons import icon, solid_icon
from api_tester.seeding import refresh_payload, seed_parameter
from api_tester.viewers import JsonTextEdit
from api_tester.widgets import AccordionScrollArea, attach_table_empty_state

from .engine import LoadEngine, LoadRunOptions, LoadRunSummary
from .planner import LoadPlan, build_plan
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.endpoint_bar_widget = QWidget()
        endpoint_bar = QHBoxLayout(self.endpoint_bar_widget)
        endpoint_bar.setContentsMargins(0, 0, 0, 0)
        endpoint_bar.addWidget(QLabel("Service"))
        self.service_combo = QComboBox()
        self.service_combo.currentTextChanged.connect(self._service_changed)
        endpoint_bar.addWidget(self.service_combo)
        endpoint_bar.addWidget(QLabel("Endpoint"))
        self.endpoint_combo = QComboBox()
        self.endpoint_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.endpoint_combo.currentIndexChanged.connect(self._endpoint_changed)
        endpoint_bar.addWidget(self.endpoint_combo, 1)
        layout.addWidget(self.endpoint_bar_widget)

        self._step_labels = ["Scenario", "Safety && Thresholds", "Pre-run summary", "Live && Results"]
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
        tab_icons = ("chart", "api-explorer", "warning", "warning", "verify")
        for index, icon_name in enumerate(tab_icons):
            self.results_tabs.setTabIcon(index, icon(icon_name, theme.TEXT_MUTED, 15))

    def _stepper_clicked(self, index: int) -> None:
        self.steps.setCurrentIndex(index)

    def _step_changed(self, index: int) -> None:
        self.stepper.set_current(index)
        live_mode = index == 3
        self.endpoint_bar_widget.setVisible(not live_mode)
        self.stepper.setVisible(not live_mode)

    def _service_changed(self, service_name: str) -> None:
        service = next((s for s in self.catalog.services if s.name == service_name), None)
        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        if service is not None:
            for endpoint in service.endpoints:
                self.endpoint_combo.addItem(f"{endpoint.method} {endpoint.path}", endpoint.id)
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
        self._render_parameters_table()
        self._render_payload_editor()
        self._refresh_write_method_hint()

    # ------------------------------------------------------------------ 1. scenario page

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
        self.stages_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.stages_table.setObjectName("roomyEditorTable")
        self.stages_table.verticalHeader().setDefaultSectionSize(theme.ROOMY_ROW_HEIGHT)
        self.stages_table.setMinimumHeight(330)
        self.stages_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
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
        layout.addWidget(limits_box)

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
        thresholds_layout.addWidget(self.thresholds_table)
        layout.addWidget(thresholds_box, 1)

        return page

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

        persist_row = QHBoxLayout()
        self.persist_checkbox = QCheckBox("Persist this run (required for the Errors tab)")
        self.persist_checkbox.setChecked(True)
        persist_row.addWidget(self.persist_checkbox)
        self.persist_path = QLineEdit(self._default_persist_path())
        persist_row.addWidget(self.persist_path, 1)
        browse_button = QPushButton("Browse...")
        browse_button.clicked.connect(self._browse_persist_path)
        persist_row.addWidget(browse_button)
        layout.addLayout(persist_row)

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
            expected_status=self.current_endpoint.expected_status,
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
        scenario = plan.scenario
        endpoint = scenario.endpoint
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
        if self.plan is None:
            return
        remaining = elapsed_seconds
        current_index = len(self.plan.scenario.stages) - 1
        for index, stage in enumerate(self.plan.scenario.stages):
            if remaining >= stage.duration_seconds:
                fraction = 1.0
                remaining -= stage.duration_seconds
            else:
                fraction = max(0.0, remaining / stage.duration_seconds)
                current_index = index
                remaining = 0.0
            if index < len(self._stage_progress_bars):
                self._stage_progress_bars[index].setValue(int(fraction * 1000))
        if self.plan.scenario.stages:
            stage = self.plan.scenario.stages[current_index]
            self.live_stage_label.setText(
                f"Stage {current_index + 1} of {len(self.plan.scenario.stages)} · "
                f"{stage.label or stage.kind.replace('_', ' ').title()}"
            )

    def _set_live_status(self, text: str, status: str) -> None:
        self.live_status_label.setText(text)
        self.live_status_label.setProperty("status", status)
        status_color = {
            "running": theme.PASS,
            "pass": theme.PASS,
            "fail": theme.FAIL,
            "aborted": theme.FAIL,
            "stopped": theme.WARN,
            "inconclusive": theme.WARN,
        }.get(status, theme.TEXT_MUTED)
        self.live_status_icon.setPixmap(icon("status-dot", status_color, 12).pixmap(12, 12))
        self.live_status_label.style().unpolish(self.live_status_label)
        self.live_status_label.style().polish(self.live_status_label)

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
        self.live_status_icon = QLabel()
        self.live_status_icon.setFixedSize(12, 12)
        header_layout.addWidget(self.live_status_icon)
        self.live_status_label = QLabel("READY")
        self.live_status_label.setObjectName("loadTestingStatusPill")
        self.live_status_label.setProperty("status", "ready")
        header_layout.addWidget(self.live_status_label)
        self.configure_button = QPushButton("Configure")
        self.configure_button.setIcon(icon("settings", theme.TEXT, 16))
        self.configure_button.clicked.connect(lambda: self.steps.setCurrentIndex(0))
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
        for key, title in (
            ("throughput", "Throughput"),
            ("latency", "P95 latency"),
            ("p99", "P99 latency"),
            ("error_rate", "Error rate"),
            ("completed", "Completed"),
            ("active_users", "Active users"),
        ):
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
        self.endpoint_metrics_table = QTableWidget(1, 8)
        self.endpoint_metrics_table.setHorizontalHeaderLabels(
            ["Endpoint", "Requests", "Passed", "Failed", "P50", "P95", "P99", "Throughput"]
        )
        self.endpoint_metrics_table.verticalHeader().setVisible(False)
        self.endpoint_metrics_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
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
        tab = QWidget()
        layout = QVBoxLayout(tab)

        form = QFormLayout()
        self.report_outcome_label = QLabel("–")
        form.addRow("Outcome", self.report_outcome_label)
        self.report_stop_reason_label = QLabel("")
        self.report_stop_reason_label.setWordWrap(True)
        form.addRow("Stop reason", self.report_stop_reason_label)
        self.report_totals_label = QLabel("–")
        form.addRow("Totals", self.report_totals_label)
        layout.addLayout(form)

        self.thresholds_result_table = QTableWidget(0, 5)
        self.thresholds_result_table.setHorizontalHeaderLabels(["Metric", "Operator", "Target", "Observed", "Result"])
        self.thresholds_result_table.verticalHeader().setVisible(False)
        self.thresholds_result_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.thresholds_result_table, 1)
        attach_table_empty_state(
            self.thresholds_result_table,
            icon_name="chart",
            title="No thresholds evaluated",
            guidance="Add pass/fail thresholds on the Thresholds step, then run the plan to see how each one scored.",
        )
        return tab

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
        self.endpoint_metrics_table.setRowCount(1)
        for column in range(self.endpoint_metrics_table.columnCount()):
            self.endpoint_metrics_table.setItem(0, column, QTableWidgetItem("—"))
        self.latency_summary_label.setText("min – / mean – / p50 – / p95 – / p99 – / max –")
        for card in self.stat_cards.values():
            card.set_value("–")
            card.set_subtitle("")
        self.run_progress_bar.setRange(0, 1)
        self.run_progress_bar.setValue(0)
        self.progress_left_label.setText("Not run yet.")
        self.live_elapsed_label.setText("00:00")
        self.live_stage_label.setText("Stage —")
        self._set_live_status("READY", "ready")
        self.report_outcome_label.setText("–")
        self.report_outcome_label.setStyleSheet("")
        self.report_stop_reason_label.setText("")
        self.report_totals_label.setText("–")
        self.thresholds_result_table.setRowCount(0)

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
            store = RunStore(path)
            self._run_store = store

        self.engine = LoadEngine(plan, store=store, event_bus=self._event_bus, cancellation=cancellation, options=LoadRunOptions())

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
                    self._update_live_metrics(event.payload)
                elif event.kind == "warning":
                    self._append_warning(event.payload)
        self._refresh_errors_from_store()

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
        total_duration = self.plan.total_duration_seconds if self.plan is not None else 0.0
        if total_duration > 0:
            self.run_progress_bar.setRange(0, int(total_duration))
            self.run_progress_bar.setValue(min(int(elapsed), int(total_duration)))
        self.progress_left_label.setText(f"{elapsed:.0f}s of {total_duration:.0f}s elapsed · {completed:,} requests completed")
        minutes, seconds = divmod(int(elapsed), 60)
        self.live_elapsed_label.setText(f"{minutes:02d}:{seconds:02d}")
        self._update_stage_progress(elapsed)
        endpoint_text = (
            f"{self.current_endpoint.method} {self.current_endpoint.path}" if self.current_endpoint is not None else "—"
        )
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

    def _render_final_report(self, summary: LoadRunSummary) -> None:
        self.report_outcome_label.setText(summary.outcome)
        color = _OUTCOME_COLORS.get(summary.outcome, theme.TEXT)
        self.report_outcome_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self.report_stop_reason_label.setText(summary.stop_reason or "(scenario completed without a stop condition)")
        self.report_totals_label.setText(
            f"{summary.total_requests:,} requests · {summary.passed:,} passed · {summary.failed:,} failed · "
            f"{summary.errored:,} errored · peak {summary.peak_users} users"
        )

        self.thresholds_result_table.setRowCount(len(summary.threshold_results))
        for row, result in enumerate(summary.threshold_results):
            definition = result.definition
            self.thresholds_result_table.setItem(row, 0, QTableWidgetItem(definition.label or definition.metric))
            self.thresholds_result_table.setItem(row, 1, QTableWidgetItem(definition.operator))
            self.thresholds_result_table.setItem(row, 2, QTableWidgetItem(f"{definition.target:g}"))
            self.thresholds_result_table.setItem(row, 3, QTableWidgetItem(f"{result.observed_value:g}"))
            result_item = QTableWidgetItem("PASS" if result.passed else "FAIL")
            result_item.setForeground(QColor(theme.PASS if result.passed else theme.FAIL))
            self.thresholds_result_table.setItem(row, 4, result_item)

    def _run_finished(self, summary: LoadRunSummary) -> None:
        self._last_summary = summary
        self._drain_events()
        self._event_timer.stop()
        color = _OUTCOME_COLORS.get(summary.outcome, theme.TEXT)
        self.run_status_label.setText(
            f"{summary.outcome} — {summary.total_requests:,} requests, {summary.passed:,} passed, "
            f"{summary.failed:,} failed, {summary.errored:,} errored"
        )
        self.run_status_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        self._set_live_status(summary.outcome, summary.outcome.lower())
        self._render_final_report(summary)
        self.results_tabs.setCurrentIndex(self.results_tabs.count() - 1)

    def _run_failed(self, message: str) -> None:
        self._event_timer.stop()
        self.run_status_label.setText("Load test failed.")
        self.run_status_label.setStyleSheet(f"color: {theme.FAIL}; font-weight: 600;")
        self._set_live_status("FAILED", "fail")
        QMessageBox.warning(self, "Load Testing Studio failed", message)

    def _cleanup_thread(self) -> None:
        self.stop_button.setEnabled(False)
        self.live_stop_button.setEnabled(False)
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        self._refresh_start_enabled()
