"""Data Runner UI: source -> mapping -> validate -> run/results wizard.

See ``platform architecture guide: load-testing-and-data-runner.md``
sections 8-11 for the design this implements. This widget is intentionally
dependency-injected (a :class:`~api_tester.catalog.Catalog` and an
``environment_provider`` callable) so it can be unit tested without the rest
of :mod:`api_tester.main`, and wired into the main shell with a couple of
lines that supply the app's live catalog/environment state.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable

from PyQt6.QtCore import QObject, Qt, QSize, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from api_tester import theme
from api_tester.catalog import Catalog, Endpoint
from api_tester.execution.cancellation import CancellationController
from api_tester.execution.events import EventBus
from api_tester.execution.metrics import LatencySketch
from api_tester.execution.models import ExecutionEnvironmentSnapshot
from api_tester.execution.persistence import RunStore
from api_tester.icons import icon, solid_icon
from api_tester.viewers import JsonTextEdit
from api_tester.widgets import attach_table_empty_state

from .charts import LabeledBarChart, MultiSeriesLineChart, OutcomeBreakdownChart, ScatterChart, SparklineChart
from .csv_source import ColumnProfile, CsvImportSettings, CsvPreview, CsvSource
from .dashboard_widgets import ContextCard, StatCard, WizardStepper
from .mapping import ColumnMapping, MappingTarget, Transform, mapping_targets
from .planner import DataRunPlan, PlanIssue, build_plan
from .runner import DataRunner, DataRunnerOptions, RunSummary

_ENCODING_CHOICES = ("Auto", "utf-8", "utf-8-sig", "utf-16", "cp1252")
_DELIMITER_CHOICES = (("Auto", ""), ("Comma (,)", ","), ("Semicolon (;)", ";"), ("Tab", "\t"), ("Pipe (|)", "|"))

_OUTCOME_COLORS = {
    "passed": "#16a34a",
    "failed": "#dc2626",
    "error": "#dc2626",
    "invalid": "#d97706",
    "skipped": "#65758b",
    "cancelled": "#65758b",
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


@dataclass
class _MappingRowWidgets:
    column: QComboBox
    target: QComboBox
    transforms_label: QLabel
    edit_button: QPushButton
    remove_button: QPushButton
    transforms: tuple[Transform, ...] = ()


class TransformEditorDialog(QDialog):
    """Configures the closed set of transforms for one column mapping.

    Options are grouped by mutually exclusive concern (type coercion, empty
    handling) rather than exposed as raw, independently orderable transform
    objects — this keeps the dialog approachable while still producing the
    exact ordered ``Transform`` list ``apply_transforms`` expects.
    """

    def __init__(self, target: MappingTarget, transforms: tuple[Transform, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Transforms for {target.label}")
        self._target = target
        layout = QVBoxLayout(self)

        self.trim = QCheckBox("Trim whitespace")
        self.required = QCheckBox("Required")
        self.required.setToolTip("Rows whose value for this field is empty are reported as invalid.")
        layout.addWidget(self.trim)
        layout.addWidget(self.required)

        form = QFormLayout()
        self.default_value = QLineEdit()
        form.addRow("Default when empty:", self.default_value)
        self.env_variable = QLineEdit()
        form.addRow("Environment variable fallback:", self.env_variable)

        self.type_coercion = QComboBox()
        self.type_coercion.addItems(["None", "Integer", "Number", "Boolean"])
        form.addRow("Convert to:", self.type_coercion)

        self.date_format = QLineEdit()
        self.date_format.setPlaceholderText("e.g. %Y-%m-%d")
        form.addRow("Parse as date (format):", self.date_format)

        self.split_delimiter = QLineEdit()
        self.split_delimiter.setPlaceholderText("e.g. | (leave blank to disable)")
        form.addRow("Split into a list on:", self.split_delimiter)

        self.prefix = QLineEdit()
        form.addRow("Prefix:", self.prefix)
        self.suffix = QLineEdit()
        form.addRow("Suffix:", self.suffix)

        self.empty_handling = QComboBox()
        self.empty_handling.addItems(["Keep as-is", "Set to null", "Omit field"])
        form.addRow("When resolved value is empty:", self.empty_handling)

        layout.addLayout(form)

        # The allowed values go in the tooltip, not the label: QCheckBox cannot
        # word-wrap, so inlining an enum would set an unshrinkable dialog width.
        self.enum_validate = QCheckBox("Validate against allowed values")
        self.enum_validate.setEnabled(bool(target.allowed_values))
        if target.allowed_values:
            self.enum_validate.setToolTip(
                "Allowed: " + ", ".join(target.allowed_values)
            )
        else:
            self.enum_validate.setToolTip(
                "This field has no allowed-value list in the catalog."
            )
        layout.addWidget(self.enum_validate)

        self.json_parse = QCheckBox("Parse value as JSON")
        layout.addWidget(self.json_parse)

        self.seed_when_empty = QCheckBox("Seed a sample value when empty")
        self.seed_when_empty.setToolTip(
            "Applied by the runner at send time; the preview above still shows the blank value."
        )
        layout.addWidget(self.seed_when_empty)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load(transforms)

    def _load(self, transforms: tuple[Transform, ...]) -> None:
        for transform in transforms:
            if transform.kind == "trim":
                self.trim.setChecked(True)
            elif transform.kind == "required":
                self.required.setChecked(True)
            elif transform.kind == "default":
                self.default_value.setText(str(transform.options.get("default", "")))
            elif transform.kind == "env_fallback":
                self.env_variable.setText(str(transform.options.get("variable", "")))
            elif transform.kind == "to_integer":
                self.type_coercion.setCurrentText("Integer")
            elif transform.kind == "to_number":
                self.type_coercion.setCurrentText("Number")
            elif transform.kind == "to_boolean":
                self.type_coercion.setCurrentText("Boolean")
            elif transform.kind == "date_format":
                self.date_format.setText(str(transform.options.get("format", "")))
            elif transform.kind == "split_list":
                self.split_delimiter.setText(str(transform.options.get("delimiter", "")))
            elif transform.kind == "prefix_suffix":
                self.prefix.setText(str(transform.options.get("prefix", "")))
                self.suffix.setText(str(transform.options.get("suffix", "")))
            elif transform.kind == "empty_is_null":
                self.empty_handling.setCurrentText("Set to null")
            elif transform.kind == "empty_is_omitted":
                self.empty_handling.setCurrentText("Omit field")
            elif transform.kind == "enum_validate":
                self.enum_validate.setChecked(True)
            elif transform.kind == "json_parse":
                self.json_parse.setChecked(True)
            elif transform.kind == "seed_when_empty":
                self.seed_when_empty.setChecked(True)

    def transforms(self) -> tuple[Transform, ...]:
        result: list[Transform] = []
        if self.trim.isChecked():
            result.append(Transform("trim"))
        if self.required.isChecked():
            result.append(Transform("required"))
        if self.default_value.text().strip():
            result.append(Transform("default", {"default": self.default_value.text()}))
        if self.env_variable.text().strip():
            result.append(Transform("env_fallback", {"variable": self.env_variable.text().strip()}))
        coercion = self.type_coercion.currentText()
        if coercion == "Integer":
            result.append(Transform("to_integer"))
        elif coercion == "Number":
            result.append(Transform("to_number"))
        elif coercion == "Boolean":
            result.append(Transform("to_boolean"))
        if self.date_format.text().strip():
            result.append(Transform("date_format", {"format": self.date_format.text().strip()}))
        if self.enum_validate.isChecked():
            result.append(Transform("enum_validate"))
        if self.json_parse.isChecked():
            result.append(Transform("json_parse"))
        if self.split_delimiter.text().strip():
            result.append(Transform("split_list", {"delimiter": self.split_delimiter.text()}))
        if self.prefix.text() or self.suffix.text():
            result.append(Transform("prefix_suffix", {"prefix": self.prefix.text(), "suffix": self.suffix.text()}))
        empty_choice = self.empty_handling.currentText()
        if empty_choice == "Set to null":
            result.append(Transform("empty_is_null"))
        elif empty_choice == "Omit field":
            result.append(Transform("empty_is_omitted"))
        if self.seed_when_empty.isChecked():
            result.append(Transform("seed_when_empty"))
        return tuple(result)


def _transforms_summary(transforms: tuple[Transform, ...]) -> str:
    if not transforms:
        return "(none)"
    return ", ".join(t.kind for t in transforms)


class DataRunnerWorker(QObject):
    """Runs a :class:`DataRunner` off the UI thread."""

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, runner: DataRunner) -> None:
        super().__init__()
        self._runner = runner

    def run(self) -> None:
        try:
            summary = self._runner.run()
            self.finished.emit(summary)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
            self.failed.emit(str(exc))


class DataRunnerTab(QWidget):
    """The Data Runner workspace tab: source, mapping, validate, run/results."""

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
        self.current_targets: list[MappingTarget] = []
        self.csv_source: CsvSource | None = None
        self.preview: CsvPreview | None = None
        self.plan: DataRunPlan | None = None
        self.runner: DataRunner | None = None
        self._thread: QThread | None = None
        self._worker: DataRunnerWorker | None = None
        self._event_bus: EventBus | None = None
        self._event_timer = QTimer(self)
        self._event_timer.setInterval(200)
        self._event_timer.timeout.connect(self._drain_events)
        self._paused = False
        self._run_store: RunStore | None = None
        self._history_store: RunStore | None = None
        self._latency_sketch = LatencySketch()
        self._outcome_counts: dict[str, int] = {}
        self._throughput_buckets: dict[int, int] = {}
        self._status_code_counts: dict[str, int] = {}
        self._error_category_counts: dict[str, int] = {}
        self._cumulative_completed = 0
        self._run_started_monotonic: float | None = None
        # Row-number -> detail payload for the Live results master-detail
        # inspector (see DataRunner._build_executed_row_detail).
        self._row_details: dict[int, dict[str, Any]] = {}
        # Set while a History run's samples are loaded into the results
        # table, so the row inspector can lazily fetch persisted detail
        # payloads (row_details table) instead of preloading all of them.
        self._loaded_history_run_id: str | None = None
        # Lightweight per-row summaries backing the paginated Live results
        # table (row_number/outcome/status/duration/error only — full detail
        # stays in self._row_details, fetched/cached per row on demand).
        self._result_rows: list[dict[str, Any]] = []
        self._results_page_size = 500
        self._current_results_page = 0
        # True while the Live results table should auto-scroll to show the
        # newest page as a run executes; set False once the user manually
        # navigates to an earlier page.
        self._follow_latest_page = True
        # Unfiltered backing list for the Errors tab; the QListWidget only
        # ever shows the subset matching the current search/category filter.
        self._error_entries: list[dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        endpoint_bar = QHBoxLayout()
        endpoint_bar.addWidget(QLabel("Service"))
        self.service_combo = QComboBox()
        self.service_combo.currentTextChanged.connect(self._service_changed)
        endpoint_bar.addWidget(self.service_combo)
        endpoint_bar.addWidget(QLabel("Endpoint"))
        self.endpoint_combo = QComboBox()
        self.endpoint_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.endpoint_combo.currentIndexChanged.connect(self._endpoint_changed)
        endpoint_bar.addWidget(self.endpoint_combo, 1)
        layout.addLayout(endpoint_bar)

        self._step_labels = ["Source", "Mapping", "Validate", "Execute & review", "History"]
        self.stepper = WizardStepper(self._step_labels)
        self.stepper.stepClicked.connect(self._stepper_clicked)
        layout.addWidget(self.stepper)

        self.steps = QTabWidget()
        self.steps.tabBar().hide()
        self.steps.addTab(self._build_source_page(), "1. Source")
        self.steps.addTab(self._build_mapping_page(), "2. Mapping")
        self.steps.addTab(self._build_validate_page(), "3. Validate")
        self.steps.addTab(self._build_run_page(), "4. Run && Results")
        self.steps.addTab(self._build_history_page(), "5. History")
        self.steps.currentChanged.connect(self._step_page_changed)
        layout.addWidget(self.steps, 1)

        self.refresh_catalog(catalog)
        self._refresh_stepper_progress()

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

    def _stepper_clicked(self, index: int) -> None:
        self.steps.setCurrentIndex(index)

    def _step_page_changed(self, index: int) -> None:
        self.stepper.set_current(index)
        self._refresh_stepper_progress()
        if index == 3:  # Execute & review
            self._refresh_run_context_panel()

    def _refresh_stepper_progress(self) -> None:
        """Marks a step complete once the artifact it produces exists, so the
        stepper reflects real wizard progress rather than a static counter."""
        completed: set[int] = set()
        if self.csv_source is not None and self.preview is not None:
            completed.add(0)
        if self._current_mappings():
            completed.add(1)
        if self.plan is not None and self.plan.is_executable:
            completed.add(2)
        if self.runner is not None and self.runner.row_outcomes:
            completed.add(3)
        self.stepper.set_completed(completed)

    def refresh_theme(self) -> None:
        for button, name in self._icon_buttons():
            _paint_icon(button, name)

    def _icon_buttons(self) -> list[tuple[QPushButton, str]]:
        pairs = [
            (self.browse_button, "folder"),
            (self.load_preview_button, "renew"),
            (self.add_mapping_button, "add"),
            (self.remove_mapping_button, "trash"),
            (self.auto_map_button, "wand"),
            (self.validate_button, "verify"),
            (self.run_button, "play"),
            (self.stop_button, "stop"),
            (self.export_button, "export"),
            (self.persist_browse_button, "folder"),
            (self.history_browse_button, "folder"),
            (self.history_refresh_button, "renew"),
            (self.history_load_button, "open"),
            (self.history_export_button, "export"),
        ]
        pairs.append((self.pause_button, "pause" if not self._paused else "play"))
        return pairs

    # ------------------------------------------------------------------ source

    def _build_source_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        path_row = QHBoxLayout()
        self.csv_path = QLineEdit()
        self.csv_path.setPlaceholderText("Path to a CSV file...")
        path_row.addWidget(self.csv_path, 1)
        self.browse_button = _icon_button("folder", "Browse for a CSV file")
        self.browse_button.clicked.connect(self._browse_csv)
        path_row.addWidget(self.browse_button)
        layout.addLayout(path_row)

        options_group = QGroupBox("Import settings")
        options_form = QFormLayout(options_group)
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItems(_ENCODING_CHOICES)
        options_form.addRow("Encoding", self.encoding_combo)
        self.delimiter_combo = QComboBox()
        for label, _value in _DELIMITER_CHOICES:
            self.delimiter_combo.addItem(label)
        options_form.addRow("Delimiter", self.delimiter_combo)
        self.quotechar_edit = QLineEdit('"')
        self.quotechar_edit.setMaxLength(1)
        options_form.addRow("Quote character", self.quotechar_edit)
        self.has_header_checkbox = QCheckBox("First row is a header")
        self.has_header_checkbox.setChecked(True)
        options_form.addRow("", self.has_header_checkbox)
        self.skip_blank_checkbox = QCheckBox("Skip blank rows")
        self.skip_blank_checkbox.setChecked(True)
        options_form.addRow("", self.skip_blank_checkbox)
        self.start_row_spin = QSpinBox()
        self.start_row_spin.setRange(0, 1_000_000)
        options_form.addRow("Start at data row", self.start_row_spin)
        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(0, 1_000_000)
        self.max_rows_spin.setSpecialValueText("All rows")
        options_form.addRow("Max rows (0 = all)", self.max_rows_spin)
        layout.addWidget(options_group)

        preview_row = QHBoxLayout()
        self.load_preview_button = _icon_button(
            "renew", "Load / refresh CSV preview", text="Load preview"
        )
        self.load_preview_button.clicked.connect(self._load_preview)
        preview_row.addWidget(self.load_preview_button)
        self.preview_summary_label = QLabel("No CSV loaded yet.")
        preview_row.addWidget(self.preview_summary_label, 1)
        layout.addLayout(preview_row)

        splitter = QSplitter()
        self.column_profile_table = QTableWidget(0, 4)
        self.column_profile_table.setHorizontalHeaderLabels(["Column", "Inferred type", "Blanks", "Sample values"])
        self.column_profile_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.column_profile_table.horizontalHeader().setStretchLastSection(True)
        attach_table_empty_state(
            self.column_profile_table,
            icon_name="chart",
            title="No CSV loaded",
            guidance="Choose a CSV file above, then load the preview to profile its columns.",
        )
        splitter.addWidget(self.column_profile_table)

        self.sample_rows_table = QTableWidget(0, 0)
        self.sample_rows_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        attach_table_empty_state(
            self.sample_rows_table,
            icon_name="eye",
            title="No sample rows",
            guidance="The first rows of your CSV appear here once a preview is loaded.",
        )
        splitter.addWidget(self.sample_rows_table)
        layout.addWidget(splitter, 1)

        return page

    def _browse_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose CSV file", "", "CSV files (*.csv);;All files (*)")
        if path:
            self.csv_path.setText(path)

    def _current_import_settings(self) -> CsvImportSettings:
        delimiter = ""
        for index, (_label, value) in enumerate(_DELIMITER_CHOICES):
            if index == self.delimiter_combo.currentIndex():
                delimiter = value
                break
        encoding = self.encoding_combo.currentText()
        max_rows = self.max_rows_spin.value()
        return CsvImportSettings(
            path=self.csv_path.text().strip(),
            encoding="" if encoding == "Auto" else encoding,
            delimiter=delimiter,
            quotechar=self.quotechar_edit.text() or '"',
            has_header=self.has_header_checkbox.isChecked(),
            start_row=self.start_row_spin.value(),
            max_rows=None if max_rows == 0 else max_rows,
            skip_blank_rows=self.skip_blank_checkbox.isChecked(),
        )

    def _load_preview(self) -> None:
        settings = self._current_import_settings()
        if not settings.path:
            QMessageBox.information(self, "Choose a CSV file", "Enter or browse for a CSV file first.")
            return
        self.csv_source = CsvSource(settings)
        try:
            self.preview = self.csv_source.preview()
        except OSError as exc:
            QMessageBox.warning(self, "Could not read CSV", str(exc))
            self.csv_source = None
            self.preview = None
            return
        self._render_preview(self.preview)
        self._refresh_mapping_column_choices()
        self._refresh_stepper_progress()

    def _render_preview(self, preview: CsvPreview) -> None:
        self.preview_summary_label.setText(
            f"{len(preview.columns)} columns · sampled {preview.total_rows_sampled} rows · "
            f"{preview.blank_row_count} blank · {preview.duplicate_row_count} duplicate"
        )
        self.column_profile_table.setRowCount(len(preview.column_profiles))
        for row, profile in enumerate(preview.column_profiles):
            self._set_profile_row(row, profile)

        self.sample_rows_table.setColumnCount(len(preview.columns))
        self.sample_rows_table.setHorizontalHeaderLabels(list(preview.columns))
        self.sample_rows_table.setRowCount(len(preview.rows))
        for row_index, row in enumerate(preview.rows):
            for column_index, column in enumerate(preview.columns):
                self.sample_rows_table.setItem(row_index, column_index, QTableWidgetItem(row.get(column, "")))

    def _set_profile_row(self, row: int, profile: ColumnProfile) -> None:
        self.column_profile_table.setItem(row, 0, QTableWidgetItem(profile.name))
        self.column_profile_table.setItem(row, 1, QTableWidgetItem(profile.inferred_type))
        self.column_profile_table.setItem(row, 2, QTableWidgetItem(str(profile.blank_count)))
        self.column_profile_table.setItem(row, 3, QTableWidgetItem(", ".join(profile.sample_values)))

    # ------------------------------------------------------------------ mapping

    def _build_mapping_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        defaults_row = QHBoxLayout()
        defaults_row.addWidget(QLabel("Default expected status"))
        self.default_expected_status = QLineEdit("200-299")
        self.default_expected_status.setMaximumWidth(140)
        defaults_row.addWidget(self.default_expected_status)
        defaults_row.addStretch()
        layout.addLayout(defaults_row)

        self.mapping_table = QTableWidget(0, 5)
        self.mapping_table.setHorizontalHeaderLabels(["CSV column", "Maps to", "Transforms", "", ""])
        header = self.mapping_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.mapping_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mapping_table.setObjectName("roomyEditorTable")
        self.mapping_table.verticalHeader().setDefaultSectionSize(theme.ROOMY_ROW_HEIGHT)
        layout.addWidget(self.mapping_table, 1)

        buttons_row = QHBoxLayout()
        self.add_mapping_button = _icon_button("add", "Add a mapping row")
        self.add_mapping_button.clicked.connect(lambda: self._add_mapping_row())
        buttons_row.addWidget(self.add_mapping_button)
        self.remove_mapping_button = _icon_button("trash", "Remove the selected mapping row", danger=True)
        self.remove_mapping_button.clicked.connect(self._remove_selected_mapping_row)
        buttons_row.addWidget(self.remove_mapping_button)
        self.auto_map_button = _icon_button("wand", "Auto-map CSV columns to targets by matching name")
        self.auto_map_button.clicked.connect(self._auto_map)
        buttons_row.addWidget(self.auto_map_button)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)

        attach_table_empty_state(
            self.mapping_table,
            icon_name="wand",
            title="No column mappings",
            guidance="Match CSV columns to request fields. Auto-map pairs them by name.",
            action_text="Auto-map columns",
            action_callback=self._auto_map,
        )

        return page

    def _target_label_for_key(self, key: str) -> str:
        for target in self.current_targets:
            if target.key == key:
                return target.label
        return key

    def _populate_target_combo(self, combo: QComboBox, selected_key: str | None = None) -> None:
        combo.blockSignals(True)
        combo.clear()
        for target in self.current_targets:
            combo.addItem(target.label, target.key)
        if selected_key is not None:
            index = combo.findData(selected_key)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _add_mapping_row(self, column_name: str = "", target_key: str | None = None, transforms: tuple[Transform, ...] = ()) -> None:
        row = self.mapping_table.rowCount()
        self.mapping_table.insertRow(row)

        column_combo = QComboBox()
        column_combo.setEditable(True)
        if self.preview is not None:
            column_combo.addItems(list(self.preview.columns))
        if column_name:
            column_combo.setCurrentText(column_name)
        self.mapping_table.setCellWidget(row, 0, column_combo)

        target_combo = QComboBox()
        self._populate_target_combo(target_combo, target_key)
        self.mapping_table.setCellWidget(row, 1, target_combo)

        transforms_label = QLabel(_transforms_summary(transforms))
        self.mapping_table.setCellWidget(row, 2, transforms_label)

        edit_button = _icon_button("edit", "Configure transforms for this mapping")
        remove_button = _icon_button("trash", "Remove this mapping row", danger=True)

        widgets = _MappingRowWidgets(column_combo, target_combo, transforms_label, edit_button, remove_button, transforms)
        edit_button.clicked.connect(lambda _checked=False, r=row: self._edit_transforms(r))
        remove_button.clicked.connect(lambda _checked=False, r=row: self._remove_mapping_row(r))
        self.mapping_table.setCellWidget(row, 3, edit_button)
        self.mapping_table.setCellWidget(row, 4, remove_button)
        self._mapping_row_widgets_for(row, widgets)

    def _mapping_row_widgets_for(self, row: int, widgets: _MappingRowWidgets | None = None) -> _MappingRowWidgets:
        if not hasattr(self, "_mapping_rows"):
            self._mapping_rows: dict[int, _MappingRowWidgets] = {}
        if widgets is not None:
            self._mapping_rows[row] = widgets
        return self._mapping_rows[row]

    def _edit_transforms(self, row: int) -> None:
        widgets = self._mapping_row_widgets_for(row)
        target_key = widgets.target.currentData()
        target = next((t for t in self.current_targets if t.key == target_key), None)
        if target is None:
            return
        dialog = TransformEditorDialog(target, widgets.transforms, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            widgets.transforms = dialog.transforms()
            widgets.transforms_label.setText(_transforms_summary(widgets.transforms))

    def _remove_mapping_row(self, row: int) -> None:
        self.mapping_table.removeRow(row)
        self._reindex_mapping_rows()

    def _remove_selected_mapping_row(self) -> None:
        rows = sorted({index.row() for index in self.mapping_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.mapping_table.removeRow(row)
        self._reindex_mapping_rows()

    def _reindex_mapping_rows(self) -> None:
        rows = getattr(self, "_mapping_rows", {})
        remaining = {}
        for new_row in range(self.mapping_table.rowCount()):
            widget = self.mapping_table.cellWidget(new_row, 0)
            for old_row, saved in list(rows.items()):
                if saved.column is widget:
                    remaining[new_row] = saved
                    break
        self._mapping_rows = remaining
        # Reconnect button lambdas to the new row indices.
        for row, widgets in self._mapping_rows.items():
            self.mapping_table.setCellWidget(row, 0, widgets.column)
            self.mapping_table.setCellWidget(row, 1, widgets.target)
            self.mapping_table.setCellWidget(row, 2, widgets.transforms_label)
            edit_button = _icon_button("edit", "Configure transforms for this mapping")
            remove_button = _icon_button("trash", "Remove this mapping row", danger=True)
            edit_button.clicked.connect(lambda _checked=False, r=row: self._edit_transforms(r))
            remove_button.clicked.connect(lambda _checked=False, r=row: self._remove_mapping_row(r))
            widgets.edit_button = edit_button
            widgets.remove_button = remove_button
            self.mapping_table.setCellWidget(row, 3, edit_button)
            self.mapping_table.setCellWidget(row, 4, remove_button)

    def _refresh_mapping_column_choices(self) -> None:
        if self.preview is None:
            return
        for widgets in getattr(self, "_mapping_rows", {}).values():
            current = widgets.column.currentText()
            widgets.column.blockSignals(True)
            widgets.column.clear()
            widgets.column.addItems(list(self.preview.columns))
            widgets.column.setCurrentText(current)
            widgets.column.blockSignals(False)

    def _auto_map(self) -> None:
        if self.preview is None:
            QMessageBox.information(self, "Load a CSV first", "Load a CSV preview before auto-mapping.")
            return
        mapped_columns = {
            widgets.column.currentText().strip().lower()
            for widgets in getattr(self, "_mapping_rows", {}).values()
        }

        def leaf_name(key: str) -> str:
            tail = key.rsplit(":", 1)[-1]
            return tail.rsplit(".", 1)[-1]

        for column in self.preview.columns:
            if column.strip().lower() in mapped_columns:
                continue
            best_key = None
            for target in self.current_targets:
                if leaf_name(target.key).lower() == column.strip().lower():
                    best_key = target.key
                    break
            if best_key is not None:
                self._add_mapping_row(column, best_key)

    def _current_mappings(self) -> list[ColumnMapping]:
        mappings: list[ColumnMapping] = []
        for widgets in getattr(self, "_mapping_rows", {}).values():
            column = widgets.column.currentText().strip()
            target_key = widgets.target.currentData()
            if not column or not target_key:
                continue
            mappings.append(ColumnMapping(column, target_key, widgets.transforms))
        return mappings

    def _refresh_mapping_summary_tab(self) -> None:
        """Populates the Run page's read-only "Mapping" tab from the mappings
        that were actually used to build ``self.plan`` for this run."""
        self.mapping_summary_table.setRowCount(0)
        mappings = self._current_mappings() if self.plan is None else list(self.plan.mappings)
        for mapping in mappings:
            row = self.mapping_summary_table.rowCount()
            self.mapping_summary_table.insertRow(row)
            transforms = ", ".join(t.kind for t in mapping.transforms) or "—"
            self.mapping_summary_table.setItem(row, 0, QTableWidgetItem(mapping.column))
            self.mapping_summary_table.setItem(row, 1, QTableWidgetItem(mapping.target_key))
            self.mapping_summary_table.setItem(row, 2, QTableWidgetItem(transforms))

    # ------------------------------------------------------------------ endpoint selection

    def _service_changed(self, service_name: str) -> None:
        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        service = next((s for s in self.catalog.services if s.name == service_name), None)
        if service is not None:
            for endpoint in service.endpoints:
                self.endpoint_combo.addItem(f"{endpoint.method} {endpoint.path}", endpoint)
        self.endpoint_combo.blockSignals(False)
        self._endpoint_changed(self.endpoint_combo.currentIndex())

    def _endpoint_changed(self, index: int) -> None:
        endpoint = self.endpoint_combo.itemData(index) if index >= 0 else None
        self.current_endpoint = endpoint
        self.current_targets = mapping_targets(endpoint, self.catalog) if endpoint is not None else []
        for widgets in getattr(self, "_mapping_rows", {}).values():
            selected = widgets.target.currentData()
            self._populate_target_combo(widgets.target, selected)

    # ------------------------------------------------------------------ validate

    def _build_validate_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        actions_row = QHBoxLayout()
        self.validate_button = _icon_button(
            "verify", "Validate the mapping against a sample of rows", text="Validate"
        )
        self.validate_button.clicked.connect(self._validate)
        actions_row.addWidget(self.validate_button)
        self.validate_summary_label = QLabel("Not validated yet.")
        actions_row.addWidget(self.validate_summary_label, 1)
        layout.addLayout(actions_row)

        self.plan_issues_list = QListWidget()
        self.plan_issues_list.setMaximumHeight(120)
        layout.addWidget(self.plan_issues_list)

        self.preview_rows_table = QTableWidget(0, 4)
        self.preview_rows_table.setHorizontalHeaderLabels(["Row", "Correlation key", "Status", "Issues"])
        self.preview_rows_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.preview_rows_table.horizontalHeader().setStretchLastSection(True)
        attach_table_empty_state(
            self.preview_rows_table,
            icon_name="verify",
            title="Nothing validated yet",
            guidance="Run Validate to check each mapped row before sending any request.",
        )
        layout.addWidget(self.preview_rows_table, 1)

        return page

    def _build_environment(self) -> ExecutionEnvironmentSnapshot | None:
        try:
            return self.environment_provider()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Environment unavailable", str(exc))
            return None

    def _validate(self) -> None:
        if self.current_endpoint is None:
            QMessageBox.information(self, "Choose an endpoint", "Select a service and endpoint first.")
            return
        if self.csv_source is None:
            QMessageBox.information(self, "Load a CSV first", "Load a CSV preview on the Source tab first.")
            return
        environment = self._build_environment()
        if environment is None:
            return
        mappings = self._current_mappings()
        plan = build_plan(
            self.current_endpoint,
            self.catalog,
            self.csv_source,
            mappings,
            environment,
            default_expected_status=self.default_expected_status.text().strip() or "200-299",
        )
        self.plan = plan
        self._render_plan(plan)
        self._refresh_stepper_progress()

    def _render_plan(self, plan: DataRunPlan) -> None:
        self.validate_summary_label.setText(
            f"{plan.total_row_count} total rows · {plan.preview_valid_count} valid · "
            f"{plan.preview_invalid_count} invalid · {plan.preview_skip_count} skip (sampled preview)"
        )
        self.plan_issues_list.clear()
        for issue in plan.issues:
            item = QListWidgetItem(f"[{issue.severity.upper()}] {issue.message}")
            if issue.severity == "error":
                item.setForeground(QColor(theme.FAIL))
            self.plan_issues_list.addItem(item)

        self.preview_rows_table.setRowCount(len(plan.preview_rows))
        for row_index, preview_row in enumerate(plan.preview_rows):
            status = "Skip" if preview_row.skip else ("Valid" if preview_row.is_valid else "Invalid")
            self.preview_rows_table.setItem(row_index, 0, QTableWidgetItem(str(preview_row.row_number)))
            self.preview_rows_table.setItem(row_index, 1, QTableWidgetItem(preview_row.correlation_key))
            self.preview_rows_table.setItem(row_index, 2, QTableWidgetItem(status))
            self.preview_rows_table.setItem(row_index, 3, QTableWidgetItem(str(preview_row.issue_count)))

        self.run_button.setEnabled(plan.is_executable)

    # ------------------------------------------------------------------ run & results

    def _build_run_page(self) -> QWidget:
        page = QWidget()
        page_layout = QHBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)
        page_layout.setSpacing(10)

        page_layout.addWidget(self._build_run_context_panel())

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        page_layout.addLayout(layout, 1)

        actions_row = QHBoxLayout()
        self.run_button = _icon_button("play", "Run the Data Runner against every valid row")
        self.run_button.setProperty("accent", True)
        _paint_icon(self.run_button, "play")
        self.run_button.setEnabled(False)
        self.run_button.clicked.connect(self._start_run)
        actions_row.addWidget(self.run_button)
        self.pause_button = _icon_button("pause", "Pause after the current row")
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self._toggle_pause)
        actions_row.addWidget(self.pause_button)
        self.stop_button = _icon_button("stop", "Stop the run", danger=True)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop_run)
        actions_row.addWidget(self.stop_button)
        self.export_button = _icon_button("export", "Export row results to CSV")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_results)
        actions_row.addWidget(self.export_button)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
        self.stat_cards: dict[str, StatCard] = {}
        for key, title in (
            ("completed", "Completed"),
            ("passed", "Passed"),
            ("failed", "Failed"),
            ("throughput", "Throughput"),
            ("latency", "P95 latency"),
            ("remaining", "Remaining"),
        ):
            card = StatCard(title)
            self.stat_cards[key] = card
            stats_row.addWidget(card)
        layout.addLayout(stats_row)

        progress_group = QVBoxLayout()
        self.run_progress_bar = QProgressBar()
        self.run_progress_bar.setObjectName("suiteRunProgress")
        self.run_progress_bar.setTextVisible(False)
        progress_group.addWidget(self.run_progress_bar)
        progress_labels_row = QHBoxLayout()
        self.progress_left_label = QLabel("Not run yet.")
        self.progress_left_label.setObjectName("dataRunnerStatCardSubtitle")
        progress_labels_row.addWidget(self.progress_left_label, 1)
        self.progress_right_label = QLabel("")
        self.progress_right_label.setObjectName("dataRunnerStatCardSubtitle")
        progress_labels_row.addWidget(self.progress_right_label)
        progress_group.addLayout(progress_labels_row)
        layout.addLayout(progress_group)

        self.run_summary_label = QLabel("Not run yet.")
        layout.addWidget(self.run_summary_label)

        self.results_tabs = QTabWidget()
        self.results_tabs.addTab(self._build_live_results_tab(), "Live results")
        self.results_tabs.addTab(self._build_input_validation_tab(), "Input validation")
        self.results_tabs.addTab(self._build_errors_tab(), "Errors")
        self.results_tabs.addTab(self._build_charts_tab(), "Charts")
        self.results_tabs.addTab(self._build_mapping_summary_tab(), "Mapping")
        self.results_tabs.addTab(self._build_export_tab(), "Export")
        layout.addWidget(self.results_tabs, 1)

        return page

    def _build_live_results_tab(self) -> QWidget:
        """The default results tab: a results table on the left and a
        master-detail row inspector (Response/Resolved request/Input row/
        Assertions) on the right, driven by ``self._row_details``.

        The table is paginated (see ``self._result_rows``/``_render_current_page``)
        so runs with tens of thousands of rows stay responsive: only the
        current page's rows are ever materialised as ``QTableWidgetItem``s.
        While a run is live, the view auto-follows the newest page unless the
        user has manually navigated away (``self._follow_latest_page``)."""
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        pagination_row = QHBoxLayout()
        pagination_row.addWidget(QLabel("Rows per page:"))
        self.results_page_size = QComboBox()
        self.results_page_size.addItems(["100", "250", "500", "1000", "5000"])
        self.results_page_size.setCurrentText("500")
        self.results_page_size.currentTextChanged.connect(self._on_page_size_changed)
        pagination_row.addWidget(self.results_page_size)
        pagination_row.addStretch(1)
        self.results_prev_page_button = QPushButton("< Prev")
        self.results_prev_page_button.clicked.connect(self._go_to_previous_page)
        self.results_next_page_button = QPushButton("Next >")
        self.results_next_page_button.clicked.connect(self._go_to_next_page)
        self.results_jump_latest_button = QPushButton("Jump to latest")
        self.results_jump_latest_button.clicked.connect(self._jump_to_latest_page)
        self.results_page_label = QLabel("Page 0 of 0 (0 rows)")
        pagination_row.addWidget(self.results_page_label)
        pagination_row.addWidget(self.results_prev_page_button)
        pagination_row.addWidget(self.results_next_page_button)
        pagination_row.addWidget(self.results_jump_latest_button)
        tab_layout.addLayout(pagination_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.results_table = QTableWidget(0, 6)
        self.results_table.setHorizontalHeaderLabels(
            ["Row", "Outcome", "Status", "Duration (ms)", "Error", "Detail"]
        )
        self.results_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.results_table.horizontalHeader().setStretchLastSection(True)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.results_table.itemSelectionChanged.connect(self._on_result_row_selected)
        attach_table_empty_state(
            self.results_table,
            icon_name="play",
            title="No results yet",
            guidance="Start the run to see each row's outcome, status, and duration here.",
        )
        splitter.addWidget(self.results_table)

        splitter.addWidget(self._build_row_inspector())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        tab_layout.addWidget(splitter, 1)
        return tab

    def _build_row_inspector(self) -> QWidget:
        box = QGroupBox("Row inspector")
        box_layout = QVBoxLayout(box)
        self.row_inspector_hint = QLabel("Select a row to inspect its request/response.")
        self.row_inspector_hint.setObjectName("dataRunnerStatCardSubtitle")
        self.row_inspector_hint.setWordWrap(True)
        box_layout.addWidget(self.row_inspector_hint)

        self.row_inspector_tabs = QTabWidget()
        self.row_inspector_response = JsonTextEdit(read_only=True)
        self.row_inspector_request = JsonTextEdit(read_only=True)
        self.row_inspector_input_row = JsonTextEdit(read_only=True)
        self.row_inspector_assertions = JsonTextEdit(read_only=True)
        self.row_inspector_tabs.addTab(self.row_inspector_response, "Response")
        self.row_inspector_tabs.addTab(self.row_inspector_request, "Resolved request")
        self.row_inspector_tabs.addTab(self.row_inspector_input_row, "Input row")
        self.row_inspector_tabs.addTab(self.row_inspector_assertions, "Assertions")
        box_layout.addWidget(self.row_inspector_tabs, 1)
        return box

    def _build_input_validation_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.addWidget(
            QLabel("Rows that were skipped or failed validation before any request was sent.")
        )
        self.invalid_rows_table = QTableWidget(0, 3)
        self.invalid_rows_table.setHorizontalHeaderLabels(["Row", "Outcome", "Issues"])
        self.invalid_rows_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.invalid_rows_table.horizontalHeader().setStretchLastSection(True)
        attach_table_empty_state(
            self.invalid_rows_table,
            icon_name="verify",
            title="No invalid rows",
            guidance="Every row passed validation. Rows rejected before sending appear here.",
        )
        tab_layout.addWidget(self.invalid_rows_table, 1)
        return tab

    def _build_errors_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.addWidget(QLabel("Failed and errored rows, aggregated by category/message."))

        filter_row = QHBoxLayout()
        self.errors_search_box = QLineEdit()
        self.errors_search_box.setPlaceholderText("Search row number, category, or message...")
        self.errors_search_box.textChanged.connect(self._apply_errors_filter)
        filter_row.addWidget(self.errors_search_box, 1)
        self.errors_category_filter = QComboBox()
        self.errors_category_filter.addItem("All categories")
        self.errors_category_filter.currentTextChanged.connect(self._apply_errors_filter)
        filter_row.addWidget(self.errors_category_filter)
        tab_layout.addLayout(filter_row)

        self.run_errors_list = QListWidget()
        tab_layout.addWidget(self.run_errors_list, 1)
        self.errors_filter_summary_label = QLabel("")
        self.errors_filter_summary_label.setObjectName("dataRunnerStatCardSubtitle")
        tab_layout.addWidget(self.errors_filter_summary_label)
        return tab

    def _build_charts_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setSpacing(10)

        top_row = QHBoxLayout()
        outcome_box = QGroupBox("Outcome breakdown")
        outcome_layout = QVBoxLayout(outcome_box)
        self.outcome_chart = OutcomeBreakdownChart()
        outcome_layout.addWidget(self.outcome_chart)
        outcome_layout.addWidget(QLabel("Status codes"))
        self.status_code_chart = LabeledBarChart(color=theme.PRIMARY, value_format="{:.0f}")
        outcome_layout.addWidget(self.status_code_chart, 1)
        top_row.addWidget(outcome_box, 1)

        throughput_box = QGroupBox("Throughput (requests / second)")
        throughput_layout = QVBoxLayout(throughput_box)
        self.throughput_chart = SparklineChart(mode="bar", color=theme.PRIMARY)
        throughput_layout.addWidget(self.throughput_chart)
        throughput_layout.addWidget(QLabel("Cumulative rows completed"))
        self.cumulative_completed_chart = MultiSeriesLineChart(
            (("Completed", theme.PRIMARY),), unit=""
        )
        throughput_layout.addWidget(self.cumulative_completed_chart, 1)
        top_row.addWidget(throughput_box, 1)

        latency_box = QGroupBox("Latency (ms)")
        latency_layout = QVBoxLayout(latency_box)
        self.latency_chart = SparklineChart(mode="line", color=theme.ACCENT)
        latency_layout.addWidget(self.latency_chart)
        self.latency_summary_label = QLabel("min – / mean – / p50 – / p90 – / p99 – / max –")
        latency_layout.addWidget(self.latency_summary_label)
        self.latency_percentile_chart = LabeledBarChart(color=theme.ACCENT, value_format="{:.0f} ms")
        latency_layout.addWidget(self.latency_percentile_chart, 1)
        top_row.addWidget(latency_box, 1)
        tab_layout.addLayout(top_row, 2)

        bottom_row = QHBoxLayout()
        errors_box = QGroupBox("Errors by category")
        errors_layout = QVBoxLayout(errors_box)
        self.error_category_chart = LabeledBarChart(color=theme.FAIL, value_format="{:.0f}")
        errors_layout.addWidget(self.error_category_chart)
        bottom_row.addWidget(errors_box, 1)

        latency_timeline_box = QGroupBox("Latency by row (spot slow clusters)")
        timeline_layout = QVBoxLayout(latency_timeline_box)
        self.latency_timeline_chart = ScatterChart(color=theme.ACCENT, unit=" ms")
        timeline_layout.addWidget(self.latency_timeline_chart)
        bottom_row.addWidget(latency_timeline_box, 2)
        tab_layout.addLayout(bottom_row, 1)

        return tab

    def _build_mapping_summary_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.addWidget(QLabel("The column -> target mapping used for this run."))
        self.mapping_summary_table = QTableWidget(0, 3)
        self.mapping_summary_table.setHorizontalHeaderLabels(["CSV column", "Target", "Transforms"])
        self.mapping_summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.mapping_summary_table.horizontalHeader().setStretchLastSection(True)
        tab_layout.addWidget(self.mapping_summary_table, 1)
        return tab

    def _build_export_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)

        persistence_group = QGroupBox("Run history (SQLite)")
        persistence_row = QHBoxLayout(persistence_group)
        self.persist_checkbox = QCheckBox("Persist this run's samples")
        persistence_row.addWidget(self.persist_checkbox)
        self.persist_path = QLineEdit()
        self.persist_path.setPlaceholderText("Path to a .db file (defaults next to the CSV)")
        persistence_row.addWidget(self.persist_path, 1)
        self.persist_browse_button = _icon_button("folder", "Choose where to store the run history database")
        self.persist_browse_button.clicked.connect(self._browse_persist_path)
        persistence_row.addWidget(self.persist_browse_button)
        tab_layout.addWidget(persistence_group)

        export_group = QGroupBox("Export row results")
        export_layout = QHBoxLayout(export_group)
        export_layout.addWidget(QLabel("Export the reserved diagnostic columns for every row to a CSV file."))
        export_layout.addStretch()
        export_tab_button = _icon_button("export", "Export row results to CSV")
        export_tab_button.clicked.connect(self._export_results)
        export_layout.addWidget(export_tab_button)
        tab_layout.addWidget(export_group)
        tab_layout.addStretch()
        return tab

    def _build_run_context_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(240)
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(8)

        self.context_card_source = ContextCard("Input source")
        panel_layout.addWidget(self.context_card_source)
        self.context_card_template = ContextCard("Execution template")
        panel_layout.addWidget(self.context_card_template)
        self.context_card_mapping = ContextCard("Schema-aware mapping")
        panel_layout.addWidget(self.context_card_mapping)
        self.context_card_settings = ContextCard("Run settings")
        panel_layout.addWidget(self.context_card_settings)

        self.write_warning_label = QLabel()
        self.write_warning_label.setObjectName("dataRunnerWriteWarning")
        self.write_warning_label.setWordWrap(True)
        self.write_warning_label.setVisible(False)
        panel_layout.addWidget(self.write_warning_label)

        panel_layout.addStretch()
        return panel

    def _refresh_run_context_panel(self) -> None:
        self.context_card_source.clear_body()
        if self.csv_source is not None and self.preview is not None:
            path = self.csv_source.settings.path
            name = path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if path else "—"
            self.context_card_source.body.addWidget(QLabel(f"<b>{name}</b>"))
            format_caption = QLabel(
                f"{self.csv_source.settings.encoding or 'auto'} · header {'yes' if self.csv_source.settings.has_header else 'no'}"
            )
            format_caption.setObjectName("dataRunnerStatCardSubtitle")
            self.context_card_source.body.addWidget(format_caption)

            if self.plan is not None:
                rows_value = self.plan.total_row_count
                valid_value = self.plan.preview_valid_count
                skipped_value = self.plan.preview_invalid_count + self.plan.preview_skip_count
            else:
                rows_value = self.preview.total_rows_sampled
                valid_value = self.preview.total_rows_sampled - self.preview.blank_row_count - self.preview.duplicate_row_count
                skipped_value = self.preview.blank_row_count + self.preview.duplicate_row_count
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            for column, (caption, value) in enumerate(
                (
                    ("Rows", rows_value),
                    ("Columns", len(self.preview.columns)),
                    ("Valid", valid_value),
                    ("Skipped", skipped_value),
                )
            ):
                cell = QVBoxLayout()
                caption_label = QLabel(caption)
                caption_label.setObjectName("dataRunnerStatCardSubtitle")
                cell.addWidget(caption_label)
                value_label = QLabel(f"{value:,}")
                value_label.setStyleSheet("font-weight: 700;")
                cell.addWidget(value_label)
                grid.addLayout(cell, column // 2, column % 2)
            self.context_card_source.body.addLayout(grid)
        else:
            self.context_card_source.body.addWidget(QLabel("No CSV loaded yet."))

        self.context_card_template.clear_body()
        if self.current_endpoint is not None:
            method_label = QLabel(self.current_endpoint.method)
            method_label.setStyleSheet(
                f"color: {theme.method_color(self.current_endpoint.method)}; font-weight: 700;"
            )
            self.context_card_template.body.addWidget(method_label)
            self.context_card_template.body.addWidget(QLabel(self.current_endpoint.path))
            service_caption = QLabel(self.service_combo.currentText())
            service_caption.setObjectName("dataRunnerStatCardSubtitle")
            self.context_card_template.body.addWidget(service_caption)
        else:
            self.context_card_template.body.addWidget(QLabel("No endpoint selected."))

        self.context_card_mapping.clear_body()
        mappings = self._current_mappings()
        if mappings:
            for mapping in mappings[:6]:
                row = QHBoxLayout()
                check = QLabel("✓")
                check.setStyleSheet(f"color: {theme.PASS}; font-weight: 700;")
                row.addWidget(check)
                target_label = self._target_label_for_key(mapping.target_key)
                row.addWidget(QLabel(f"{target_label}"))
                row.addStretch()
                source_label = QLabel(mapping.column)
                source_label.setObjectName("dataRunnerStatCardSubtitle")
                row.addWidget(source_label)
                self.context_card_mapping.body.addLayout(row)
            if len(mappings) > 6:
                more_label = QLabel(f"+{len(mappings) - 6} more")
                more_label.setObjectName("dataRunnerStatCardSubtitle")
                self.context_card_mapping.body.addWidget(more_label)
        else:
            self.context_card_mapping.body.addWidget(QLabel("No mappings yet."))

        self.context_card_settings.clear_body()
        settings_rows = [
            ("Execution mode", "Sequential"),
            ("Default expected status", self.default_expected_status.text().strip() or "200-299"),
            ("Persist history", "Yes" if self.persist_checkbox.isChecked() else "No"),
        ]
        for caption, value in settings_rows:
            row = QHBoxLayout()
            caption_label = QLabel(caption)
            caption_label.setObjectName("dataRunnerStatCardSubtitle")
            row.addWidget(caption_label)
            row.addStretch()
            row.addWidget(QLabel(str(value)))
            self.context_card_settings.body.addLayout(row)

        if self.current_endpoint is not None and self.current_endpoint.method.upper() in (
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        ):
            self.write_warning_label.setText(
                f"This is a write endpoint ({self.current_endpoint.method}). Every valid row will call "
                "the live service — review the mapping and environment before running."
            )
            self.write_warning_label.setVisible(True)
        else:
            self.write_warning_label.setVisible(False)

    def _default_persist_path(self) -> str:
        if self.csv_source is not None and self.csv_source.settings.path:
            return f"{self.csv_source.settings.path}.runs.db"
        return ""

    def _browse_persist_path(self) -> None:
        default = self.persist_path.text().strip() or self._default_persist_path()
        path, _ = QFileDialog.getSaveFileName(self, "Choose run history database", default, "SQLite database (*.db)")
        if path:
            self.persist_path.setText(path)

    def _reset_live_metrics(self) -> None:
        self._latency_sketch = LatencySketch()
        self._outcome_counts = {}
        self._throughput_buckets = {}
        self._status_code_counts = {}
        self._error_category_counts = {}
        self._cumulative_completed = 0
        self._row_details = {}
        self._loaded_history_run_id = None
        self._result_rows = []
        self._current_results_page = 0
        self._follow_latest_page = True
        self.results_table.setRowCount(0)
        self._update_pagination_controls()
        self.throughput_chart.clear()
        self.latency_chart.clear()
        self.latency_summary_label.setText("min – / mean – / p50 – / p90 – / p99 – / max –")
        self.status_code_chart.set_entries({})
        self.cumulative_completed_chart.clear()
        self.latency_percentile_chart.set_entries({})
        self.error_category_chart.set_entries({})
        self.latency_timeline_chart.clear()
        for card in self.stat_cards.values():
            card.set_value("–")
            card.set_subtitle("")
        self.run_progress_bar.setValue(0)
        self.progress_left_label.setText("Not run yet.")
        self.progress_right_label.setText("")
        self.invalid_rows_table.setRowCount(0)
        self._error_entries = []
        self.run_errors_list.clear()
        self.errors_search_box.clear()
        self.errors_category_filter.clear()
        self.errors_category_filter.addItem("All categories")
        self.errors_filter_summary_label.setText("")
        self._clear_row_inspector()

    def _start_run(self) -> None:
        if self.plan is None or not self.plan.is_executable:
            QMessageBox.information(self, "Validate first", "Validate the mapping before running.")
            return
        environment = self._build_environment()
        if environment is None:
            return
        # Re-plan against the freshest environment/mappings right before execution.
        plan = build_plan(
            self.current_endpoint,
            self.catalog,
            self.csv_source,
            self._current_mappings(),
            environment,
            default_expected_status=self.default_expected_status.text().strip() or "200-299",
        )
        if not plan.is_executable:
            self._render_plan(plan)
            QMessageBox.warning(self, "Cannot run", "The current plan has blocking issues; check the Validate tab.")
            return
        self.plan = plan

        self.results_table.setRowCount(0)
        self._reset_live_metrics()
        self._refresh_mapping_summary_tab()
        self.run_summary_label.setText("Running...")
        self._run_started_monotonic = time.monotonic()
        self.run_progress_bar.setRange(0, max(1, plan.total_row_count))
        self.progress_left_label.setText(f"0 of {plan.total_row_count:,} rows completed")
        self._event_bus = EventBus()
        cancellation = CancellationController()

        if self._run_store is not None:
            self._run_store.close()
            self._run_store = None
        store = None
        if self.persist_checkbox.isChecked():
            path = self.persist_path.text().strip() or self._default_persist_path()
            if path:
                self.persist_path.setText(path)
                store = RunStore(path)
                self._run_store = store

        self.runner = DataRunner(
            plan,
            store=store,
            event_bus=self._event_bus,
            cancellation=cancellation,
            options=DataRunnerOptions(),
        )
        self._paused = False

        self._thread = QThread()
        self._worker = DataRunnerWorker(self.runner)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._run_finished)
        self._worker.failed.connect(self._run_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

        self.run_button.setEnabled(False)
        self.pause_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self.export_button.setEnabled(False)
        self._event_timer.start()
        self._refresh_run_context_panel()
        self._refresh_stepper_progress()

    def _toggle_pause(self) -> None:
        if self.runner is None:
            return
        self._paused = not self._paused
        if self._paused:
            self.runner.cancellation.pause()
            self.pause_button.setToolTip("Resume the run")
            self.pause_button.setIcon(icon("play", theme.TEXT, 18))
        else:
            self.runner.cancellation.resume()
            self.pause_button.setToolTip("Pause after the current row")
            self.pause_button.setIcon(icon("pause", theme.TEXT, 18))

    def _stop_run(self) -> None:
        if self.runner is not None:
            self.runner.cancellation.stop()
            self.stop_button.setEnabled(False)
            self.run_summary_label.setText("Stopping after the current row...")

    def _drain_events(self) -> None:
        if self._event_bus is None:
            return
        for event in self._event_bus.drain():
            if event.kind in ("request_completed", "request_failed"):
                self._append_result_row(event.payload)
                self._update_live_metrics(event.payload)
            elif event.kind == "row_completed":
                # Skipped/invalid rows never hit the network, so they only
                # arrive via this event rather than request_completed/failed.
                outcome = event.payload.get("outcome")
                self._append_result_row(
                    {
                        "row_number": event.payload.get("row_number"),
                        "outcome": outcome,
                        "detail": event.payload.get("detail"),
                    }
                )
                self._outcome_counts[outcome] = self._outcome_counts.get(outcome, 0) + 1
                self.outcome_chart.set_counts(self._outcome_counts)
                self._cumulative_completed += 1
                self.cumulative_completed_chart.append({"Completed": self._cumulative_completed})
                self._refresh_run_progress()

    def _update_live_metrics(self, payload: dict[str, Any]) -> None:
        outcome = str(payload.get("outcome", ""))
        self._outcome_counts[outcome] = self._outcome_counts.get(outcome, 0) + 1
        self.outcome_chart.set_counts(self._outcome_counts)

        status_code = payload.get("status_code")
        if status_code not in (None, ""):
            key = str(status_code)
            self._status_code_counts[key] = self._status_code_counts.get(key, 0) + 1
            self.status_code_chart.set_entries(self._status_code_counts)

        http_ms = payload.get("http_ms")
        if isinstance(http_ms, (int, float)):
            self._latency_sketch.add(float(http_ms))
            self.latency_chart.append(float(http_ms))
            self.latency_summary_label.setText(
                f"min {self._latency_sketch.min:.0f} / mean {self._latency_sketch.mean:.0f} / "
                f"p50 {self._latency_sketch.percentile(50):.0f} / p90 {self._latency_sketch.percentile(90):.0f} / "
                f"p99 {self._latency_sketch.percentile(99):.0f} / max {self._latency_sketch.max:.0f}"
            )
            self.latency_percentile_chart.set_entries(
                [
                    ("p50", self._latency_sketch.percentile(50)),
                    ("p90", self._latency_sketch.percentile(90)),
                    ("p99", self._latency_sketch.percentile(99)),
                    ("max", self._latency_sketch.max),
                ],
                sort_descending=False,
            )
            row_number = payload.get("row_number")
            if row_number is not None:
                self.latency_timeline_chart.append(float(row_number), float(http_ms))

        self._cumulative_completed += 1
        self.cumulative_completed_chart.append({"Completed": self._cumulative_completed})

        offset_ms = payload.get("offset_ms")
        if isinstance(offset_ms, (int, float)):
            bucket = int(offset_ms // 1000)
            self._throughput_buckets[bucket] = self._throughput_buckets.get(bucket, 0) + 1
            if self._throughput_buckets:
                span = range(min(self._throughput_buckets), max(self._throughput_buckets) + 1)
                self.throughput_chart.set_values([self._throughput_buckets.get(i, 0) for i in span])

        self._refresh_run_progress()

    def _refresh_run_progress(self) -> None:
        """Recomputes the stat cards and progress bar from live counters —
        called after every drained event so they track the run in real time."""
        completed = sum(self._outcome_counts.values())
        passed = self._outcome_counts.get("passed", 0)
        failed = self._outcome_counts.get("failed", 0)
        errored = self._outcome_counts.get("error", 0)
        total = self.plan.total_row_count if self.plan is not None else 0

        self.stat_cards["completed"].set_value(f"{completed:,}")
        if total:
            self.stat_cards["completed"].set_subtitle(f"{completed / total * 100:.0f}% of {total:,} rows")

        self.stat_cards["passed"].set_value(f"{passed:,}", color=theme.PASS if passed else None)
        if completed:
            self.stat_cards["passed"].set_subtitle(f"{passed / completed * 100:.0f}% success")

        failed_total = failed + errored
        self.stat_cards["failed"].set_value(f"{failed_total:,}", color=theme.FAIL if failed_total else None)
        if failed_total:
            self.stat_cards["failed"].set_subtitle(f"{failed} HTTP · {errored} network")

        elapsed = (time.monotonic() - self._run_started_monotonic) if self._run_started_monotonic else 0.0
        rate = completed / elapsed if elapsed > 0 else 0.0
        self.stat_cards["throughput"].set_value(f"{rate:.1f} rows/s" if completed else "–")

        has_latency = self._latency_sketch.count > 0
        self.stat_cards["latency"].set_value(f"{self._latency_sketch.percentile(95):.0f} ms" if has_latency else "–")
        if has_latency:
            self.stat_cards["latency"].set_subtitle(f"p50 {self._latency_sketch.percentile(50):.0f} ms")

        remaining = max(0, total - completed) if total else None
        self.stat_cards["remaining"].set_value(f"{remaining:,}" if remaining is not None else "–")

        if total:
            self.run_progress_bar.setRange(0, total)
            self.run_progress_bar.setValue(min(completed, total))
            self.progress_left_label.setText(f"{completed:,} of {total:,} rows completed")
            if remaining and rate > 0:
                eta_seconds = remaining / rate
                minutes, seconds = divmod(int(eta_seconds), 60)
                self.progress_right_label.setText(f"Estimated remaining {minutes:02d}:{seconds:02d}")
            else:
                self.progress_right_label.setText("")

    def _append_result_row(self, payload: dict[str, Any]) -> None:
        """Records one row's result and, if the currently displayed page is
        the newest one (live-tailing), reflects it into the table
        immediately. See ``_render_current_page``/``_update_pagination_controls``
        for how paging keeps large runs responsive."""
        row_number = payload.get("row_number")
        outcome = str(payload.get("outcome", ""))
        summary = {
            "row_number": row_number,
            "outcome": outcome,
            "status_code": payload.get("status_code", ""),
            "http_ms": payload.get("http_ms", ""),
            "error_category": payload.get("error_category", ""),
            "error_message": payload.get("error_message", ""),
        }
        self._result_rows.append(summary)

        detail = payload.get("detail")
        if row_number is not None and detail is not None:
            self._row_details[int(row_number)] = detail

        if outcome in ("skipped", "invalid"):
            self._append_invalid_row(row_number, outcome, detail)
        elif outcome in ("failed", "error"):
            category = payload.get("error_category") or outcome
            message = payload.get("error_message") or ""
            self._record_error_entry(row_number, category, message)

        new_last_page = self._total_results_pages() - 1
        if self._follow_latest_page:
            if self._current_results_page != new_last_page:
                self._current_results_page = new_last_page
                self._render_current_page()
            else:
                self._append_table_row(summary)
                self._update_pagination_controls()
        else:
            self._update_pagination_controls()

    def _append_table_row(self, summary: dict[str, Any]) -> None:
        row = self.results_table.rowCount()
        self.results_table.insertRow(row)
        outcome = str(summary.get("outcome", ""))
        row_number = summary.get("row_number")
        items = [
            str(row_number if row_number is not None else ""),
            outcome,
            str(summary.get("status_code", "")),
            str(summary.get("http_ms", "")),
            str(summary.get("error_category", "") or ""),
            str(summary.get("error_message", "") or ""),
        ]
        for column, text in enumerate(items):
            item = QTableWidgetItem(text)
            color = _OUTCOME_COLORS.get(outcome)
            if color:
                item.setForeground(QColor(color))
            if column == 0 and row_number is not None:
                item.setData(Qt.ItemDataRole.UserRole, row_number)
            self.results_table.setItem(row, column, item)

    def _total_results_pages(self) -> int:
        if not self._result_rows:
            return 1
        return (len(self._result_rows) - 1) // self._results_page_size + 1

    def _render_current_page(self) -> None:
        self.results_table.setRowCount(0)
        start = self._current_results_page * self._results_page_size
        end = start + self._results_page_size
        for summary in self._result_rows[start:end]:
            self._append_table_row(summary)
        self._update_pagination_controls()

    def _update_pagination_controls(self) -> None:
        total_pages = self._total_results_pages()
        total_rows = len(self._result_rows)
        self.results_page_label.setText(
            f"Page {self._current_results_page + 1} of {total_pages} ({total_rows:,} rows)"
        )
        self.results_prev_page_button.setEnabled(self._current_results_page > 0)
        self.results_next_page_button.setEnabled(self._current_results_page < total_pages - 1)
        self.results_jump_latest_button.setEnabled(not self._follow_latest_page)

    def _on_page_size_changed(self, text: str) -> None:
        try:
            size = int(text)
        except ValueError:
            return
        if size == self._results_page_size:
            return
        # Re-anchor to the first row of the page currently being viewed so
        # changing page size doesn't feel like it "loses" the user's place.
        first_visible_row = self._current_results_page * self._results_page_size
        self._results_page_size = size
        self._current_results_page = min(
            first_visible_row // size, max(0, self._total_results_pages() - 1)
        )
        self._render_current_page()

    def _go_to_previous_page(self) -> None:
        if self._current_results_page <= 0:
            return
        self._current_results_page -= 1
        self._follow_latest_page = False
        self._render_current_page()

    def _go_to_next_page(self) -> None:
        total_pages = self._total_results_pages()
        if self._current_results_page >= total_pages - 1:
            return
        self._current_results_page += 1
        self._follow_latest_page = self._current_results_page == total_pages - 1
        self._render_current_page()

    def _jump_to_latest_page(self) -> None:
        self._follow_latest_page = True
        self._current_results_page = self._total_results_pages() - 1
        self._render_current_page()

    def _append_invalid_row(self, row_number: Any, outcome: str, detail: dict[str, Any] | None) -> None:
        assertions = (detail or {}).get("assertions") or []
        issues = "; ".join(str(a.get("name", "")) for a in assertions) or "—"
        row = self.invalid_rows_table.rowCount()
        self.invalid_rows_table.insertRow(row)
        self.invalid_rows_table.setItem(row, 0, QTableWidgetItem(str(row_number if row_number is not None else "")))
        self.invalid_rows_table.setItem(row, 1, QTableWidgetItem(outcome))
        self.invalid_rows_table.setItem(row, 2, QTableWidgetItem(issues))

    def _record_error_entry(self, row_number: Any, category: str, message: str) -> None:
        """Appends to the unfiltered backing list for the Errors tab, adds
        the category to the filter dropdown if new, and either appends the
        new entry directly (no active filter, so it's certainly visible) or
        re-applies the current filter (keeps large-run performance linear
        rather than re-rendering the whole list on every single error)."""
        self._error_entries.append({"row_number": row_number, "category": category, "message": message})
        self._error_category_counts[category] = self._error_category_counts.get(category, 0) + 1
        self.error_category_chart.set_entries(self._error_category_counts)
        existing_categories = {
            self.errors_category_filter.itemText(i) for i in range(self.errors_category_filter.count())
        }
        if category not in existing_categories:
            self.errors_category_filter.addItem(category)
        if self._errors_filter_is_active():
            self._apply_errors_filter()
        else:
            self.run_errors_list.addItem(f"Row {row_number} [{category}] {message}")
            self.errors_filter_summary_label.setText(f"{len(self._error_entries)} errors")

    def _errors_filter_is_active(self) -> bool:
        return bool(self.errors_search_box.text().strip()) or self.errors_category_filter.currentText() != "All categories"

    def _apply_errors_filter(self, *_args: Any) -> None:
        search = self.errors_search_box.text().strip().lower()
        category_filter = self.errors_category_filter.currentText()
        self.run_errors_list.clear()
        shown = 0
        for entry in self._error_entries:
            if category_filter != "All categories" and entry["category"] != category_filter:
                continue
            text = f"Row {entry['row_number']} [{entry['category']}] {entry['message']}"
            if search and search not in text.lower():
                continue
            self.run_errors_list.addItem(text)
            shown += 1
        total = len(self._error_entries)
        if search or category_filter != "All categories":
            self.errors_filter_summary_label.setText(f"Showing {shown} of {total} errors")
        else:
            self.errors_filter_summary_label.setText(f"{total} errors" if total else "")

    def _clear_row_inspector(self) -> None:
        self.row_inspector_hint.setText("Select a row to inspect its request/response.")
        for editor in (
            self.row_inspector_response,
            self.row_inspector_request,
            self.row_inspector_input_row,
            self.row_inspector_assertions,
        ):
            editor.setPlainText("")

    def _fetch_persisted_row_detail(self, row_number: int) -> dict[str, Any] | None:
        """Lazily loads a single row's detail payload from the History
        store's row_details table (populated by RunStore.record_row_detail),
        so History-loaded runs get a working row inspector without having
        to preload every row's detail up front."""
        if self._loaded_history_run_id is None or self._history_store is None:
            return None
        detail = self._history_store.get_row_detail(self._loaded_history_run_id, row_number)
        if detail is not None:
            self._row_details[row_number] = detail
        return detail

    def _on_result_row_selected(self) -> None:
        selected = self.results_table.selectedItems()
        if not selected:
            self._clear_row_inspector()
            return
        row = selected[0].row()
        row_item = self.results_table.item(row, 0)
        row_number = row_item.data(Qt.ItemDataRole.UserRole) if row_item is not None else None
        if row_number is None:
            self._clear_row_inspector()
            return
        detail = self._row_details.get(int(row_number))
        if detail is None:
            detail = self._fetch_persisted_row_detail(int(row_number))
        if detail is None:
            self.row_inspector_hint.setText(
                f"Row {row_number}: no detail captured for this row (run predates detail capture)."
            )
            for editor in (
                self.row_inspector_response,
                self.row_inspector_request,
                self.row_inspector_input_row,
                self.row_inspector_assertions,
            ):
                editor.setPlainText("")
            return
        self.row_inspector_hint.setText(f"Row {row_number}")
        response = detail.get("response")
        if response is None:
            self.row_inspector_response.setPlainText("(no response — row was not executed)")
        elif not response.get("body_retained", False):
            self.row_inspector_response.setPlainText(
                json.dumps(
                    {k: v for k, v in response.items() if k != "body"},
                    indent=2,
                )
                + "\n\n(Successful response bodies are not retained.)"
            )
        else:
            self.row_inspector_response.setPlainText(json.dumps(response, indent=2))
        self.row_inspector_request.setPlainText(json.dumps(detail.get("resolved_request", {}), indent=2))
        self.row_inspector_input_row.setPlainText(json.dumps(detail.get("input_row", {}), indent=2))
        self.row_inspector_assertions.setPlainText(json.dumps(detail.get("assertions", []), indent=2))

    def _run_finished(self, summary: RunSummary) -> None:
        self._drain_events()
        self._event_timer.stop()
        self.run_summary_label.setText(
            f"{summary.outcome} — {summary.executed} executed, {summary.passed} passed, "
            f"{summary.failed} failed, {summary.errored} errored, {summary.invalid} invalid, "
            f"{summary.skipped} skipped"
        )
        self.export_button.setEnabled(bool(self.runner and self.runner.row_outcomes))
        self.progress_right_label.setText("")
        self._refresh_stepper_progress()

    def _run_failed(self, message: str) -> None:
        self._event_timer.stop()
        self.run_summary_label.setText("Run failed.")
        QMessageBox.warning(self, "Data Runner failed", message)

    def _cleanup_thread(self) -> None:
        self.run_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    def _export_results(self) -> None:
        if self.runner is None or not self.runner.row_outcomes:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export row results", "results.csv", "CSV files (*.csv)")
        if path:
            self.runner.export_rows_csv(path)

    # ------------------------------------------------------------------ history

    def _build_history_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        path_row = QHBoxLayout()
        self.history_path = QLineEdit()
        self.history_path.setPlaceholderText("Path to a run history .db file...")
        path_row.addWidget(self.history_path, 1)
        self.history_browse_button = _icon_button("folder", "Browse for a run history database")
        self.history_browse_button.clicked.connect(self._browse_history_path)
        path_row.addWidget(self.history_browse_button)
        self.history_refresh_button = _icon_button("renew", "Reload runs from this database")
        self.history_refresh_button.clicked.connect(self._refresh_history)
        path_row.addWidget(self.history_refresh_button)
        layout.addLayout(path_row)

        splitter = QSplitter()
        self.history_runs_table = QTableWidget(0, 6)
        self.history_runs_table.setHorizontalHeaderLabels(
            ["Run", "Endpoint", "Started", "Finished", "Outcome", "CSV"]
        )
        self.history_runs_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.history_runs_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.history_runs_table.itemSelectionChanged.connect(self._history_selection_changed)
        attach_table_empty_state(
            self.history_runs_table,
            icon_name="open",
            title="No runs loaded",
            guidance="Choose a run history database above, then refresh to list its runs.",
        )
        splitter.addWidget(self.history_runs_table)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)
        self.history_outcome_chart = OutcomeBreakdownChart()
        detail_layout.addWidget(self.history_outcome_chart)
        self.history_errors_list = QListWidget()
        detail_layout.addWidget(QLabel("Grouped errors"))
        detail_layout.addWidget(self.history_errors_list)
        actions_row = QHBoxLayout()
        self.history_load_button = _icon_button(
            "open", "Load this run's rows into the Run & Results tab", text="Load"
        )
        self.history_load_button.setEnabled(False)
        self.history_load_button.clicked.connect(self._load_history_run_into_results)
        actions_row.addWidget(self.history_load_button)
        self.history_export_button = _icon_button(
            "export", "Export this run's rows to CSV", text="Export"
        )
        self.history_export_button.setEnabled(False)
        self.history_export_button.clicked.connect(self._export_history_run)
        actions_row.addWidget(self.history_export_button)
        actions_row.addStretch()
        detail_layout.addLayout(actions_row)
        splitter.addWidget(detail_widget)
        layout.addWidget(splitter, 1)

        return page

    def _browse_history_path(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose run history database", "", "SQLite database (*.db)")
        if path:
            self.history_path.setText(path)
            self._refresh_history()

    def _refresh_history(self) -> None:
        path = self.history_path.text().strip()
        if not path:
            QMessageBox.information(self, "Choose a database", "Enter or browse for a run history .db file first.")
            return
        if self._history_store is not None:
            self._history_store.close()
        self._history_store = RunStore(path)
        runs = self._history_store.list_runs(kind="data_runner")
        self.history_runs_table.setRowCount(len(runs))
        for row, run in enumerate(runs):
            definition = run.get("definition") or {}
            values = [
                run["run_id"],
                str(definition.get("endpoint_id", "")),
                str(run.get("started_at", "")),
                str(run.get("finished_at", "")),
                str(run.get("outcome", "")),
                str(definition.get("csv_path", "")),
            ]
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, run["run_id"])
                self.history_runs_table.setItem(row, column, item)
        self.history_load_button.setEnabled(False)
        self.history_export_button.setEnabled(False)
        self.history_errors_list.clear()
        self.history_outcome_chart.set_counts({})

    def _selected_history_run_id(self) -> str | None:
        selected = self.history_runs_table.selectedItems()
        if not selected:
            return None
        row = selected[0].row()
        item = self.history_runs_table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _history_selection_changed(self) -> None:
        run_id = self._selected_history_run_id()
        has_selection = run_id is not None and self._history_store is not None
        self.history_load_button.setEnabled(has_selection)
        self.history_export_button.setEnabled(has_selection)
        if not has_selection:
            self.history_outcome_chart.set_counts({})
            self.history_errors_list.clear()
            return
        assert self._history_store is not None and run_id is not None
        counts = self._history_store.count_by_outcome(run_id)
        # Reserved samples use the runner's outcome vocabulary directly.
        self.history_outcome_chart.set_counts(counts)
        self.history_errors_list.clear()
        for error in self._history_store.list_errors(run_id):
            self.history_errors_list.addItem(
                f"[{error['category']}] {error['message']} (x{error['count']})"
            )

    def _load_history_run_into_results(self) -> None:
        run_id = self._selected_history_run_id()
        if run_id is None or self._history_store is None:
            return
        samples = self._history_store.list_samples(run_id)
        self.results_table.setRowCount(0)
        self._reset_live_metrics()
        self._loaded_history_run_id = run_id
        for sample in samples:
            self._append_result_row(sample)
            self._update_live_metrics(sample)
        self.run_summary_label.setText(f"Loaded {len(samples)} rows from run {run_id} (history).")
        self.steps.setCurrentIndex(3)

    def _export_history_run(self) -> None:
        run_id = self._selected_history_run_id()
        if run_id is None or self._history_store is None:
            return
        samples = self._history_store.list_samples(run_id)
        path, _ = QFileDialog.getSaveFileName(self, "Export run history", f"{run_id}.csv", "CSV files (*.csv)")
        if not path:
            return
        import csv as csv_module

        columns = (
            "run_id", "endpoint_id", "service", "method", "outcome", "status_code",
            "http_ms", "row_number", "correlation_key", "error_category", "error_message",
        )
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv_module.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for sample in samples:
                writer.writerow(sample)
