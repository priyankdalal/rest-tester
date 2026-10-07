"""Test-suite authoring, execution, and analysis UI."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, unquote_plus

from PyQt6 import sip
from PyQt6.QtCore import QObject, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSplitter,
    QStackedLayout,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .catalog import Catalog, Endpoint
from .comparison import ResponseComparison
from .icons import icon
from .runner import CaseResult, SuiteResult, append_suite_history, export_report, run_suite
from .request_auth import SECRET_HEADER_NAMES, populate_auth_choices
from .authentication import AuthError
from .request_editor import RequestEditor
from .seeding import seed_parameter
from .suite import (
    ASSERTION_KINDS,
    Assertion,
    Capture,
    TestCase,
    TestSuite,
    clone_case,
    load_suite,
    save_suite,
    suggest_case_name,
)
from .suite_timeline import SuiteTimelineTab
from .visualizer import OUTCOME_COLORS, RunVisualizer
from .viewers import ResponseViewer
from .widgets import (
    AccordionScrollArea,
    EmptyStateWidget,
    attach_table_empty_state,
    expected_status_combo,
    inset_shadow_detail_pane,
)
from .workspace_store import WorkspaceStore, as_store

CAPTURE_SOURCES = ["body", "header", "status"]


class SuiteWorker(QObject):
    """Runs a suite off the UI thread, streaming each case result back."""

    case_completed = pyqtSignal(object)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, arguments: dict[str, Any]) -> None:
        super().__init__()
        self.arguments = arguments
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            result = run_suite(
                **self.arguments,
                on_result=self.case_completed.emit,
                should_stop=lambda: self._stop,
            )
            self.finished.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))


class SuiteTab(QWidget):
    """Authoring and running of endpoint test suites."""

    def __init__(
        self,
        catalog: Catalog,
        suites_dir: Path,
        history: "WorkspaceStore | Path | str",
        environment: Callable[[], dict[str, Any]],
    ) -> None:
        super().__init__()
        self.catalog = catalog
        self.suites_dir = suites_dir
        self.history_store = as_store(history)
        self.environment = environment
        self.endpoints: dict[str, Endpoint] = {
            endpoint.id: endpoint
            for service in catalog.services
            for endpoint in service.endpoints
        }
        self.suite = TestSuite()
        self.current_case: TestCase | None = None
        self.results: dict[str, CaseResult] = {}
        self.last_result: SuiteResult | None = None
        self._loading = False
        self._thread: QThread | None = None
        self._worker: SuiteWorker | None = None

        self._build_ui()
        self._refresh_cases()

    def refresh_catalog(self, catalog: Catalog | None = None) -> None:
        """Rebinds the tab to a catalog loaded after start-up."""
        if catalog is not None:
            self.catalog = catalog
        self.endpoints = {
            endpoint.id: endpoint
            for service in self.catalog.services
            for endpoint in service.endpoints
        }
        self.results.clear()
        self.last_result = None
        self._refresh_cases()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        page_header = QHBoxLayout()
        page_heading = QVBoxLayout()
        page_heading.setSpacing(2)
        title = QLabel("Test Suites")
        title.setProperty("pageTitle", True)
        page_heading.addWidget(title)
        description = QLabel(
            "Build repeatable Rest Tester API workflows and inspect every result."
        )
        description.setProperty("pageDescription", True)
        page_heading.addWidget(description)
        page_header.addLayout(page_heading)
        page_header.addStretch()
        layout.addLayout(page_header)

        toolbar_frame = QWidget()
        toolbar_frame.setObjectName("suiteActionBar")
        toolbar_frame.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        toolbar = QHBoxLayout(toolbar_frame)
        toolbar.setContentsMargins(10, 8, 10, 8)
        toolbar.setSpacing(8)
        self.suite_name = QLineEdit(self.suite.name)
        self.suite_name.setMaximumWidth(260)
        self.suite_name.textChanged.connect(self._suite_name_changed)
        toolbar.addWidget(QLabel("Suite"))
        toolbar.addWidget(self.suite_name)
        self.suite_toolbar_buttons: dict[str, QPushButton] = {}
        for caption, icon_name, slot in (
            ("New", "new", self.new_suite),
            ("Open...", "open", self.open_suite),
            ("Save", "save", self.save),
            ("Save as...", "save-as", self.save_as),
        ):
            button = QPushButton(caption)
            button.clicked.connect(slot)
            toolbar.addWidget(button)
            self.suite_toolbar_buttons[icon_name] = button
        self.stop_on_failure = QCheckBox("Stop on first failure")
        self.stop_on_failure.toggled.connect(self._stop_on_failure_changed)
        toolbar.addWidget(self.stop_on_failure)
        toolbar.addStretch()
        self.run_button = QPushButton("Run suite")
        self.run_button.setProperty("accent", True)
        self.run_button.clicked.connect(self.run)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setProperty("danger", True)
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        self.export_button = QPushButton("Export report...")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export)
        toolbar.addWidget(self.run_button)
        toolbar.addWidget(self.stop_button)
        toolbar.addWidget(self.export_button)
        self.suite_toolbar_buttons.update(
            {
                "play": self.run_button,
                "stop": self.stop_button,
                "export": self.export_button,
            }
        )
        self.suite_action_bar = toolbar_frame
        layout.addWidget(toolbar_frame)

        self.progress_panel = QWidget()
        self.progress_panel.setObjectName("suiteProgressPanel")
        self.progress_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        progress_layout = QHBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(12, 8, 12, 8)
        progress_layout.setSpacing(10)
        self.progress_status = QLabel("Running test suite")
        self.progress_status.setObjectName("suiteProgressStatus")
        progress_layout.addWidget(self.progress_status)
        self.progress = QProgressBar()
        self.progress.setObjectName("suiteRunProgress")
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.progress.setAccessibleName("Suite execution progress")
        progress_layout.addWidget(self.progress, 1)
        self.progress_count = QLabel("0 of 0 cases")
        self.progress_count.setObjectName("suiteProgressCount")
        self.progress_count.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.progress_count.setMinimumWidth(88)
        progress_layout.addWidget(self.progress_count)
        self.progress_panel.setVisible(False)
        layout.addWidget(self.progress_panel)

        splitter = QSplitter()
        splitter.setObjectName("suiteWorkspaceSplitter")
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left.setObjectName("suiteNavigator")
        left.setProperty("preserveMinimumWidth", True)
        left.setMinimumWidth(280)
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel("Test cases (run in order)"))
        self.case_list = QListWidget()
        self.case_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.case_list.currentItemChanged.connect(self._case_selected)
        self.case_list.itemChanged.connect(self._case_toggled)
        self.case_list_empty_state = attach_table_empty_state(
            self.case_list,
            icon_name="test-suites",
            title="No test cases yet",
            guidance="Add a test case to start building this suite.",
        )
        left_layout.addWidget(self.case_list)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.case_action_buttons: dict[str, QPushButton] = {}
        for caption, icon_name, slot in (
            ("Add test case", "add", self.add_case),
            ("Duplicate selected test case", "duplicate", self.duplicate_case),
            ("Remove selected test case", "trash", self.remove_case),
            ("Move selected test case up", "move-up", lambda: self.move_case(-1)),
            (
                "Move selected test case down",
                "move-down",
                lambda: self.move_case(1),
            ),
        ):
            button = QPushButton()
            button.setObjectName("caseActionButton")
            button.setToolTip(caption)
            button.setAccessibleName(caption)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            button.setFixedHeight(34)
            button.setIconSize(QSize(18, 18))
            if icon_name == "trash":
                button.setProperty("danger", True)
            button.clicked.connect(slot)
            buttons.addWidget(button, 1)
            self.case_action_buttons[icon_name] = button
        left_layout.addLayout(buttons)

        variables_box = QGroupBox("Suite variables")
        variables_box.setToolTip("Reference a suite variable in any value as {{name}}")
        variables_layout = QVBoxLayout(variables_box)
        variables_layout.setContentsMargins(0, 0, 0, 0)
        self.variables = QTableWidget(0, 2)
        self.variables.setHorizontalHeaderLabels(["Name", "Value"])
        self.variables.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.variables.itemChanged.connect(self._variables_changed)
        self.variables.setMaximumHeight(140)
        self.variables_empty_state = attach_table_empty_state(
            self.variables,
            icon_name="fields",
            title="No suite variables",
            guidance="Add a variable to reuse a value as {{name}} across cases.",
        )
        variables_layout.addWidget(self.variables)
        variable_buttons = QHBoxLayout()
        variable_buttons.setSpacing(6)
        self.variable_action_buttons: dict[str, QPushButton] = {}
        for caption, icon_name, slot in (
            ("Add suite variable", "add", self.add_variable),
            ("Remove selected suite variable", "trash", self.remove_variable),
        ):
            button = QPushButton()
            button.setObjectName("variableActionButton")
            button.setToolTip(caption)
            button.setAccessibleName(caption)
            button.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            button.setFixedHeight(34)
            button.setIconSize(QSize(18, 18))
            if icon_name == "trash":
                button.setProperty("danger", True)
            button.clicked.connect(slot)
            variable_buttons.addWidget(button, 1)
            self.variable_action_buttons[icon_name] = button
        variables_layout.addLayout(variable_buttons)
        variables_layout.addStretch(1)
        left_layout.addWidget(variables_box)
        self.suite_navigator = left
        splitter.addWidget(left)

        self.detail_tabs = QTabWidget()
        self.detail_tabs.setObjectName("suiteDetailTabs")
        self.detail_tabs.tabBar().setUsesScrollButtons(False)
        self.suite_detail_left_shadows: list[QWidget] = []
        self.suite_detail_top_shadows: list[QWidget] = []

        def add_detail_tab(page: QWidget, title: str) -> None:
            shell, left_shadow, top_shadow = inset_shadow_detail_pane(
                page,
                shell_name="suiteTabContentShell",
            )
            self.suite_detail_left_shadows.append(left_shadow)
            self.suite_detail_top_shadows.append(top_shadow)
            self.detail_tabs.addTab(shell, title)

        add_detail_tab(self._build_request_tab(), "Request")
        add_detail_tab(self._build_expectations_tab(), "Expected result")
        add_detail_tab(self._build_result_tab(), "Case result")
        self.visualizer = RunVisualizer()
        self.visualizer.case_selected.connect(self._select_case_by_id)
        add_detail_tab(self.visualizer, "Run analysis")
        self.suite_timeline = SuiteTimelineTab()
        self.suite_timeline.case_selected.connect(self._select_case_by_id)
        add_detail_tab(self.suite_timeline, "Timeline")
        detail_shell = QWidget()
        detail_shell.setObjectName("suiteDetailShell")
        detail_layout = QVBoxLayout(detail_shell)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        detail_layout.addWidget(self.detail_tabs)
        self.suite_detail_shell = detail_shell
        detail_shell.setProperty("preserveMinimumWidth", True)
        detail_shell.setMinimumWidth(520)
        self.suite_detail_left_shadow = self.suite_detail_left_shadows[0]
        self.suite_detail_top_shadow = self.suite_detail_top_shadows[0]
        splitter.addWidget(detail_shell)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([430, 1170])
        self.suite_workspace_splitter = splitter
        # Stretch 1: the workspace, not the page heading, absorbs spare height.
        # Without it the heading grew whenever the current tab's size hint was
        # small (e.g. Timeline).
        layout.addWidget(splitter, 1)
        self._configure_form_layouts()

    def _configure_form_layouts(self) -> None:
        for form in self.findChildren(QFormLayout):
            form.setLabelAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
            form.setHorizontalSpacing(16)
            form.setVerticalSpacing(8)
            form.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
            )

    def _build_request_tab(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)

        header = QFormLayout()
        self.case_name = QLineEdit()
        self.case_name.textChanged.connect(self._case_name_changed)
        header.addRow("Case name", self.case_name)
        self.case_endpoint = QLabel("—")
        self.case_endpoint.setProperty("monospace", True)
        self.case_endpoint.setStyleSheet("font-weight: bold;")
        header.addRow("Endpoint", self.case_endpoint)
        self.case_description = QLineEdit()
        self.case_description.textChanged.connect(self._case_description_changed)
        header.addRow("Notes", self.case_description)
        self.case_phase = QComboBox()
        self.case_phase.addItem("Normal workflow", "normal")
        self.case_phase.addItem("Cleanup / teardown", "cleanup")
        self.case_phase.currentIndexChanged.connect(self._case_phase_changed)
        header.addRow("Phase", self.case_phase)
        endpoint_height = max(
            self.case_name.sizeHint().height(),
            self.case_description.sizeHint().height(),
            self.case_phase.sizeHint().height(),
        )
        self.case_endpoint.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.case_endpoint.setFixedHeight(endpoint_height)
        self.case_endpoint.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.case_always_run = QCheckBox(
            "Continue despite a failed dependency or Stop request"
        )
        self.case_always_run.setToolTip(
            "Cleanup cases always run after the normal phase. For a normal "
            "case, this is an explicit override that keeps it running after "
            "a failed dependency or Stop request."
        )
        self.case_always_run.toggled.connect(self._case_always_run_changed)
        header.addRow("Cleanup policy", self.case_always_run)
        self.case_dependencies = QListWidget()
        self.case_dependencies.setMaximumHeight(110)
        self.case_dependencies.setToolTip(
            "This case runs only after every selected prerequisite succeeds. "
            "Cleanup cases may depend on normal or cleanup cases."
        )
        self.case_dependencies.itemChanged.connect(self._case_dependencies_changed)
        header.addRow("Depends on", self.case_dependencies)
        self.case_authentication = QComboBox()
        populate_auth_choices(self.case_authentication, {})
        self.case_authentication.setToolTip(
            "Use the service binding, omit credentials for negative tests, "
            "or select an identity. Configure profiles in Settings > Environments."
        )
        self.case_authentication.currentIndexChanged.connect(self._authentication_changed)
        header.addRow("Authentication", self.case_authentication)
        page_layout.addLayout(header)

        # Same editor as the API explorer, so a case is authored with the
        # exact controls used to explore its endpoint.
        self.request_editor = RequestEditor(self)
        self.request_editor.setToolTip(
            "{{variables}} in any value are substituted at run time"
        )
        self.request_editor.changed.connect(self._request_changed)
        for name in (
            "request_tabs", "path_parameters", "query_parameters",
            "path_seed_button", "query_seed_button", "query_builder",
            "sort_builder", "form_builder", "payload", "payload_form",
            "payload_fields_button", "payload_seed_button",
            "payload_fields_buttons", "payload_fields_dialog",
            "filter_dialog", "sort_dialog", "path_tab_index",
            "query_tab_index", "form_tab_index", "json_tab_index",
        ):
            setattr(self, name, getattr(self.request_editor, name))
        page_layout.addWidget(self.request_editor, 1)
        return page

    def _build_expectations_tab(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        scroll = AccordionScrollArea()
        scroll.setObjectName("expectedResultScroll")

        assertions_content = QWidget()
        assertions_layout = QVBoxLayout(assertions_content)
        assertions_layout.setContentsMargins(0, 0, 0, 0)
        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("Expected status"))
        self.expected_status = expected_status_combo()
        self.expected_status.currentTextChanged.connect(self._expected_status_changed)
        status_row.addWidget(self.expected_status)
        status_row.addStretch()
        assertions_layout.addLayout(status_row)

        assertions_hint = QLabel(
            "Every enabled assertion must pass for the case to succeed."
        )
        assertions_hint.setProperty("muted", True)
        assertions_layout.addWidget(assertions_hint)
        self.assertions = QTableWidget(0, 4)
        self.assertions.setHorizontalHeaderLabels(["On", "Assertion", "Path / header", "Expected value"])
        self.assertions.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self.assertions.itemChanged.connect(self._assertions_changed)
        self.assertions_empty_state = self._empty_state(
            "verify",
            "No assertions yet",
            "Add an assertion, or suggest some from the last response. "
            "Until then only the expected status is verified.",
        )
        assertions_layout.addWidget(
            self._table_or_empty_state(
                self.assertions, self.assertions_empty_state, 220
            )
        )
        assertion_buttons = QHBoxLayout()
        add_assertion = QPushButton("Add assertion")
        add_assertion.clicked.connect(self.add_assertion)
        remove_assertion = QPushButton("Remove")
        remove_assertion.setProperty("danger", True)
        remove_assertion.clicked.connect(self.remove_assertion)
        suggest = QPushButton("Suggest from last response")
        suggest.setToolTip("Adds assertions based on the most recent response for this case")
        suggest.clicked.connect(self.suggest_assertions)
        assertion_buttons.addWidget(add_assertion)
        assertion_buttons.addWidget(remove_assertion)
        assertion_buttons.addWidget(suggest)
        assertion_buttons.addStretch()
        assertions_layout.addLayout(assertion_buttons)

        baseline_content = QWidget()
        baseline_layout = QVBoxLayout(baseline_content)
        baseline_layout.setContentsMargins(0, 0, 0, 0)
        baseline_form = QFormLayout()
        self.baseline_enabled = QCheckBox(
            "Validate every response against the saved baseline"
        )
        self.baseline_enabled.toggled.connect(self._baseline_changed)
        baseline_form.addRow("Enabled", self.baseline_enabled)
        self.baseline_ignore_paths = QPlainTextEdit()
        self.baseline_ignore_paths.setPlaceholderText(
            "$.updatedAt\n$.values[*].id\n$.continuationTokenNext"
        )
        self.baseline_ignore_paths.setMaximumHeight(72)
        self.baseline_ignore_paths.textChanged.connect(self._baseline_changed)
        baseline_form.addRow("Ignore paths", self.baseline_ignore_paths)
        self.baseline_ignore_array_order = QCheckBox(
            "Ignore array order where no identity key is configured"
        )
        self.baseline_ignore_array_order.toggled.connect(self._baseline_changed)
        baseline_form.addRow("Arrays", self.baseline_ignore_array_order)
        self.baseline_identity_keys = QPlainTextEdit()
        self.baseline_identity_keys.setPlaceholderText(
            "$.values=id\n$.items=code"
        )
        self.baseline_identity_keys.setMaximumHeight(64)
        self.baseline_identity_keys.textChanged.connect(self._baseline_changed)
        baseline_form.addRow("Array identity keys", self.baseline_identity_keys)
        self.baseline_tolerance = QDoubleSpinBox()
        self.baseline_tolerance.setDecimals(8)
        self.baseline_tolerance.setRange(0.0, 1_000_000_000.0)
        self.baseline_tolerance.setSingleStep(0.001)
        self.baseline_tolerance.valueChanged.connect(self._baseline_changed)
        baseline_form.addRow("Numeric tolerance", self.baseline_tolerance)
        baseline_layout.addLayout(baseline_form)
        baseline_actions = QHBoxLayout()
        self.save_baseline_button = QPushButton("Save last response as baseline")
        self.save_baseline_button.clicked.connect(self.save_baseline)
        self.compare_baseline_button = QPushButton("Compare last response")
        self.compare_baseline_button.clicked.connect(self.compare_baseline)
        self.clear_baseline_button = QPushButton("Clear baseline")
        self.clear_baseline_button.clicked.connect(self.clear_baseline)
        baseline_actions.addWidget(self.save_baseline_button)
        baseline_actions.addWidget(self.compare_baseline_button)
        baseline_actions.addWidget(self.clear_baseline_button)
        baseline_actions.addStretch()
        baseline_layout.addLayout(baseline_actions)
        self.baseline_summary = QLabel("No baseline saved.")
        self.baseline_summary.setProperty("muted", True)
        baseline_layout.addWidget(self.baseline_summary)

        captures_content = QWidget()
        captures_layout = QVBoxLayout(captures_content)
        captures_layout.setContentsMargins(0, 0, 0, 0)
        captures_hint = QLabel(
            "Save response values into {{variables}} for dependent cases."
        )
        captures_hint.setProperty("muted", True)
        captures_layout.addWidget(captures_hint)
        self.captures = QTableWidget(0, 3)
        self.captures.setHorizontalHeaderLabels(["Variable", "From", "Path"])
        self.captures.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.captures.itemChanged.connect(self._captures_changed)
        self.captures_empty_state = self._empty_state(
            "fields",
            "No captured variables",
            "Add a capture to save a response value as a {{variable}} "
            "for the cases that run after this one.",
        )
        captures_layout.addWidget(
            self._table_or_empty_state(
                self.captures, self.captures_empty_state, 180
            )
        )
        capture_buttons = QHBoxLayout()
        add_capture = QPushButton("Add capture")
        add_capture.clicked.connect(self.add_capture)
        remove_capture = QPushButton("Remove")
        remove_capture.setProperty("danger", True)
        remove_capture.clicked.connect(self.remove_capture)
        capture_buttons.addWidget(add_capture)
        capture_buttons.addWidget(remove_capture)
        capture_buttons.addStretch()
        captures_layout.addLayout(capture_buttons)

        self.assertions_section = scroll.add_section(
            "Status and response assertions",
            assertions_content,
            expanded=True,
            summary="0 assertions",
        )
        self.baseline_section = scroll.add_section(
            "Structural JSON baseline", baseline_content, summary="Optional"
        )
        self.captures_section = scroll.add_section(
            "Captured variables", captures_content, summary="0 variables"
        )
        self.expected_result_scroll = scroll
        page_layout.addWidget(scroll)
        return page

    def _build_result_tab(self) -> QWidget:
        page = QWidget()
        page_layout = QVBoxLayout(page)
        result_summary = QWidget()
        result_summary.setObjectName("caseResultSummary")
        result_summary_layout = QVBoxLayout(result_summary)
        result_summary_layout.setContentsMargins(0, 0, 0, 0)
        result_summary_layout.setSpacing(10)
        self.case_outcome = QLabel("Not run yet")
        self.case_outcome.setObjectName("caseResultOutcome")
        self.case_outcome.setStyleSheet("font-size: 15px; font-weight: bold;")
        result_summary_layout.addWidget(self.case_outcome)
        self.assertion_results = QTableWidget(0, 3)
        self.assertion_results.setObjectName("caseResultTable")
        self.assertion_results.setHorizontalHeaderLabels(["Result", "Assertion", "Detail"])
        self.assertion_results.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self.case_captured = QTableWidget(0, 2)
        self.case_captured.setObjectName("caseResultTable")
        self.case_captured.setHorizontalHeaderLabels(["Variable", "Value"])
        self.case_captured.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.case_captured.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.case_captured.setWordWrap(False)
        self.case_captured.verticalHeader().setVisible(False)
        self.captured_summary = QLabel("Not run yet")
        self.captured_summary.setProperty("muted", True)
        self.assertion_results_stack = QStackedLayout()
        self.captured_results_stack = QStackedLayout()
        self.assertion_empty_state = self._empty_state(
            "verify",
            "No assertion results",
            "Run this test case to see validation outcomes.",
        )
        self.captured_empty_state = self._empty_state(
            "fields",
            "No captured variables",
            "Variables captured by this case will appear here.",
        )
        self.case_tables_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.case_tables_splitter.setObjectName("caseResultTablesSplitter")
        self.case_tables_splitter.setChildrenCollapsible(False)
        self.case_tables_splitter.setHandleWidth(18)
        for title, table in (
            ("Test assertions", self.assertion_results),
            ("Captured variables", self.case_captured),
        ):
            section = QWidget()
            section.setObjectName("caseResultColumn")
            group_layout = QVBoxLayout(section)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(6)
            heading_row = QHBoxLayout()
            heading_row.setContentsMargins(2, 0, 2, 0)
            heading = QLabel(title)
            heading.setObjectName("caseResultColumnTitle")
            heading_row.addWidget(heading)
            if table is self.case_captured:
                heading_row.addStretch()
                heading_row.addWidget(self.captured_summary)
            group_layout.addLayout(heading_row)
            stack = (
                self.captured_results_stack
                if table is self.case_captured
                else self.assertion_results_stack
            )
            empty_state = (
                self.captured_empty_state
                if table is self.case_captured
                else self.assertion_empty_state
            )
            stack.addWidget(table)
            stack.addWidget(empty_state)
            stack.setCurrentWidget(empty_state)
            stack_container = QWidget()
            stack_container.setObjectName("caseResultStack")
            stack_container.setMinimumWidth(240)
            stack_container.setLayout(stack)
            group_layout.addWidget(stack_container)
            self.case_tables_splitter.addWidget(section)
        self.case_tables_splitter.setStretchFactor(0, 3)
        self.case_tables_splitter.setStretchFactor(1, 2)
        self.case_tables_splitter.setSizes([600, 400])
        result_summary_layout.addWidget(self.case_tables_splitter, 1)
        result_summary.setMinimumHeight(260)
        self.case_response = ResponseViewer()
        self.case_response.setMinimumHeight(420)
        self.case_result_accordion = AccordionScrollArea()
        self.result_summary_section = self.case_result_accordion.add_section(
            "Result summary and assertions",
            result_summary,
            expanded=True,
            summary="Not run",
        )
        self.result_response_section = self.case_result_accordion.add_section(
            "Recorded response",
            self.case_response,
            expanded=True,
            summary="No response",
        )
        page_layout.addWidget(self.case_result_accordion, 1)
        return page

    @staticmethod
    def _table_or_empty_state(
        table: QTableWidget, empty_state: QWidget, minimum_height: int
    ) -> QWidget:
        """Swaps ``table`` for a headerless empty card whenever it has no rows,
        matching the Case result tables."""
        stack = QStackedLayout()
        stack.addWidget(table)
        stack.addWidget(empty_state)
        container = QWidget()
        container.setObjectName("caseResultStack")
        container.setMinimumHeight(minimum_height)
        container.setLayout(stack)

        model = table.model()
        signals = (model.rowsInserted, model.rowsRemoved, model.modelReset)

        def sync(*_args) -> None:
            # QTableWidget's destructor clears its model, which emits
            # rowsRemoved after the table (and possibly the stack) is gone.
            if sip.isdeleted(table) or sip.isdeleted(empty_state) or sip.isdeleted(stack):
                return
            stack.setCurrentWidget(table if table.rowCount() else empty_state)

        def disconnect(*_args) -> None:
            for signal in signals:
                try:
                    signal.disconnect(sync)
                except (TypeError, RuntimeError):
                    pass

        for signal in signals:
            signal.connect(sync)
        table.destroyed.connect(disconnect)
        container.destroyed.connect(disconnect)
        sync()
        return container

    @staticmethod
    def _empty_state(icon_name: str, title: str, description: str) -> QWidget:
        """The same placeholder card the overlaid table empty states use, for
        the stacked tables whose rows are filled in by the runner."""
        empty = EmptyStateWidget()
        empty.setObjectName("tableEmptyState")
        empty.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        empty.set_content(icon_name=icon_name, title=title, guidance=description)
        return empty

    # -------------------------------------------------------------- suites

    def _suite_name_changed(self, text: str) -> None:
        if not self._loading:
            self.suite.name = text

    def _stop_on_failure_changed(self, checked: bool) -> None:
        if not self._loading:
            self.suite.stop_on_failure = checked

    def new_suite(self) -> None:
        self.suite = TestSuite()
        self.results.clear()
        self.last_result = None
        self._publish_result(None)
        self._load_suite_into_ui()

    def open_suite(self) -> None:
        self.suites_dir.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(
            self, "Open test suite", str(self.suites_dir), "Test suites (*.json)"
        )
        if not path:
            return
        try:
            self.suite = load_suite(Path(path))
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            QMessageBox.warning(self, "Could not open suite", str(exc))
            return
        self.results.clear()
        self.last_result = None
        self._publish_result(None)
        self._load_suite_into_ui()

    def save(self) -> None:
        if self.suite.path is None:
            self.save_as()
            return
        save_suite(self.suite, self.suite.path)
        QMessageBox.information(self, "Suite saved", f"Saved to {self.suite.path}")

    def save_as(self) -> None:
        self.suites_dir.mkdir(parents=True, exist_ok=True)
        suggested = self.suites_dir / f"{self.suite.name or 'suite'}.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save test suite", str(suggested), "Test suites (*.json)"
        )
        if not path:
            return
        save_suite(self.suite, Path(path))
        QMessageBox.information(self, "Suite saved", f"Saved to {path}")

    def _load_suite_into_ui(self) -> None:
        self._loading = True
        self.suite_name.setText(self.suite.name)
        self.stop_on_failure.setChecked(self.suite.stop_on_failure)
        self.variables.setRowCount(0)
        for name, value in self.suite.variables.items():
            row = self.variables.rowCount()
            self.variables.insertRow(row)
            self.variables.setItem(row, 0, QTableWidgetItem(name))
            self.variables.setItem(row, 1, QTableWidgetItem(value))
        self._loading = False
        self._refresh_cases()

    # --------------------------------------------------------------- cases

    def suggested_case_name(self, endpoint: Endpoint) -> str:
        """Unique default name for a new case against ``endpoint``."""
        return suggest_case_name(
            endpoint.method, endpoint.path, [case.name for case in self.suite.cases]
        )

    def prompt_case_name(self, endpoint: Endpoint, parent: QWidget | None = None) -> str | None:
        """Asks for a case name, pre-filled with a unique suggestion.

        Returns ``None`` when the user cancels, so the case is not added.
        """
        name, accepted = QInputDialog.getText(
            parent or self,
            "Name test case",
            f"Name for this {endpoint.method} {endpoint.path} case:",
            text=self.suggested_case_name(endpoint),
        )
        if not accepted:
            return None
        name = name.strip()
        return name or self.suggested_case_name(endpoint)

    def add_case_for_endpoint(
        self,
        endpoint: Endpoint,
        values: dict[str, str],
        payload: Any,
        name: str | None = None,
    ) -> None:
        """Adds a case seeded from the explorer's current request."""
        case = TestCase(
            endpoint_id=endpoint.id,
            name=name or self.suggested_case_name(endpoint),
            values=dict(values),
            payload=payload,
            expected_status=endpoint.expected_status,
        )
        self.suite.cases.append(case)
        self._refresh_cases()
        self.case_list.setCurrentRow(len(self.suite.cases) - 1)

    def add_case(self) -> None:
        options = [
            f"{endpoint.service} · {endpoint.method} {endpoint.path}"
            for endpoint in self.endpoints.values()
        ]
        choice, accepted = QInputDialog.getItem(
            self, "Add test case", "Endpoint", options, 0, True
        )
        if not accepted or not choice:
            return
        index = options.index(choice) if choice in options else -1
        if index < 0:
            QMessageBox.warning(self, "Unknown endpoint", "Pick an endpoint from the list.")
            return
        endpoint = list(self.endpoints.values())[index]
        name = self.prompt_case_name(endpoint)
        if name is None:
            return
        values = {
            f"{parameter.source}:{parameter.name}": seed_parameter(parameter)
            for parameter in endpoint.parameters
            if not (parameter.source == "form" and "file" in parameter.type.lower())
        }
        self.add_case_for_endpoint(endpoint, values, endpoint.payload, name)

    def duplicate_case(self) -> None:
        if self.current_case is None:
            return
        index = self.suite.cases.index(self.current_case)
        self.suite.cases.insert(index + 1, clone_case(self.current_case))
        self._refresh_cases()
        self.case_list.setCurrentRow(index + 1)

    def remove_case(self) -> None:
        if self.current_case is None:
            return
        index = self.suite.cases.index(self.current_case)
        removed = self.suite.cases.pop(index)
        for case in self.suite.cases:
            case.depends_on = [
                dependency for dependency in case.depends_on
                if dependency != removed.id
            ]
        self.current_case = None
        self._refresh_cases()
        if self.suite.cases:
            self.case_list.setCurrentRow(min(index, len(self.suite.cases) - 1))

    def move_case(self, offset: int) -> None:
        if self.current_case is None:
            return
        index = self.suite.cases.index(self.current_case)
        target = index + offset
        if not 0 <= target < len(self.suite.cases):
            return
        self.suite.cases[index], self.suite.cases[target] = (
            self.suite.cases[target],
            self.suite.cases[index],
        )
        self._refresh_cases()
        self.case_list.setCurrentRow(target)

    def _refresh_cases(self) -> None:
        self._loading = True
        selected = self.current_case.id if self.current_case else None
        self.case_list.clear()
        for index, case in enumerate(self.suite.cases, start=1):
            endpoint = self.endpoints.get(case.endpoint_id)
            label = case.name or (
                f"{endpoint.method} {endpoint.path}" if endpoint else case.endpoint_id
            )
            phase = "Cleanup" if case.phase == "cleanup" else "Run"
            item = QListWidgetItem(f"{index:>3}. [{phase}] {label}")
            item.setData(Qt.ItemDataRole.UserRole, case.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if case.enabled else Qt.CheckState.Unchecked
            )
            if endpoint is None:
                item.setForeground(QColor("#b54708"))
                item.setToolTip("Endpoint missing from the catalog")
            result = self.results.get(case.id)
            if result is not None:
                item.setForeground(OUTCOME_COLORS.get(result.outcome, QColor("#101828")))
            self.case_list.addItem(item)
        self._loading = False
        if selected is not None:
            self._select_case_by_id(selected)

    def _select_case_by_id(self, case_id: str) -> None:
        for row in range(self.case_list.count()):
            if self.case_list.item(row).data(Qt.ItemDataRole.UserRole) == case_id:
                self.case_list.setCurrentRow(row)
                return

    def _case_toggled(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        case = self.suite.case(item.data(Qt.ItemDataRole.UserRole))
        if case is not None:
            case.enabled = item.checkState() == Qt.CheckState.Checked

    def _case_selected(self, current: QListWidgetItem | None) -> None:
        if current is None:
            self.current_case = None
            return
        case = self.suite.case(current.data(Qt.ItemDataRole.UserRole))
        if case is None:
            return
        self.current_case = case
        self._load_case_into_ui(case)

    def _load_case_into_ui(self, case: TestCase) -> None:
        self._loading = True
        endpoint = self.endpoints.get(case.endpoint_id)
        self.case_name.setText(case.name)
        self.case_description.setText(case.description)
        self.case_phase.setCurrentIndex(
            max(self.case_phase.findData(case.phase), 0)
        )
        self.case_always_run.setChecked(
            case.always_run or case.phase == "cleanup"
        )
        self.case_always_run.setEnabled(case.phase != "cleanup")
        self.case_dependencies.clear()
        for candidate in self.suite.cases:
            if candidate.id == case.id:
                continue
            item = QListWidgetItem(candidate.name or candidate.id)
            item.setData(Qt.ItemDataRole.UserRole, candidate.id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if candidate.id in case.depends_on
                else Qt.CheckState.Unchecked
            )
            candidate_phase = (
                "cleanup" if candidate.phase == "cleanup" else "normal"
            )
            item.setToolTip(f"{candidate_phase}: {candidate.id}")
            self.case_dependencies.addItem(item)
        self.case_endpoint.setText(
            f"{endpoint.service} · {endpoint.method} {endpoint.path}"
            if endpoint
            else f"missing: {case.endpoint_id}"
        )
        self.expected_status.setCurrentText(case.expected_status)
        self.refresh_authentication_choices()

        self.request_editor.load(
            self.catalog,
            endpoint,
            dict(case.values),
            "" if case.payload is None else json.dumps(case.payload, indent=2),
        )
        self._refresh_header_context()

        self.assertions.setRowCount(0)
        for assertion in case.assertions:
            self._append_assertion_row(assertion)
        baseline = case.baseline
        self.baseline_enabled.setChecked(baseline.enabled)
        self.baseline_ignore_paths.setPlainText("\n".join(baseline.ignored_paths))
        self.baseline_ignore_array_order.setChecked(baseline.ignore_array_order)
        self.baseline_identity_keys.setPlainText(
            "\n".join(
                f"{path}={key}"
                for path, key in sorted(baseline.array_identity_keys.items())
            )
        )
        self.baseline_tolerance.setValue(baseline.numeric_tolerance)
        self._refresh_baseline_controls()
        self.baseline_section.set_expanded(
            baseline.enabled or baseline.document is not None
        )
        self.captures.setRowCount(0)
        for capture in case.captures:
            self._append_capture_row(capture)
        self.captures_section.set_expanded(bool(case.captures))
        self._refresh_expectation_summaries()
        self._loading = False
        self._show_case_result(self.results.get(case.id))

    # ------------------------------------------------------- case editing

    def _case_name_changed(self, text: str) -> None:
        if self._loading or self.current_case is None:
            return
        self.current_case.name = text
        item = self.case_list.currentItem()
        if item is not None:
            index = self.suite.cases.index(self.current_case) + 1
            phase = "Cleanup" if self.current_case.phase == "cleanup" else "Run"
            self._loading = True
            item.setText(f"{index:>3}. [{phase}] {text}")
            self._loading = False

    def _case_description_changed(self, text: str) -> None:
        if not self._loading and self.current_case is not None:
            self.current_case.description = text

    def _case_phase_changed(self) -> None:
        if self._loading or self.current_case is None:
            return
        phase = str(self.case_phase.currentData() or "normal")
        self.current_case.phase = phase
        self.case_always_run.setEnabled(phase != "cleanup")
        self.case_always_run.setChecked(phase == "cleanup")
        self._refresh_cases()

    def _case_always_run_changed(self, checked: bool) -> None:
        if not self._loading and self.current_case is not None:
            self.current_case.always_run = checked

    def _case_dependencies_changed(self, _item: QListWidgetItem) -> None:
        if self._loading or self.current_case is None:
            return
        self.current_case.depends_on = [
            str(item.data(Qt.ItemDataRole.UserRole))
            for row in range(self.case_dependencies.count())
            if (item := self.case_dependencies.item(row)).checkState()
            == Qt.CheckState.Checked
        ]

    def _expected_status_changed(self, text: str) -> None:
        if not self._loading and self.current_case is not None:
            self.current_case.expected_status = text

    def refresh_authentication_choices(self) -> None:
        context = self.environment().get("auth_context")
        populate_auth_choices(
            self.case_authentication, context.profiles if context is not None else {},
            self.current_case.authentication if self.current_case is not None else "inherit",
        )
        self._refresh_header_context()

    def _authentication_changed(self) -> None:
        if not self._loading and self.current_case is not None:
            self.current_case.authentication = self.case_authentication.currentData() or "inherit"
        self._refresh_header_context()

    def _refresh_header_context(self) -> None:
        environment = self.environment()
        context = environment.get("auth_context")
        case = self.current_case
        endpoint = self.endpoints.get(case.endpoint_id) if case else None
        managed = frozenset()
        omitted = frozenset()
        error = ""
        if context is not None and endpoint is not None and case is not None:
            try:
                managed = context.managed_header_names(endpoint.service, case.authentication)
                omitted = context.secret_names if context.is_disabled(endpoint.service, case.authentication) else frozenset()
            except AuthError as exc:
                error = str(exc)
        self.request_editor.headers_editor.set_context(
            environment.get("custom_headers", {}),
            {**environment.get("variables", {}), **self.suite.variables},
            managed, omitted,
            context.secret_names if context is not None else SECRET_HEADER_NAMES,
            error,
        )
        self.request_editor.set_file_secret_names(
            context.secret_names if context is not None else SECRET_HEADER_NAMES
        )

    def _request_changed(self) -> None:
        if self._loading or self.current_case is None:
            return
        values = self.current_case.values
        # Replace every form key so cleared fields are dropped from the case.
        for key in [k for k in values if k.startswith(("form:", "file:", "header:", "enabled:header:"))]:
            del values[key]
        values.update(self.request_editor.values())
        text = self.request_editor.payload_text().strip()
        if not text:
            self.current_case.payload = None
            return
        try:
            self.current_case.payload = json.loads(text)
        except json.JSONDecodeError:
            # Keep the previous parsed payload; the run guard reports invalid JSON.
            pass

    # --------------------------------------------------------- assertions

    def _append_assertion_row(self, assertion: Assertion) -> None:
        row = self.assertions.rowCount()
        self.assertions.insertRow(row)

        enabled = QTableWidgetItem()
        enabled.setFlags(
            (enabled.flags() | Qt.ItemFlag.ItemIsUserCheckable) & ~Qt.ItemFlag.ItemIsEditable
        )
        enabled.setCheckState(
            Qt.CheckState.Checked if assertion.enabled else Qt.CheckState.Unchecked
        )
        self.assertions.setItem(row, 0, enabled)

        kind = QComboBox()
        for name, meta in ASSERTION_KINDS.items():
            if name == "baseline":
                continue
            kind.addItem(meta["label"], name)
        kind.setCurrentIndex(max(kind.findData(assertion.kind), 0))
        kind.currentIndexChanged.connect(lambda _, r=row: self._assertion_kind_changed(r))
        self.assertions.setCellWidget(row, 1, kind)

        self.assertions.setItem(row, 2, QTableWidgetItem(assertion.path))
        self.assertions.setItem(row, 3, QTableWidgetItem(assertion.value))
        self._apply_assertion_kind_hints(row)

    def _apply_assertion_kind_hints(self, row: int) -> None:
        kind_box = self.assertions.cellWidget(row, 1)
        if kind_box is None:
            return
        meta = ASSERTION_KINDS.get(kind_box.currentData(), {})
        for column, needed in ((2, meta.get("path")), (3, meta.get("value"))):
            item = self.assertions.item(row, column)
            if item is None:
                continue
            if needed:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                item.setBackground(QColor(theme.SURFACE))
            else:
                item.setText("")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                item.setBackground(QColor(theme.SURFACE_ALT))

    def refresh_theme(self) -> None:
        self.request_editor.refresh_theme()
        for icon_name, button in self.suite_toolbar_buttons.items():
            color = (
                theme.TEXT_INVERSE
                if button is self.run_button
                else theme.FAIL
                if button is self.stop_button
                else theme.TEXT
            )
            button.setIcon(icon(icon_name, color, 18))
        for icon_name, button in self.case_action_buttons.items():
            button.setIcon(
                icon(icon_name, theme.FAIL if icon_name == "trash" else theme.TEXT, 18)
            )
        for icon_name, button in self.variable_action_buttons.items():
            button.setIcon(
                icon(icon_name, theme.FAIL if icon_name == "trash" else theme.TEXT, 18)
            )
        for row in range(self.assertions.rowCount()):
            self._apply_assertion_kind_hints(row)
        for row in range(self.assertion_results.rowCount()):
            status = self.assertion_results.item(row, 0)
            if status is None:
                continue
            passed = status.text() == "PASS"
            status.setForeground(QColor(theme.PASS if passed else theme.FAIL))
            status.setBackground(
                QColor(theme.SUCCESS_SOFT if passed else theme.DANGER_SOFT)
            )
        self.visualizer.refresh_theme()
        for empty_state in (
            self.assertions_empty_state,
            self.captures_empty_state,
            self.assertion_empty_state,
            self.captured_empty_state,
            self.case_list_empty_state,
            self.variables_empty_state,
        ):
            empty_state.refresh_theme()

    def _assertion_kind_changed(self, row: int) -> None:
        self._apply_assertion_kind_hints(row)
        self._assertions_changed(None)

    def _baseline_changed(self, *_args) -> None:
        if self._loading or self.current_case is None:
            return
        identity_keys: dict[str, str] = {}
        for line in self.baseline_identity_keys.toPlainText().splitlines():
            path, separator, key = line.partition("=")
            if separator and path.strip() and key.strip():
                identity_keys[path.strip()] = key.strip()
        baseline = self.current_case.baseline
        baseline.enabled = self.baseline_enabled.isChecked()
        baseline.ignored_paths = [
            line.strip()
            for line in self.baseline_ignore_paths.toPlainText().splitlines()
            if line.strip()
        ]
        baseline.ignore_array_order = self.baseline_ignore_array_order.isChecked()
        baseline.array_identity_keys = identity_keys
        baseline.numeric_tolerance = self.baseline_tolerance.value()
        self._refresh_baseline_controls()

    def _refresh_baseline_controls(self) -> None:
        case = self.current_case
        baseline = case.baseline if case is not None else None
        has_baseline = baseline is not None and baseline.document is not None
        result = self.results.get(case.id) if case is not None else None
        has_json_result = result is not None and bool(result.response_body.strip())
        self.clear_baseline_button.setEnabled(has_baseline)
        self.compare_baseline_button.setEnabled(has_baseline and has_json_result)
        self.save_baseline_button.setEnabled(has_json_result)
        self.save_baseline_button.setText(
            "Update baseline from last response"
            if has_baseline
            else "Save last response as baseline"
        )
        if not has_baseline:
            self.baseline_summary.setText("No baseline saved.")
        else:
            self.baseline_summary.setText(
                f"Baseline saved · {len(baseline.ignored_paths)} ignored path(s) · "
                f"{len(baseline.array_identity_keys)} array identity rule(s)"
            )
        if hasattr(self, "baseline_section"):
            self.baseline_section.set_summary(
                "Enabled" if baseline is not None and baseline.enabled else "Optional"
            )

    def save_baseline(self) -> None:
        if self.current_case is None:
            return
        result = self.results.get(self.current_case.id)
        if result is None:
            QMessageBox.information(
                self,
                "No response",
                "Run this case before saving a response as its baseline.",
            )
            return
        try:
            document = json.loads(result.response_body)
        except json.JSONDecodeError as exc:
            QMessageBox.warning(
                self,
                "Response is not JSON",
                f"Only JSON responses can be saved as structural baselines: {exc}",
            )
            return
        if document is None:
            QMessageBox.warning(
                self,
                "Empty JSON baseline",
                "A JSON null response cannot be saved as a structural baseline.",
            )
            return
        self.current_case.baseline.document = document
        self.current_case.baseline.enabled = True
        self.baseline_enabled.setChecked(True)
        self._refresh_baseline_controls()

    def clear_baseline(self) -> None:
        if self.current_case is None:
            return
        self.current_case.baseline.document = None
        self.current_case.baseline.enabled = False
        self.baseline_enabled.setChecked(False)
        self._refresh_baseline_controls()

    def compare_baseline(self) -> None:
        if self.current_case is None:
            return
        baseline = self.current_case.baseline.document
        result = self.results.get(self.current_case.id)
        if baseline is None or result is None:
            return
        previous = json.dumps(baseline, indent=2, ensure_ascii=False)
        current = result.baseline_actual or result.response_body
        ResponseComparison(previous, current, self).exec()

    def _assertions_changed(self, _item: QTableWidgetItem | None) -> None:
        if self._loading or self.current_case is None:
            return
        assertions: list[Assertion] = []
        for row in range(self.assertions.rowCount()):
            kind_box = self.assertions.cellWidget(row, 1)
            enabled_item = self.assertions.item(row, 0)
            assertions.append(
                Assertion(
                    kind=kind_box.currentData() if kind_box else "json_exists",
                    path=self._cell_text(self.assertions, row, 2),
                    value=self._cell_text(self.assertions, row, 3),
                    enabled=enabled_item is None
                    or enabled_item.checkState() == Qt.CheckState.Checked,
                )
            )
        self.current_case.assertions = assertions
        self._refresh_expectation_summaries()

    @staticmethod
    def _cell_text(table: QTableWidget, row: int, column: int) -> str:
        item = table.item(row, column)
        return item.text() if item is not None else ""

    def add_assertion(self) -> None:
        if self.current_case is None:
            QMessageBox.information(self, "No case", "Select a test case first.")
            return
        self._append_assertion_row(Assertion())
        self._assertions_changed(None)

    def remove_assertion(self) -> None:
        row = self.assertions.currentRow()
        if row < 0:
            return
        self.assertions.removeRow(row)
        self._rebind_assertion_rows()
        self._assertions_changed(None)

    def _rebind_assertion_rows(self) -> None:
        for row in range(self.assertions.rowCount()):
            kind_box = self.assertions.cellWidget(row, 1)
            if kind_box is None:
                continue
            kind_box.blockSignals(True)
            try:
                kind_box.currentIndexChanged.disconnect()
            except TypeError:
                pass
            kind_box.currentIndexChanged.connect(
                lambda _, r=row: self._assertion_kind_changed(r)
            )
            kind_box.blockSignals(False)

    def suggest_assertions(self) -> None:
        """Derives assertions from the most recent response for the selected case."""
        if self.current_case is None:
            return
        result = self.results.get(self.current_case.id)
        if result is None or not result.response_body.strip():
            QMessageBox.information(
                self,
                "No response yet",
                "Run the suite once so there is a response to derive assertions from.",
            )
            return
        try:
            document = json.loads(result.response_body)
        except json.JSONDecodeError:
            QMessageBox.information(
                self, "Not JSON", "The last response was not JSON, so fields cannot be suggested."
            )
            return

        suggestions: list[Assertion] = []
        if isinstance(document, list):
            suggestions.append(Assertion(kind="json_type", path="", value="array"))
            suggestions.append(
                Assertion(kind="json_length_gte", path="", value=str(min(len(document), 1)))
            )
            document = document[0] if document and isinstance(document[0], dict) else {}
            prefix = "[0]."
        else:
            prefix = ""
        if isinstance(document, dict):
            for key, value in list(document.items())[:8]:
                if isinstance(value, (dict, list)):
                    suggestions.append(Assertion(kind="json_exists", path=f"{prefix}{key}"))
                elif value is not None and value != "":
                    suggestions.append(
                        Assertion(kind="json_equals", path=f"{prefix}{key}", value=str(value))
                    )
        if not suggestions:
            QMessageBox.information(self, "Nothing to suggest", "No scalar fields were found.")
            return
        for assertion in suggestions:
            self._append_assertion_row(assertion)
        self._assertions_changed(None)
        self.detail_tabs.setCurrentIndex(1)

    # ----------------------------------------------------------- captures

    def _append_capture_row(self, capture: Capture) -> None:
        row = self.captures.rowCount()
        self.captures.insertRow(row)
        self.captures.setItem(row, 0, QTableWidgetItem(capture.name))
        source = QComboBox()
        source.addItems(CAPTURE_SOURCES)
        source.setCurrentText(capture.source)
        source.currentIndexChanged.connect(lambda _: self._captures_changed(None))
        self.captures.setCellWidget(row, 1, source)
        self.captures.setItem(row, 2, QTableWidgetItem(capture.path))

    def add_capture(self) -> None:
        if self.current_case is None:
            QMessageBox.information(self, "No case", "Select a test case first.")
            return
        self._append_capture_row(Capture(name="id", path="id"))
        self._captures_changed(None)

    def remove_capture(self) -> None:
        row = self.captures.currentRow()
        if row < 0:
            return
        self.captures.removeRow(row)
        self._captures_changed(None)

    def _captures_changed(self, _item: QTableWidgetItem | None) -> None:
        if self._loading or self.current_case is None:
            return
        captures: list[Capture] = []
        for row in range(self.captures.rowCount()):
            source_box = self.captures.cellWidget(row, 1)
            captures.append(
                Capture(
                    name=self._cell_text(self.captures, row, 0),
                    source=source_box.currentText() if source_box else "body",
                    path=self._cell_text(self.captures, row, 2),
                )
            )
        self.current_case.captures = captures
        self._refresh_expectation_summaries()

    def _refresh_expectation_summaries(self) -> None:
        if hasattr(self, "assertions_section"):
            count = self.assertions.rowCount()
            self.assertions_section.set_summary(
                f"{count} assertion{'s' if count != 1 else ''}"
            )
        if hasattr(self, "captures_section"):
            count = self.captures.rowCount()
            self.captures_section.set_summary(
                f"{count} variable{'s' if count != 1 else ''}"
            )

    # ---------------------------------------------------------- variables

    def add_variable(self) -> None:
        row = self.variables.rowCount()
        self.variables.insertRow(row)
        self.variables.setItem(row, 0, QTableWidgetItem(f"variable{row + 1}"))
        self.variables.setItem(row, 1, QTableWidgetItem(""))
        self._variables_changed(None)

    def remove_variable(self) -> None:
        row = self.variables.currentRow()
        if row < 0:
            return
        self.variables.removeRow(row)
        self._variables_changed(None)

    def _variables_changed(self, _item: QTableWidgetItem | None) -> None:
        if self._loading:
            return
        variables: dict[str, str] = {}
        for row in range(self.variables.rowCount()):
            name = self._cell_text(self.variables, row, 0).strip()
            if name:
                variables[name] = self._cell_text(self.variables, row, 1)
        self.suite.variables = variables

    # --------------------------------------------------------------- run

    def run(self) -> None:
        if not self.suite.cases:
            QMessageBox.information(self, "Empty suite", "Add at least one test case.")
            return
        text = self.payload.toPlainText().strip()
        if text:
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                QMessageBox.warning(
                    self, "Invalid payload", f"The selected case's payload is not valid JSON: {exc}"
                )
                return

        environment = self.environment()
        missing = sorted(
            {
                self.endpoints[case.endpoint_id].service
                for case in self.suite.cases
                if case.enabled
                and case.endpoint_id in self.endpoints
                and not environment["base_urls"].get(
                    self.endpoints[case.endpoint_id].service, ""
                ).strip()
            }
        )
        if missing:
            QMessageBox.warning(
                self,
                "Missing base URL",
                "Provide a base URL for: " + ", ".join(missing),
            )
            return

        self.results.clear()
        self._refresh_cases()
        self.progress.setRange(0, len(self.suite.cases))
        self.progress.setValue(0)
        self.progress_status.setText("Running test suite")
        self.progress_count.setText(f"0 of {len(self.suite.cases)} cases")
        self.progress_panel.setVisible(True)
        self.run_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.export_button.setEnabled(False)
        self.detail_tabs.setCurrentIndex(self.detail_tabs.indexOf(self.visualizer))

        self._thread = QThread()
        self._worker = SuiteWorker(
            {
                "suite": deepcopy(self.suite),
                "endpoints": dict(self.endpoints),
                "base_urls": environment["base_urls"],
                "access_token": environment["access_token"],
                "api_key": environment["api_key"],
                "timeout": environment.get("request_timeout", 30),
                "verify_ssl": environment.get("verify_ssl", True),
                "custom_headers": environment.get("custom_headers", {}),
                "environment_variables": environment.get("variables", {}),
                "auth_context": environment.get("auth_context"),
                "catalog": self.catalog,
            }
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.case_completed.connect(self._case_completed)
        self._worker.finished.connect(self._run_finished)
        self._worker.failed.connect(self._run_failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._cleanup_thread)
        self._thread.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self.stop_button.setEnabled(False)
            self.progress_status.setText("Stopping after current case...")

    def _case_completed(self, result: CaseResult) -> None:
        self.results[result.case_id] = result
        self.progress.setValue(self.progress.value() + 1)
        self.progress_status.setText(f"Completed {result.case_name}")
        self.progress_count.setText(
            f"{self.progress.value()} of {self.progress.maximum()} cases"
        )
        for row in range(self.case_list.count()):
            item = self.case_list.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == result.case_id:
                item.setForeground(OUTCOME_COLORS.get(result.outcome, QColor("#101828")))
                break
        if self.current_case is not None and self.current_case.id == result.case_id:
            self._show_case_result(result)

    def _run_finished(self, result: SuiteResult) -> None:
        self.last_result = result
        self._publish_result(result)
        append_suite_history(self.history_store, result)
        self.export_button.setEnabled(True)
        self.progress_status.setText("Run complete")
        self.progress.setValue(self.progress.maximum())
        QTimer.singleShot(1600, self._hide_completed_progress)

    def _publish_result(self, result: SuiteResult | None) -> None:
        """Feeds a run to every view that renders one."""
        self.visualizer.set_result(result)
        self.suite_timeline.set_result(result)

    def _run_failed(self, message: str) -> None:
        self.progress_panel.setVisible(False)
        QMessageBox.warning(self, "Suite run failed", message)

    def _hide_completed_progress(self) -> None:
        if self._worker is None and self.progress_status.text() == "Run complete":
            self.progress_panel.setVisible(False)

    def _cleanup_thread(self) -> None:
        self.run_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    def _show_case_result(self, result: CaseResult | None) -> None:
        if result is None:
            self.case_outcome.setText("Not run yet")
            self.case_outcome.setProperty("outcome", "")
            self._refresh_widget_style(self.case_outcome)
            self.assertion_results.setRowCount(0)
            self.case_response.clear()
            self.case_captured.setRowCount(0)
            self.assertion_results_stack.setCurrentWidget(self.assertion_empty_state)
            self.captured_results_stack.setCurrentWidget(self.captured_empty_state)
            self.captured_summary.setText("Not run yet")
            self.result_summary_section.set_summary("Not run")
            self.result_response_section.set_summary("No response")
            self._refresh_baseline_controls()
            return
        summary = (
            f"{result.outcome} — HTTP {result.status_code} "
            f"(expected {result.expected_status}) in {result.elapsed_ms} ms"
        )
        if result.error:
            summary = f"{result.outcome} — {result.error}"
        elif result.skipped and result.skip_reason:
            summary = f"{result.outcome} — {result.skip_reason}"
        if result.phase == "cleanup":
            summary = f"CLEANUP · {summary}"
        self.case_outcome.setText(summary)
        self.case_outcome.setProperty("outcome", result.outcome.lower())
        self._refresh_widget_style(self.case_outcome)
        self.result_summary_section.set_summary(result.outcome.title())
        response_summary = (
            f"HTTP {result.status_code} · {result.elapsed_ms} ms"
            if result.status_code
            else result.outcome.title()
        )
        self.result_response_section.set_summary(response_summary)
        self.assertion_results.setRowCount(len(result.assertions))
        self.assertion_results_stack.setCurrentWidget(
            self.assertion_results if result.assertions else self.assertion_empty_state
        )
        for row, outcome in enumerate(result.assertions):
            status = QTableWidgetItem("PASS" if outcome.passed else "FAIL")
            status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            font = status.font()
            font.setBold(True)
            status.setFont(font)
            if outcome.passed:
                status.setForeground(QColor(theme.PASS))
                status.setBackground(QColor(theme.SUCCESS_SOFT))
            else:
                status.setForeground(QColor(theme.FAIL))
                status.setBackground(QColor(theme.DANGER_SOFT))
            self.assertion_results.setItem(row, 0, status)
            self.assertion_results.setItem(row, 1, QTableWidgetItem(outcome.assertion.label))
            self.assertion_results.setItem(row, 2, QTableWidgetItem(outcome.detail))
        self.case_captured.setRowCount(len(result.captured))
        self.captured_results_stack.setCurrentWidget(
            self.case_captured if result.captured else self.captured_empty_state
        )
        for row, (name, value) in enumerate(result.captured.items()):
            for column, text in enumerate((name, value)):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.case_captured.setItem(row, column, item)
        self.captured_summary.setText(
            f"{len(result.captured)} captured" if result.captured else "None captured"
        )
        self.case_response.show_result(result)
        location, fragment_separator, fragment = result.url.partition("#")
        path, query_separator, query = location.partition("?")
        self.case_response.url_label.setText(
            unquote(path) + query_separator + unquote_plus(query)
            + fragment_separator + unquote(fragment)
        )
        self._refresh_baseline_controls()
        if result.baseline_differences:
            self.baseline_summary.setText(
                f"{len(result.baseline_differences)} structural difference(s). "
                "Use Compare last response for the side-by-side view."
            )

    @staticmethod
    def _refresh_widget_style(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def export(self) -> None:
        if self.last_result is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export run report",
            str(self.suites_dir / f"{self.suite.name or 'suite'}-report.json"),
            "JSON report (*.json)",
        )
        if not path:
            return
        export_report(self.last_result, Path(path))
        QMessageBox.information(self, "Report exported", f"Written to {path}")
