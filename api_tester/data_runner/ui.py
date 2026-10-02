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
from pathlib import Path
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
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
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
from api_tester.widgets import (
    SEARCH_TEXT_ROLE,
    EmptyStateWidget,
    Pager,
    SearchableComboBox,
    attach_table_empty_state,
)

from .charts import (
    LabeledBarChart,
    MultiSeriesLineChart,
    OutcomeBreakdownChart,
    PieChart,
    ScatterChart,
    SparklineChart,
)
from .csv_source import CsvImportSettings, CsvPreview, CsvSource
from .dashboard_widgets import ContextCard, StatCard, WizardStepper
from .mapping import ColumnMapping, MappingTarget, Transform, mapping_targets, template_request_values
from .planner import DataRunPlan, PlanIssue, build_plan
from .runner import DataRunner, DataRunnerOptions, RunSummary

_PREVIEW_ROW_LIMIT = 100
_PREVIEW_DEBOUNCE_MS = 350
_SOURCE_STACK_THRESHOLD = 1040
_SOURCE_SIDE_WIDTH = 460
#: Mapping-table column widths. Both editor columns previously used Qt's 100px
#: default, which elided the headers and the combo contents alike.
_MAPPING_COLUMN_WIDTH = 190
#: Sized from the widest target label the catalog produces — measured at 320px,
#: plus the combo's arrow, padding and border.
_MAPPING_TARGET_COLUMN_WIDTH = 400
#: Validation issue table: Row, Correlation key, Severity. Issue stretches.
_VALIDATION_COLUMN_WIDTHS = (90, 220, 110)
#: Row count up to which validation runs inline on the UI thread. Below this a
#: full pass finishes faster than a thread hand-off, and the progress bar would
#: only flicker; above it, validation moves to a worker.
_VALIDATION_SYNC_ROW_LIMIT = 2000

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


class _ResponsiveSplitter(QSplitter):
    """Lays two panes out side by side, stacking them when the available width
    drops below ``threshold``.

    Sizes are re-applied on every flip because a splitter's stored sizes are
    orientation-specific. Side by side the first pane is capped at
    ``side_width`` so a wide window grows the content pane rather than the
    settings form; stacked it gets its natural height so nothing clips.
    """

    _UNCONSTRAINED = 16777215

    def __init__(self, threshold: int, side_width: int, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._threshold = threshold
        self._side_width = side_width
        self._applied: Qt.Orientation | None = None
        self.setChildrenCollapsible(False)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_orientation()

    def _apply_orientation(self) -> None:
        if self.count() < 2:
            return
        wanted = (
            Qt.Orientation.Horizontal
            if self.width() >= self._threshold
            else Qt.Orientation.Vertical
        )
        if wanted == self._applied:
            return
        self._applied = wanted
        self.setOrientation(wanted)
        side = self.widget(0)
        if wanted == Qt.Orientation.Horizontal:
            side.setMaximumWidth(self._side_width)
            self.setSizes([self._side_width, max(1, self.width() - self._side_width)])
        else:
            side.setMaximumWidth(self._UNCONSTRAINED)
            natural = side.sizeHint().height()
            self.setSizes([natural, max(1, self.height() - natural)])


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


class ValidationWorker(QObject):
    """Builds a :class:`DataRunPlan` off the UI thread.

    Full-file validation is unbounded work, so it cannot run inline for large
    sources. Every signal carries the validation ``token`` it belongs to: a
    superseded worker keeps running until its next cancellation check, and the
    token is what stops its late result from overwriting a newer one.

    Slots are bound methods of the UI widget rather than lambdas on purpose —
    PyQt connects a plain callable *directly*, which would run the UI updates
    on this thread.
    """

    progressed = pyqtSignal(int, int, int)
    finished = pyqtSignal(object, int)
    failed = pyqtSignal(str, int)

    def __init__(self, build: Callable[[Callable[[int, int], None]], DataRunPlan], token: int) -> None:
        super().__init__()
        self._build = build
        self._token = token

    def run(self) -> None:
        try:
            plan = self._build(self._report)
            self.finished.emit(plan, self._token)
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
            self.failed.emit(str(exc), self._token)

    def _report(self, validated: int, total: int) -> None:
        self.progressed.emit(validated, total, self._token)


class DataRunnerTab(QWidget):
    """The Data Runner workspace tab: source, mapping, validate, run/results."""

    #: Emitted when a run stops, as ``(title, detail, ok)``. The tab stays
    #: free of any notification knowledge; the shell turns this into a bell
    #: entry that routes back here.
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
        self.current_targets: list[MappingTarget] = []
        self.csv_source: CsvSource | None = None
        self.preview: CsvPreview | None = None
        self.plan: DataRunPlan | None = None
        self._validate_summary_severity = "idle"
        self._environment_error: str | None = None
        # Background validation state. The token rises on every _validate()
        # call so a superseded worker's late result can be discarded.
        self._validation_token = 0
        self._validate_thread: QThread | None = None
        # Every live validation thread, not just the current one: a superseded
        # worker keeps running until its next cancellation check, and Qt aborts
        # if such a thread is still running at teardown.
        self._validation_jobs: dict[QThread, ValidationWorker] = {}
        self._validate_cancellation: CancellationController | None = None
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
        self.service_combo = SearchableComboBox(placeholder="Search services…")
        self.service_combo.setToolTip("Type to filter services")
        self.service_combo.setMinimumWidth(200)
        # currentIndexChanged, not currentTextChanged: on an editable combo
        # the latter fires on every keystroke of a search.
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
        layout.addLayout(endpoint_bar)

        # Request template handed over from API Explorer ("Open in Data
        # Runner"); every row starts from it and mapped CSV columns override.
        self._template_endpoint_id: str | None = None
        self._template_values: dict[str, str] = {}
        self._template_payload: Any = None
        self.template_banner = QFrame()
        self.template_banner.setObjectName("dataRunnerTemplateBanner")
        banner_layout = QHBoxLayout(self.template_banner)
        banner_layout.setContentsMargins(10, 6, 6, 6)
        self.template_banner_label = QLabel()
        self.template_banner_label.setWordWrap(True)
        banner_layout.addWidget(self.template_banner_label, 1)
        self.template_clear_button = QPushButton("Clear template")
        self.template_clear_button.setToolTip(
            "Stop using the API Explorer request as the base of every row."
        )
        self.template_clear_button.clicked.connect(self.clear_request_template)
        banner_layout.addWidget(self.template_clear_button)
        self.template_banner.hide()
        layout.addWidget(self.template_banner)

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

    def show_results(self) -> None:
        """Brings the run/results step forward.

        Used when a bell notification routes the user back to a run that
        finished while they were working somewhere else.
        """
        self.steps.setCurrentIndex(3)

    def _stepper_clicked(self, index: int) -> None:
        self.steps.setCurrentIndex(index)

    def _step_page_changed(self, index: int) -> None:
        self.stepper.set_current(index)
        self._refresh_stepper_progress()
        if index == 2:  # Validate
            # Validation is cheap (a sampled resolve) and its result depends on
            # the source, mapping and environment alike, so it is always re-run
            # on entry rather than tracking which of them changed.
            self._validate(interactive=False)
        elif index == 3:  # Execute & review
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
        # The summary's colour is a per-widget stylesheet holding a resolved
        # hex value, so a palette switch has to re-resolve it.
        self._set_validate_summary(
            self.validate_summary_label.text(), self._validate_summary_severity
        )

    def _icon_buttons(self) -> list[tuple[QPushButton, str]]:
        pairs = [
            (self.browse_button, "folder"),
            (self.add_mapping_button, "add"),
            (self.remove_mapping_button, "trash"),
            (self.auto_map_button, "wand"),
            (self.run_button, "play"),
            (self.stop_button, "stop"),
            (self.validate_cancel_button, "stop"),
            (self.export_button, "export"),
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
        # AllNonFixedFieldsGrow, not ExpandingFieldsGrow: the latter only grows
        # fields whose size policy is Expanding, which left the Preferred combos
        # and Minimum spinboxes at four different widths.
        options_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
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
        options_form.addRow("", self._build_preview_hint())

        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)
        preview_layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setSpacing(8)
        self.preview_summary_label = QLabel("No CSV loaded yet.")
        self.preview_summary_label.setWordWrap(True)
        header_row.addWidget(self.preview_summary_label, 1)
        header_row.addWidget(self._build_pager())
        preview_layout.addLayout(header_row)

        self.sample_rows_table = QTableWidget(0, 0)
        self.sample_rows_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        # A wide CSV must scroll rather than squeeze its columns, so the header
        # keeps per-column widths and both scrollbars stay available.
        self.sample_rows_table.horizontalHeader().setStretchLastSection(False)
        self.sample_rows_table.setHorizontalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.sample_rows_table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.sample_rows_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.sample_rows_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.sample_rows_table.setWordWrap(False)
        self.sample_rows_table.setAlternatingRowColors(True)
        attach_table_empty_state(
            self.sample_rows_table,
            icon_name="eye",
            title="No CSV loaded",
            guidance="Choose a CSV file above and its first rows appear here automatically.",
        )
        preview_layout.addWidget(self.sample_rows_table, 1)

        self.source_splitter = _ResponsiveSplitter(_SOURCE_STACK_THRESHOLD, _SOURCE_SIDE_WIDTH)
        self.source_splitter.addWidget(options_group)
        self.source_splitter.addWidget(preview_group)
        self.source_splitter.setStretchFactor(0, 0)
        self.source_splitter.setStretchFactor(1, 1)
        layout.addWidget(self.source_splitter, 1)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(_PREVIEW_DEBOUNCE_MS)
        self._preview_timer.timeout.connect(self._auto_load_preview)
        self._last_preview_settings: CsvImportSettings | None = None
        self._wire_preview_auto_reload()

        return page

    def _build_pager(self) -> QWidget:
        """The shared split-pill pager; it hides itself on a single-page file."""
        self.preview_pager = Pager(page_size=_PREVIEW_ROW_LIMIT)
        self.preview_pager.page_changed.connect(self._load_preview_page)
        return self.preview_pager

    def _load_preview_page(self, page: int) -> None:
        """Re-reads one window from the already-configured source.

        The ``CsvSource`` is reused so paging never rebuilds the import
        settings; the ``Pager`` has already clamped ``page`` into range.
        """
        if self.csv_source is None:
            return
        try:
            preview = self.csv_source.preview(
                sample_rows=_PREVIEW_ROW_LIMIT, row_offset=page * _PREVIEW_ROW_LIMIT
            )
        except OSError as exc:
            self._clear_preview(f"Could not read CSV: {exc}")
            return
        self.preview = preview
        self._render_preview(preview)

    def _build_preview_hint(self) -> QLabel:
        hint = QLabel(
            f"The preview loads {_PREVIEW_ROW_LIMIT} rows per page. "
            "The full file is used when the run executes."
        )
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        return hint

    def _wire_preview_auto_reload(self) -> None:
        """Any change to the path or the import settings re-reads the preview.

        Every signal lands on one debounce timer and ``_auto_load_preview``
        skips work when the resulting settings match the last load, so holding
        a spinbox arrow or typing a path costs a single read.
        """
        self.csv_path.textChanged.connect(self._schedule_preview)
        self.encoding_combo.currentIndexChanged.connect(self._schedule_preview)
        self.delimiter_combo.currentIndexChanged.connect(self._schedule_preview)
        self.quotechar_edit.textChanged.connect(self._schedule_preview)
        self.has_header_checkbox.toggled.connect(self._schedule_preview)
        self.skip_blank_checkbox.toggled.connect(self._schedule_preview)
        self.start_row_spin.valueChanged.connect(self._schedule_preview)
        self.max_rows_spin.valueChanged.connect(self._schedule_preview)

    def _schedule_preview(self, *_args: object) -> None:
        self._preview_timer.start()

    def _auto_load_preview(self) -> None:
        settings = self._current_import_settings()
        if not settings.path:
            self._clear_preview("No CSV loaded yet.")
            return
        if settings == self._last_preview_settings:
            return
        self._load_preview(interactive=False)

    def _clear_preview(self, message: str) -> None:
        self.csv_source = None
        self.preview = None
        self._last_preview_settings = None
        self.sample_rows_table.setRowCount(0)
        self.sample_rows_table.setColumnCount(0)
        self.preview_summary_label.setText(message)
        self.preview_pager.reset()
        self._refresh_stepper_progress()

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

    def _load_preview(self, *_args: object, interactive: bool = True) -> None:
        settings = self._current_import_settings()
        if not settings.path:
            if interactive:
                QMessageBox.information(self, "Choose a CSV file", "Enter or browse for a CSV file first.")
            else:
                self._clear_preview("No CSV loaded yet.")
            return
        self.csv_source = CsvSource(settings)
        try:
            self.preview = self.csv_source.preview(sample_rows=_PREVIEW_ROW_LIMIT)
        except OSError as exc:
            # Auto-reload fires while a path is still being typed, so a failure
            # there reports inline instead of interrupting with a modal.
            if interactive:
                QMessageBox.warning(self, "Could not read CSV", str(exc))
            self._clear_preview(f"Could not read CSV: {exc}")
            return
        self._last_preview_settings = settings
        # Resets to page 1, which is what a new file or new import settings mean.
        self.preview_pager.set_total(
            self.preview.total_rows_available, exact=self.preview.total_is_exact
        )
        self._render_preview(self.preview)
        self._refresh_mapping_column_choices()
        self._refresh_stepper_progress()

    def _render_preview(self, preview: CsvPreview) -> None:
        total = preview.total_rows_available
        total_text = f"{total}+" if not preview.total_is_exact else str(total)
        summary = (
            f"{len(preview.columns)} columns · {total_text} rows · "
            f"{preview.blank_row_count} blank · {preview.duplicate_row_count} duplicate"
        )
        shown = len(preview.rows)
        if total > _PREVIEW_ROW_LIMIT:
            first = preview.row_offset + 1 if shown else preview.row_offset
            summary += f" — showing rows {first}-{preview.row_offset + shown}"
        else:
            summary += f" — showing all {shown} rows"
        self.preview_summary_label.setText(summary)

        self.sample_rows_table.setUpdatesEnabled(False)
        try:
            self.sample_rows_table.setColumnCount(len(preview.columns))
            self.sample_rows_table.setHorizontalHeaderLabels(list(preview.columns))
            self.sample_rows_table.setRowCount(len(preview.rows))
            self.sample_rows_table.setVerticalHeaderLabels(
                [str(preview.row_offset + index + 1) for index in range(len(preview.rows))]
            )
            for row_index, row in enumerate(preview.rows):
                for column_index, column in enumerate(preview.columns):
                    self.sample_rows_table.setItem(row_index, column_index, QTableWidgetItem(row.get(column, "")))
            self.sample_rows_table.resizeColumnsToContents()
        finally:
            self.sample_rows_table.setUpdatesEnabled(True)

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
        # Qt's 100px default truncated both headers and their combo contents.
        # "Maps to" holds the widest text in the table — target labels such as
        # "Filter - Id (eq, neq, gt, lt, gte, lte, in, nin)" - so it gets the
        # larger share; Transforms still stretches into whatever is left.
        # A header-wide minimum section size is deliberately not set: it would
        # also apply to the two icon-button columns sized to their contents.
        self.mapping_table.setColumnWidth(0, _MAPPING_COLUMN_WIDTH)
        self.mapping_table.setColumnWidth(1, _MAPPING_TARGET_COLUMN_WIDTH)
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
        # Target labels can outrun even the widened column, and the combo hard
        # cuts rather than eliding, so the full text stays available on hover.
        target_combo.currentTextChanged.connect(target_combo.setToolTip)
        target_combo.setToolTip(target_combo.currentText())
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

    def _service_index_changed(self, index: int) -> None:
        self._service_changed(self.service_combo.itemText(index) if index >= 0 else "")

    def _service_changed(self, service_name: str) -> None:
        self.endpoint_combo.blockSignals(True)
        self.endpoint_combo.clear()
        service = next((s for s in self.catalog.services if s.name == service_name), None)
        if service is not None:
            for endpoint in service.endpoints:
                self.endpoint_combo.addItem(f"{endpoint.method} {endpoint.path}", endpoint)
                self.endpoint_combo.setItemData(
                    self.endpoint_combo.count() - 1,
                    f"{endpoint.method} {endpoint.path} {endpoint.controller} {endpoint.action}",
                    SEARCH_TEXT_ROLE,
                )
        if self.endpoint_combo.count():
            self.endpoint_combo.setCurrentIndex(0)
        self.endpoint_combo.blockSignals(False)
        self._endpoint_changed(self.endpoint_combo.currentIndex())

    def _endpoint_changed(self, index: int) -> None:
        endpoint = self.endpoint_combo.itemData(index) if index >= 0 else None
        self.current_endpoint = endpoint
        if self._template_endpoint_id is not None and (
            endpoint is None or endpoint.id != self._template_endpoint_id
        ):
            self.clear_request_template()
        self.current_targets = mapping_targets(endpoint, self.catalog) if endpoint is not None else []
        for widgets in getattr(self, "_mapping_rows", {}).values():
            selected = widgets.target.currentData()
            self._populate_target_combo(widgets.target, selected)

    # ------------------------------------------------------------------ request template

    def load_request(self, endpoint: Endpoint, values: dict[str, str], payload: Any) -> bool:
        """Selects ``endpoint`` and makes the given request the base template
        for every row. Returns False when the endpoint is not in the catalog."""
        service_index = self.service_combo.findText(endpoint.service)
        if service_index < 0:
            return False
        if self.service_combo.currentIndex() != service_index:
            self.service_combo.setCurrentIndex(service_index)
        endpoint_index = next(
            (
                i
                for i in range(self.endpoint_combo.count())
                if getattr(self.endpoint_combo.itemData(i), "id", None) == endpoint.id
            ),
            -1,
        )
        if endpoint_index < 0:
            return False
        if self.endpoint_combo.currentIndex() != endpoint_index:
            self.endpoint_combo.setCurrentIndex(endpoint_index)
        self._template_endpoint_id = endpoint.id
        self._template_values = template_request_values(values)
        self._template_payload = json.loads(json.dumps(payload)) if payload is not None else None
        self._template_changed()
        self.steps.setCurrentIndex(0)
        return True

    def clear_request_template(self) -> None:
        had_template = self._template_endpoint_id is not None
        self._template_endpoint_id = None
        self._template_values = {}
        self._template_payload = None
        if had_template:
            self._template_changed()

    def has_request_template(self) -> bool:
        return self._template_endpoint_id is not None

    def _template_plan_kwargs(self) -> dict[str, Any]:
        if self._template_endpoint_id is None:
            return {}
        return {
            "template_values": dict(self._template_values),
            "template_payload": self._template_payload,
        }

    def _template_summary(self) -> str:
        count = len(self._template_values)
        parts = [f"{count} value{'s' if count != 1 else ''}"]
        if self._template_payload is not None:
            parts.append("payload")
        return " · ".join(parts)

    def _template_changed(self) -> None:
        self.plan = None
        if self._template_endpoint_id is None:
            self.template_banner.hide()
        else:
            self.template_banner_label.setText(
                "<b>Request template from API Explorer</b> · "
                f"{self._template_summary()} — every row starts from this request; "
                "mapped CSV columns override it."
            )
            self.template_banner.show()
        self._refresh_stepper_progress()
        if self.steps.currentIndex() == 3:
            self._refresh_run_context_panel()

    # ------------------------------------------------------------------ validate

    def _build_validate_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        # No Validate button: the page validates on entry, so a button would
        # only ever re-run work that has already happened.
        self.validate_summary_label = QLabel("Not validated yet.")
        self.validate_summary_label.setWordWrap(True)
        layout.addWidget(self.validate_summary_label)

        # Progress is only shown while a large file is validating on the
        # worker thread; small files finish inline and never reveal this row.
        self.validate_progress_row = QWidget()
        progress_layout = QHBoxLayout(self.validate_progress_row)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        self.validate_progress_bar = QProgressBar()
        self.validate_progress_bar.setRange(0, 100)
        self.validate_progress_bar.setTextVisible(True)
        progress_layout.addWidget(self.validate_progress_bar, 1)
        self.validate_cancel_button = _icon_button("stop", "Cancel validation", danger=True)
        self.validate_cancel_button.clicked.connect(self._cancel_validation)
        progress_layout.addWidget(self.validate_cancel_button)
        self.validate_progress_row.setVisible(False)
        layout.addWidget(self.validate_progress_row)

        self.preview_rows_table = QTableWidget(0, 4)
        self.preview_rows_table.setHorizontalHeaderLabels(["Row", "Correlation key", "Severity", "Issue"])
        header = self.preview_rows_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for column, width in enumerate(_VALIDATION_COLUMN_WIDTHS):
            self.preview_rows_table.setColumnWidth(column, width)
        self.preview_rows_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.validate_empty_state = attach_table_empty_state(
            self.preview_rows_table,
            icon_name="verify",
            title="Nothing validated yet",
            guidance="Load a CSV and map its columns; this page checks them automatically.",
        )
        layout.addWidget(self.preview_rows_table, 1)

        return page

    def _build_environment(self, *, interactive: bool = True) -> ExecutionEnvironmentSnapshot | None:
        """Resolves the active environment, or ``None`` with the reason shown.

        ``interactive=False`` suppresses the modal: validation now runs simply
        because the user entered the step, and a dialog triggered by
        navigation would be hostile. The caller reports the failure in the
        summary line instead.
        """
        try:
            return self.environment_provider()
        except Exception as exc:  # noqa: BLE001
            self._environment_error = str(exc)
            if interactive:
                QMessageBox.warning(self, "Environment unavailable", str(exc))
            return None

    def _validate(self, *, interactive: bool = True) -> None:
        """Rebuilds the plan from the current source, mapping and environment.

        Entering the Validate step calls this with ``interactive=False``: a
        modal dialog fired by merely navigating to a tab would be hostile, so
        the blocking reason is reported in the summary line instead.

        Every row is validated. Small files run inline so the call is
        synchronous and the result is available immediately; larger ones are
        handed to :class:`ValidationWorker` with a progress bar.
        """
        self._cancel_validation()
        self._validation_token += 1
        blocker: str | None = None
        if self.current_endpoint is None:
            blocker = "Select a service and endpoint to validate."
        elif self.csv_source is None:
            blocker = "Load a CSV on the Source tab to validate it."
        if blocker is not None:
            if interactive:
                QMessageBox.information(self, "Cannot validate yet", blocker)
            self.plan = None
            self._clear_plan_render(blocker)
            return
        self._environment_error = None
        environment = self._build_environment(interactive=interactive)
        if environment is None:
            self.plan = None
            reason = self._environment_error or "Environment unavailable"
            self._clear_plan_render(f"{reason}; validation did not run.")
            return
        mappings = self._current_mappings()
        expected_status = self.default_expected_status.text().strip() or "200-299"
        template_kwargs = self._template_plan_kwargs()

        def build(
            progress: Callable[[int, int], None] | None,
            cancellation: CancellationController | None,
        ) -> DataRunPlan:
            return build_plan(
                self.current_endpoint,
                self.catalog,
                self.csv_source,
                mappings,
                environment,
                default_expected_status=expected_status,
                progress=progress,
                cancellation=cancellation,
                **template_kwargs,
            )

        if self._should_validate_inline():
            self._validation_finished(build(None, None), self._validation_token)
            return
        self._start_background_validation(build)

    def _should_validate_inline(self) -> bool:
        """True when the row count is small enough to resolve on the UI thread.

        Uses the Source step's already-computed preview counts rather than
        counting the file again here, which would itself block the UI.
        """
        preview = self.preview
        if preview is None:
            return True
        if not preview.total_is_exact:
            return False
        return preview.total_rows_available <= _VALIDATION_SYNC_ROW_LIMIT

    def _start_background_validation(
        self,
        build: Callable[
            [Callable[[int, int], None] | None, CancellationController | None], DataRunPlan
        ],
    ) -> None:
        cancellation = CancellationController()
        self._validate_cancellation = cancellation
        self.validate_progress_bar.setRange(0, 100)
        self.validate_progress_bar.setValue(0)
        self.validate_progress_bar.setFormat("Validating…")
        self.validate_progress_row.setVisible(True)
        self.validate_cancel_button.setEnabled(True)
        self._set_validate_summary("Validating every row…", "idle")
        # Without this the placeholder still reads "Nothing validated yet"
        # while a quarter-million rows are visibly being validated.
        self.validate_empty_state.set_content(
            icon_name="verify",
            title="Validating every row…",
            guidance="Problem rows appear here as soon as the check finishes.",
        )
        self.run_button.setEnabled(False)

        # The controller is bound here rather than read from ``self`` inside
        # the worker: cancelling clears the attribute, and a worker that had
        # not yet started would otherwise read None and ignore the stop.
        def run_build(progress: Callable[[int, int], None] | None) -> DataRunPlan:
            return build(progress, cancellation)

        thread = QThread()
        worker = ValidationWorker(run_build, self._validation_token)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progressed.connect(self._validation_progressed)
        worker.finished.connect(self._validation_finished)
        worker.failed.connect(self._validation_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._cleanup_validation_thread)
        self._validate_thread = thread
        self._validation_jobs[thread] = worker
        thread.start()

    def _validation_progressed(self, validated: int, total: int, token: int) -> None:
        if token != self._validation_token or total <= 0:
            return
        self.validate_progress_bar.setValue(int(validated * 100 / total))
        self.validate_progress_bar.setFormat(f"Validating {validated:,} of {total:,} rows (%p%)")

    def _validation_finished(self, plan: DataRunPlan, token: int) -> None:
        if token != self._validation_token:
            return
        self.validate_progress_row.setVisible(False)
        self.plan = plan
        self._render_plan(plan)
        self._refresh_stepper_progress()

    def _validation_failed(self, message: str, token: int) -> None:
        if token != self._validation_token:
            return
        self.validate_progress_row.setVisible(False)
        self.plan = None
        self._clear_plan_render(f"Validation failed: {message}")

    def _cancel_validation(self) -> None:
        """Stops an in-flight background validation, if any.

        Also called before starting a new validation: re-entering the step
        while a large file is still validating would otherwise leave two
        workers racing to render into the same table.
        """
        if self._validate_cancellation is not None:
            self._validate_cancellation.stop()
            self._validate_cancellation = None
        self.validate_cancel_button.setEnabled(False)

    def _cleanup_validation_thread(self) -> None:
        """Disposes of the thread that just finished, current or superseded."""
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        worker = self._validation_jobs.pop(thread, None)
        if worker is not None:
            worker.deleteLater()
        thread.deleteLater()
        if thread is self._validate_thread:
            self._validate_thread = None

    def shutdown(self) -> None:
        """Stops background validation so Qt never destroys a running thread.

        Validation of a large file can still be in flight when the window
        closes, and a superseded worker may be draining too; the cancellation
        check is per row, so each wait is short.
        """
        self._cancel_validation()
        for thread in list(self._validation_jobs):
            if thread.isRunning():
                thread.quit()
                thread.wait(2000)

    def _clear_plan_render(self, message: str) -> None:
        """Shows why no plan exists, leaving the issue table empty."""
        self._set_validate_summary(message, "blocked")
        self.preview_rows_table.setRowCount(0)
        self.validate_empty_state.set_content(
            icon_name="verify",
            title="Nothing to validate yet",
            guidance=message,
        )
        self.run_button.setEnabled(False)

    def _set_validate_summary(self, text: str, severity: str) -> None:
        """Colours the summary by outcome so status is readable at a glance."""
        self._validate_summary_severity = severity
        self.validate_summary_label.setText(text)
        colour = {
            "error": theme.FAIL,
            "warning": theme.WARN,
            "ok": theme.PASS,
        }.get(severity, theme.TEXT_MUTED)
        self.validate_summary_label.setStyleSheet(f"color: {colour}; font-weight: 600;")

    def _render_plan(self, plan: DataRunPlan) -> None:
        validated = plan.validated_row_count
        counts = (
            f"{plan.total_row_count:,} total rows · {plan.valid_count:,} valid · "
            f"{plan.invalid_count:,} invalid · {plan.skip_count:,} skip"
        )
        if validated < plan.total_row_count:
            counts = f"{counts} (validated {validated:,})"

        # Plan-level problems and per-row problems share one table: both block
        # or degrade the same run, and splitting them made the user check two
        # places. Valid rows are deliberately omitted - a list of "Valid, 0
        # issues" rows hid the handful of rows that actually need attention.
        rows: list[tuple[str, str, str, str]] = []
        for issue in plan.issues:
            rows.append(("Plan", "—", issue.severity.capitalize(), issue.message))
        for issue_row in plan.issue_rows:
            severity = "Skip" if issue_row.skip else "Invalid"
            message = "; ".join(issue_row.issue_messages) or (
                "Row skipped by a skip-if transform." if issue_row.skip else "Row is not valid."
            )
            rows.append((str(issue_row.row_number), issue_row.correlation_key, severity, message))
        if plan.issue_rows_truncated:
            rows.append(
                (
                    "…",
                    "—",
                    "Warning",
                    f"Only the first {len(plan.issue_rows):,} problem rows are listed; "
                    "the counts above cover every row.",
                )
            )

        self.preview_rows_table.setRowCount(len(rows))
        for index, (row_label, key, severity, message) in enumerate(rows):
            severity_item = QTableWidgetItem(severity)
            if severity in ("Error", "Invalid"):
                severity_item.setForeground(QColor(theme.FAIL))
            elif severity in ("Warning", "Skip"):
                severity_item.setForeground(QColor(theme.WARN))
            message_item = QTableWidgetItem(message)
            message_item.setToolTip(message)
            self.preview_rows_table.setItem(index, 0, QTableWidgetItem(row_label))
            self.preview_rows_table.setItem(index, 1, QTableWidgetItem(key))
            self.preview_rows_table.setItem(index, 2, severity_item)
            self.preview_rows_table.setItem(index, 3, message_item)

        # The counts alone cannot explain a red summary: a plan can be blocked
        # (no base URL) while every row resolves cleanly. Lead with the
        # blocker so the colour always has a stated reason.
        blocking = sum(1 for issue in plan.issues if issue.severity == "error")
        warnings = len(plan.issues) - blocking
        if blocking:
            lead = f"{blocking} blocking issue{'s' if blocking > 1 else ''}"
            self._set_validate_summary(f"{lead} · {counts}", "error")
        elif plan.invalid_count:
            self._set_validate_summary(
                f"{plan.invalid_count:,} invalid rows · {counts}", "error"
            )
        elif rows:
            lead = f"{warnings} warning{'s' if warnings != 1 else ''}" if warnings else "Rows skipped"
            self._set_validate_summary(f"{lead} · {counts}", "warning")
        else:
            self._set_validate_summary(f"Ready to run · {counts}", "ok")
            self.validate_empty_state.set_content(
                icon_name="verify",
                title="No validation issues",
                guidance=(
                    f"All {validated:,} rows resolved cleanly against "
                    f"{plan.endpoint.method} {plan.endpoint.path} and are ready to run."
                ),
            )

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
        # Shown only while a run is in flight, matching the Validate step: an
        # idle bar reads as a stalled run and is just an empty box otherwise.
        self.run_progress_bar.setVisible(False)
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

        # Export lives in the tab bar's right corner rather than in a tab of
        # its own: it is an action on the results, not another view of them.
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 6, 0)
        corner_layout.setSpacing(6)
        self.export_button = _icon_button("export", "Export row results to CSV")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self._export_results)
        corner_layout.addWidget(self.export_button)
        self.results_tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
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
        pagination_row.setSpacing(8)
        pagination_row.addWidget(QLabel("Rows per page:"))
        self.results_page_size = QComboBox()
        self.results_page_size.addItems(["100", "250", "500", "1000", "5000"])
        self.results_page_size.setCurrentText("500")
        self.results_page_size.currentTextChanged.connect(self._on_page_size_changed)
        pagination_row.addWidget(self.results_page_size)
        pagination_row.addStretch(1)
        # The shared split-pill pager, so results page the same way the CSV
        # preview and every other paged table in the app do.
        self.results_pager = Pager(page_size=self._results_page_size)
        self.results_pager.page_changed.connect(self._on_results_page_changed)
        pagination_row.addWidget(self.results_pager)
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

        # Two pages rather than an empty editor: with nothing selected the
        # read-only editors rendered as a large blank panel, which read as a
        # broken view instead of an intentional empty state.
        self.row_inspector_stack = QStackedWidget()
        self.row_inspector_stack.setObjectName("dataRunnerTransparentPane")

        self.row_inspector_empty = EmptyStateWidget()
        self.row_inspector_empty.set_content(
            icon_name="eye",
            title="Nothing to inspect",
            guidance="Select a row on the left to see its resolved request, response, and assertions.",
        )
        self.row_inspector_stack.addWidget(self.row_inspector_empty)

        content = QWidget()
        content.setObjectName("dataRunnerTransparentPane")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        self.row_inspector_hint = QLabel("Select a row to inspect its request/response.")
        self.row_inspector_hint.setObjectName("dataRunnerStatCardSubtitle")
        self.row_inspector_hint.setWordWrap(True)
        content_layout.addWidget(self.row_inspector_hint)

        self.row_inspector_tabs = QTabWidget()
        self.row_inspector_response = JsonTextEdit(read_only=True)
        self.row_inspector_request = JsonTextEdit(read_only=True)
        self.row_inspector_input_row = JsonTextEdit(read_only=True)
        self.row_inspector_assertions = JsonTextEdit(read_only=True)
        self.row_inspector_tabs.addTab(self.row_inspector_response, "Response")
        self.row_inspector_tabs.addTab(self.row_inspector_request, "Resolved request")
        self.row_inspector_tabs.addTab(self.row_inspector_input_row, "Input row")
        self.row_inspector_tabs.addTab(self.row_inspector_assertions, "Assertions")
        content_layout.addWidget(self.row_inspector_tabs, 1)
        self.row_inspector_stack.addWidget(content)

        self.row_inspector_stack.setCurrentWidget(self.row_inspector_empty)
        box_layout.addWidget(self.row_inspector_stack, 1)
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
        # Scrolled: the six charts cannot all meet their minimum heights in a
        # short window, and without this the group boxes squeeze below their
        # minimum and clip the plots rather than letting the user scroll.
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setSpacing(10)

        top_row = QHBoxLayout()
        outcome_box = QGroupBox("Outcome breakdown")
        outcome_layout = QVBoxLayout(outcome_box)
        self.outcome_chart = OutcomeBreakdownChart()
        outcome_layout.addWidget(self.outcome_chart, 1)
        top_row.addWidget(outcome_box, 1)

        throughput_box = QGroupBox("Throughput (rows completed)")
        throughput_layout = QVBoxLayout(throughput_box)
        self.cumulative_completed_chart = MultiSeriesLineChart(
            (("Completed", theme.PRIMARY),), unit="", style="bar"
        )
        throughput_layout.addWidget(self.cumulative_completed_chart, 1)
        top_row.addWidget(throughput_box, 1)

        latency_box = QGroupBox("Latency (ms)")
        latency_layout = QVBoxLayout(latency_box)
        self.latency_chart = SparklineChart(mode="bar", color=theme.ACCENT, dotted_grid=True)
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
        self.error_category_chart = PieChart()
        errors_layout.addWidget(self.error_category_chart)
        bottom_row.addWidget(errors_box, 1)

        latency_timeline_box = QGroupBox("Latency by row (spot slow clusters)")
        timeline_layout = QVBoxLayout(latency_timeline_box)
        self.latency_timeline_chart = ScatterChart(color=theme.ACCENT, unit=" ms")
        timeline_layout.addWidget(self.latency_timeline_chart)
        bottom_row.addWidget(latency_timeline_box, 2)
        tab_layout.addLayout(bottom_row, 1)

        self.charts_scroll = QScrollArea()
        self.charts_scroll.setWidgetResizable(True)
        self.charts_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.charts_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.charts_scroll.setWidget(tab)
        return self.charts_scroll

    def _build_mapping_summary_tab(self) -> QWidget:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.addWidget(QLabel("The column -> target mapping used for this run."))
        self.mapping_summary_table = QTableWidget(0, 3)
        self.mapping_summary_table.setHorizontalHeaderLabels(["CSV column", "Target", "Transforms"])
        self.mapping_summary_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.mapping_summary_table.horizontalHeader().setStretchLastSection(True)
        attach_table_empty_state(
            self.mapping_summary_table,
            icon_name="data-runner",
            title="No mapping yet",
            guidance="Map CSV columns to request fields, then run to see them here.",
        )
        tab_layout.addWidget(self.mapping_summary_table, 1)
        return tab

    def _build_persist_card(self) -> ContextCard:
        """The run-history card on the left rail.

        Deliberately never passed through ``_refresh_run_context_panel``:
        ``ContextCard.clear_body`` calls ``deleteLater`` on its children, which
        would destroy these live controls and leave dangling references.
        """
        card = ContextCard("Run history")
        self.persist_checkbox = QCheckBox("Persist this run")
        self.persist_checkbox.setToolTip(
            "Store this run's rows in a local database file so it appears in the History step"
        )
        self.persist_checkbox.toggled.connect(self._on_persist_toggled)
        card.body.addWidget(self.persist_checkbox)

        self.persist_off_label = QLabel("Off — results are kept in memory only.")
        self.persist_off_label.setObjectName("dataRunnerStatCardSubtitle")
        self.persist_off_label.setWordWrap(True)
        card.body.addWidget(self.persist_off_label)

        # Collapsed until the user opts in, so the card costs one row at rest.
        self.persist_detail = QWidget()
        # Bare QWidgets inherit the global `QWidget { background-color }` rule,
        # which painted a grey block over the white card.
        self.persist_detail.setObjectName("dataRunnerTransparentPane")
        detail_layout = QVBoxLayout(self.persist_detail)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(4)

        self.persist_path = QLineEdit()
        self.persist_path.setPlaceholderText("Defaults next to the CSV")
        self.persist_path.setToolTip("Path to the .db file that stores this run")
        self.persist_path.textChanged.connect(self._refresh_persist_status)
        detail_layout.addWidget(self.persist_path)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        self.persist_status_label = QLabel("")
        self.persist_status_label.setObjectName("dataRunnerStatCardSubtitle")
        status_row.addWidget(self.persist_status_label, 1)
        self.persist_browse_button = QPushButton("Browse…")
        self.persist_browse_button.setToolTip(
            "Choose where to store the run history database"
        )
        self.persist_browse_button.clicked.connect(self._browse_persist_path)
        status_row.addWidget(self.persist_browse_button)
        detail_layout.addLayout(status_row)

        self.persist_detail.setVisible(False)
        card.body.addWidget(self.persist_detail)
        return card

    def _on_persist_toggled(self, checked: bool) -> None:
        self.persist_detail.setVisible(checked)
        self.persist_off_label.setVisible(not checked)
        if checked and not self.persist_path.text().strip():
            self.persist_path.setPlaceholderText(
                self._default_persist_path() or "Defaults next to the CSV"
            )
        self._refresh_persist_status()

    def _refresh_persist_status(self) -> None:
        """Reports where the run will be written and whether that file exists."""
        if not self.persist_checkbox.isChecked():
            return
        path = self.persist_path.text().strip() or self._default_persist_path()
        if not path:
            self.persist_status_label.setText("Size · load a CSV first")
            return
        existing = Path(path)
        if existing.exists():
            size_mb = existing.stat().st_size / (1024 * 1024)
            self.persist_status_label.setText(f"Size · {size_mb:.1f} MB")
        else:
            self.persist_status_label.setText("Size · new file")

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
        panel_layout.addWidget(self._build_persist_card())
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
            service_caption = QLabel(self.service_combo.itemText(self.service_combo.currentIndex()))
            service_caption.setObjectName("dataRunnerStatCardSubtitle")
            self.context_card_template.body.addWidget(service_caption)
            if self.has_request_template():
                template_caption = QLabel(f"From API Explorer · {self._template_summary()}")
                template_caption.setObjectName("dataRunnerStatCardSubtitle")
                template_caption.setWordWrap(True)
                self.context_card_template.body.addWidget(template_caption)
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
        self._error_category_counts = {}
        self._cumulative_completed = 0
        self._row_details = {}
        self._loaded_history_run_id = None
        self._result_rows = []
        self._current_results_page = 0
        self._follow_latest_page = True
        self.results_table.setRowCount(0)
        self._update_pagination_controls()
        self.latency_chart.clear()
        self.latency_summary_label.setText("min – / mean – / p50 – / p90 – / p99 – / max –")
        self.cumulative_completed_chart.clear()
        self.latency_percentile_chart.set_entries({})
        self.error_category_chart.set_entries({})
        self.latency_timeline_chart.clear()
        for card in self.stat_cards.values():
            card.set_value("–")
            card.set_subtitle("")
        self.run_progress_bar.setValue(0)
        self.run_progress_bar.setVisible(False)
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
            QMessageBox.information(
                self,
                "Cannot run",
                "The current plan is not executable. Open the Validate step to see why.",
            )
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
            **self._template_plan_kwargs(),
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
        self.run_progress_bar.setVisible(True)
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
        total_rows = len(self._result_rows)
        # set_total rewinds to page 1, so the current page is restored right
        # after — a live run appends rows constantly and must not jump back.
        self.results_pager.set_total(total_rows, page_size=self._results_page_size)
        self.results_pager.set_page(self._current_results_page, emit=False)
        self._current_results_page = self.results_pager.page

    def _on_results_page_changed(self, page: int) -> None:
        self._current_results_page = page
        self._follow_latest_page = page == self._total_results_pages() - 1
        self._render_current_page()

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
        self.results_pager.set_page(self._current_results_page - 1)

    def _go_to_next_page(self) -> None:
        if self._current_results_page >= self._total_results_pages() - 1:
            return
        self.results_pager.set_page(self._current_results_page + 1)

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
        self.row_inspector_empty.set_content(
            icon_name="eye",
            title="Nothing to inspect",
            guidance="Select a row on the left to see its resolved request, response, and assertions.",
        )
        self.row_inspector_stack.setCurrentWidget(self.row_inspector_empty)
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
            # Row selected, but nothing captured — still an empty state rather
            # than four blank editors.
            self.row_inspector_empty.set_content(
                icon_name="eye",
                title=f"No detail for row {row_number}",
                guidance="This run predates detail capture, so its request and response were not stored.",
            )
            self.row_inspector_stack.setCurrentWidget(self.row_inspector_empty)
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
        self.row_inspector_stack.setCurrentIndex(1)
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
        # The run may have just created the database, so the card's size
        # readout is stale until it is recomputed.
        self._refresh_persist_status()
        self._refresh_stepper_progress()
        self.run_completed.emit(
            "Data Runner finished",
            f"{summary.outcome} — {summary.passed} passed, {summary.failed} failed",
            summary.failed == 0 and summary.errored == 0,
        )

    def _run_failed(self, message: str) -> None:
        self._event_timer.stop()
        self.run_summary_label.setText("Run failed.")
        self.run_completed.emit("Data Runner failed", message.strip(), False)
        QMessageBox.warning(self, "Data Runner failed", message)

    def _cleanup_thread(self) -> None:
        self.run_button.setEnabled(True)
        self.pause_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        # Reached for every ending — finished, failed, or cancelled — so the
        # bar never lingers after the run stops.
        self.run_progress_bar.setVisible(False)
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
