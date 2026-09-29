from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QElapsedTimer, QObject, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .branding import APP_NAME, display_name
from .builders import FormBuilder, PayloadForm, QueryBuilder, SortBuilder
from .catalog import Catalog, Endpoint, Service, load_catalog
from .client import (
    ApiResult,
    append_history,
    execute_endpoint,
    generate_curl,
    generate_python,
    parameter_enabled_key,
    parameter_is_enabled,
    prepare_endpoint_request,
    redact_headers,
)
from .environment import AppSettings, validate_base_url
from .authentication import AuthError
from .request_auth import AuthenticationContext, populate_auth_choices
from .data_runner.ui import DataRunnerTab
from .load_testing.ui import LoadTestingTab
from .environment_ui import EnvironmentEditor, EnvironmentManagerPage
from .execution.models import ExecutionEnvironmentSnapshot
from .icons import app_icon, app_pixmap, icon
from .documentation import endpoint_documentation
from .seeding import refresh_payload, seed_parameter
from .saved_requests import (
    CollectionsPage,
    SavedRequest,
    SavedRequestStore,
    SavedRequestsPage,
)
from .suite import TestCase, TestSuite, apply_variables
from .suite_ui import SuiteTab
from .splash import MINIMUM_VISIBLE_MS, SplashScreen
from .viewers import FilePicker, JsonTextEdit, RequestBodyEditor, ResponseViewer, ValuePicker
from .widgets import (
    AccordionSection,
    AccordionScrollArea,
    ElidingLabel,
    OverlayEmptyState,
    attach_table_empty_state,
    inset_shadow_detail_pane,
)
from .workspace_store import WorkspaceStore, migrate_legacy_files

def application_root() -> Path:
    """Returns the source root or the portable executable directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def application_resource_root() -> Path:
    """Returns the root containing read-only resources bundled by PyInstaller."""
    bundled_root = getattr(sys, "_MEIPASS", None)
    return Path(bundled_root).resolve() if bundled_root else application_root()


ROOT = application_root()
CATALOG_PATH = application_resource_root() / "data" / "api_catalog.json"
SETTINGS_PATH = ROOT / "data" / "settings.json"
SUITES_DIR = ROOT / "suites"
#: Run history, suite history, and saved requests share one database. Data
#: Runner and Load Testing keep writing one database per execution so a run
#: stays a portable artifact; retention never touches those.
WORKSPACE_DB_PATH = ROOT / "data" / "workspace.db"
LEGACY_HISTORY_PATH = ROOT / "data" / "run_history.jsonl"
LEGACY_SUITE_HISTORY_PATH = ROOT / "data" / "suite_history.jsonl"
LEGACY_SAVED_REQUESTS_PATH = ROOT / "data" / "saved_requests.json"

#: Stand-in used when the build ships without a catalog, or the configured one
#: cannot be read. It keeps the shell usable so the user can load or build one.
EMPTY_CATALOG = Catalog(
    services=(),
    filter_schemas={},
    payload_schemas={},
    enums={},
)


class RequestWorker(QObject):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(
        self, arguments: tuple[Any, ...],
        auth_context: AuthenticationContext | None = None, auth_mode: str = "inherit",
    ) -> None:
        super().__init__()
        self.arguments = arguments
        self.auth_context = auth_context
        self.auth_mode = auth_mode

    def run(self) -> None:
        try:
            self.completed.emit(execute_endpoint(
                *self.arguments, auth_context=self.auth_context, auth_mode=self.auth_mode,
            ))
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(
        self,
        startup_progress: Callable[[int, str], None] | None = None,
    ) -> None:
        super().__init__()
        report_progress = startup_progress or (lambda _value, _status: None)
        report_progress(10, "Loading settings")
        self._startup_catalog_error = ""
        raw_settings = self._load_settings()
        report_progress(25, "Loading API catalog")
        self.catalog = self._load_startup_catalog(raw_settings)
        self.services = self.catalog.services
        self.current_endpoint: Endpoint | None = None
        self._request_drafts: dict[str, tuple[dict[str, str], str]] = {}
        self._request_auth_modes: dict[str, str] = {}
        self.endpoint_items: dict[str, QTreeWidgetItem] = {}
        self.base_url_inputs: dict[str, QLineEdit] = {}
        self._request_thread: QThread | None = None
        self._request_worker: RequestWorker | None = None
        self.app_settings = AppSettings.from_dict(
            raw_settings,
            {service.name: service.default_base_url for service in self.services},
        )
        self.workspace_store = WorkspaceStore(WORKSPACE_DB_PATH)
        self.saved_request_store = SavedRequestStore(self.workspace_store)

        report_progress(40, "Building application shell")
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1600, 950)
        # Keep the shell narrower than any common display so a maximized window
        # never pushes content outside the frame.
        self.setMinimumSize(1120, 680)
        self._splitter_sizes_restored = False
        self._build_ui()
        report_progress(70, "Preparing service workspace")
        self._populate_services()
        report_progress(82, "Restoring workspace")
        self._restore_shell_state()
        report_progress(90, "Applying interface theme")
        theme.apply_theme(QApplication.instance(), self.app_settings.theme)
        self.suite_tab.refresh_theme()
        self.response.refresh_theme()
        self.parameters_seed_button.setIcon(icon("seed", theme.TEXT, 18))
        self.payload_seed_button.setIcon(icon("seed", theme.TEXT, 18))
        self.payload.refresh_theme()
        self.form_builder.refresh_theme()
        self.payload_form.refresh_theme()
        for icon_name, button in self.payload_fields_buttons.items():
            button.setIcon(icon(icon_name, theme.TEXT, 18))
        self.query_builder.refresh_theme()
        self.sort_builder.refresh_theme()
        report_progress(96, "Finalizing workspace")

    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QWidget()
        header.setObjectName("appHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 7, 14, 7)
        logo = QLabel()
        logo.setObjectName("appLogo")
        logo.setPixmap(app_pixmap(26))
        logo.setFixedSize(26, 26)
        logo.setScaledContents(True)
        header_layout.addWidget(logo)
        self.app_title = ElidingLabel()
        self.app_title.setObjectName("appTitle")
        header_layout.addWidget(self.app_title)
        self._refresh_app_title()
        header_layout.addStretch()
        self.environment_banner = QLabel()
        self.environment_banner.setObjectName("connectionBadge")
        self.environment_banner.setProperty("shellRegion", "environmentStatus")
        self.environment_banner.setToolTip(
            "Active environment. Change it in Settings > Environments."
        )
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["Light", "Dark", "System"])
        self.theme_combo.setCurrentText(self.app_settings.theme)
        self.theme_combo.setToolTip("Application theme")
        self.theme_combo.currentTextChanged.connect(self._theme_changed)
        header_layout.addWidget(self.theme_combo)
        help_button = QPushButton("?")
        help_button.setObjectName("iconButton")
        help_button.setToolTip("Help and keyboard shortcuts")
        help_button.clicked.connect(self._show_help)
        header_layout.addWidget(help_button)
        layout.addWidget(header)

        self.environment_toolbar = QFrame()
        self.environment_toolbar.setObjectName("environmentToolbar")
        self.environment_toolbar.setProperty("shellRegion", "environment")
        environment_toolbar_layout = QHBoxLayout(self.environment_toolbar)
        environment_toolbar_layout.setContentsMargins(12, 6, 12, 6)
        environment_toolbar_layout.setSpacing(8)

        environment_caption = QLabel("Target")
        environment_caption.setObjectName("environmentToolbarCaption")
        environment_caption.setProperty("fieldCaption", True)
        environment_toolbar_layout.addWidget(environment_caption)
        self.environment_selector = QComboBox()
        self.environment_selector.setObjectName("activeEnvironmentSelector")
        self.environment_selector.setMinimumWidth(150)
        self.environment_selector.setAccessibleName("Active environment")
        self.environment_selector.addItems(list(self.app_settings.environments))
        self.environment_selector.setCurrentText(self.app_settings.active_environment)
        self.environment_selector.currentTextChanged.connect(self._activate_environment)
        environment_toolbar_layout.addWidget(self.environment_selector)

        service_caption = QLabel("Service")
        service_caption.setObjectName("serviceToolbarCaption")
        service_caption.setProperty("fieldCaption", True)
        environment_toolbar_layout.addWidget(service_caption)
        self.service_selector = QComboBox()
        self.service_selector.setObjectName("activeServiceSelector")
        self.service_selector.setMinimumWidth(150)
        self.service_selector.setAccessibleName("Active service")
        self.service_selector.addItems([service.name for service in self.services])
        self.service_selector.setCurrentText(self.app_settings.active_service)
        self.service_selector.currentTextChanged.connect(self._active_service_changed)
        environment_toolbar_layout.addWidget(self.service_selector)

        self.base_url_label = ElidingLabel()
        self.base_url_label.setObjectName("environmentBaseUrl")
        self.base_url_label.setProperty("shellRegion", "environment")
        self.base_url_label.setProperty("monospace", True)
        self.base_url_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.base_url_label.setToolTip("Base URL for the active service")
        environment_toolbar_layout.addWidget(self.base_url_label, 1)
        environment_toolbar_layout.addWidget(self.environment_banner)
        self.edit_environment_button = QPushButton("Edit environment")
        self.edit_environment_button.setObjectName("editEnvironmentButton")
        self.edit_environment_button.setProperty("ghost", True)
        self.edit_environment_button.clicked.connect(self._edit_environment)
        environment_toolbar_layout.addWidget(self.edit_environment_button)
        layout.addWidget(self.environment_toolbar)

        self.base_url_inputs = {
            service.name: QLineEdit(self.app_settings.active.base_urls.get(service.name, ""))
            for service in self.services
        }
        # Compatibility aliases used by existing integrations and tests. The
        # values now live on the active environment profile, which is edited
        # from Settings rather than a toolbar.
        self.access_token = QLineEdit(self.app_settings.active.access_token)
        self.access_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key = QLineEdit(self.app_settings.active.api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.verify_ssl = QCheckBox()
        self.verify_ssl.setChecked(self.app_settings.active.verify_ssl)
        self.request_timeout = QSpinBox()
        self.request_timeout.setRange(1, 3600)
        self.request_timeout.setValue(self.app_settings.active.request_timeout)

        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.tabBar().hide()
        splitter = QSplitter()

        explorer = QWidget()
        explorer.setObjectName("endpointExplorer")
        explorer_layout = QVBoxLayout(explorer)
        explorer_layout.setContentsMargins(10, 10, 10, 10)
        explorer_header = QHBoxLayout()
        explorer_title = QLabel("API Explorer")
        explorer_title.setObjectName("apiExplorerPageTitle")
        explorer_title.setProperty("pageTitle", True)
        explorer_title.setProperty("workspaceTitle", True)
        explorer_header.addWidget(explorer_title)
        explorer_header.addStretch()
        explorer_layout.addLayout(explorer_header)
        explorer_description = QLabel(
            "Browse services, controllers, and endpoints from the active API catalog."
        )
        explorer_description.setObjectName("apiExplorerPageDescription")
        explorer_description.setProperty("pageDescription", True)
        explorer_description.setWordWrap(True)
        explorer_layout.addWidget(explorer_description)
        search_row = QHBoxLayout()
        self.endpoint_search = QLineEdit()
        self.endpoint_search.setAccessibleName("Search endpoints")
        self.endpoint_search.setPlaceholderText("Search endpoints...")
        self.endpoint_search.addAction(
            icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        self.endpoint_search.textChanged.connect(self._filter_endpoints)
        search_row.addWidget(self.endpoint_search)
        self.method_filter = QComboBox()
        self.method_filter.setAccessibleName("Filter endpoints by HTTP method")
        self.method_filter.addItem("All methods", "")
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            self.method_filter.addItem(method, method)
        self.method_filter.currentIndexChanged.connect(self._filter_endpoints)
        search_row.addWidget(self.method_filter)
        self.scope_filter = QComboBox()
        self.scope_filter.setAccessibleName("Filter favorite or recent endpoints")
        self.scope_filter.addItem("All endpoints", "")
        self.scope_filter.addItem("Favorites", "favorites")
        self.scope_filter.addItem("Recent", "recent")
        self.scope_filter.currentIndexChanged.connect(self._filter_endpoints)
        search_row.addWidget(self.scope_filter)
        explorer_layout.addLayout(search_row)
        self.endpoint_tree = QTreeWidget()
        self.endpoint_tree.setObjectName("endpointTree")
        self.endpoint_tree.setProperty("shellRegion", "explorer")
        self.endpoint_tree.setAccessibleName("Rest Tester endpoint catalog")
        self.endpoint_tree.setHeaderLabels(["Action / path", "Method / count"])
        self.endpoint_tree.setRootIsDecorated(True)
        self.endpoint_tree.setAnimated(False)
        self.endpoint_tree.itemSelectionChanged.connect(self._endpoint_selected)
        endpoint_header_view = self.endpoint_tree.header()
        endpoint_header_view.setStretchLastSection(False)
        endpoint_header_view.setMinimumSectionSize(40)
        endpoint_header_view.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        endpoint_header_view.setSectionResizeMode(
            1, QHeaderView.ResizeMode.Interactive
        )
        widths = self.app_settings.endpoint_column_widths
        self.endpoint_tree.setColumnWidth(0, widths[0] if widths else 255)
        self.endpoint_tree.setColumnWidth(1, widths[1] if len(widths) > 1 else 65)
        explorer_layout.addWidget(self.endpoint_tree)
        self.endpoint_tree_empty_state = attach_table_empty_state(
            self.endpoint_tree,
            icon_name="api-explorer",
            title="No APIs to show",
            guidance="This build ships without an API catalog. Load or create one to browse endpoints.",
        )
        self.endpoint_explorer = explorer
        splitter.addWidget(explorer)

        details = QWidget()
        details.setObjectName("requestWorkspace")
        details_layout = QVBoxLayout(details)
        details_layout.setContentsMargins(12, 10, 12, 10)
        details_layout.setSpacing(8)
        self.endpoint_title = QLabel("Select an endpoint")
        self.endpoint_title.setObjectName("endpointBreadcrumb")
        self.endpoint_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        details_layout.addWidget(self.endpoint_title)
        self.endpoint_source = QLabel("")
        self.endpoint_source.setObjectName("endpointSource")
        self.endpoint_source.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        endpoint_header = QHBoxLayout()
        endpoint_header.setSpacing(8)
        endpoint_identity = QFrame()
        endpoint_identity.setObjectName("endpointIdentity")
        endpoint_identity_layout = QHBoxLayout(endpoint_identity)
        endpoint_identity_layout.setContentsMargins(0, 0, 0, 0)
        endpoint_identity_layout.setSpacing(0)
        self.endpoint_method = QLabel("—")
        self.endpoint_method.setObjectName("endpointMethod")
        self.endpoint_method.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.endpoint_method.setMinimumWidth(76)
        self.endpoint_route = QLabel("Select an endpoint")
        self.endpoint_route.setObjectName("endpointRoute")
        self.endpoint_route.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        # Long routes must never force the window wider than the screen.
        self.endpoint_route.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.endpoint_route.setMinimumWidth(0)
        endpoint_identity_layout.addWidget(self.endpoint_method)
        endpoint_identity_layout.addWidget(self.endpoint_route, 1)
        endpoint_header.addWidget(endpoint_identity, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("endpointSendButton")
        self.send_button.setAccessibleName("Send request without verification")
        self.send_button.setIcon(icon("send", "#ffffff"))
        self.send_button.setProperty("accent", True)
        self.send_button.clicked.connect(self._send_without_verification)
        endpoint_header.addWidget(self.send_button)
        self.send_and_verify_button = QPushButton("Send and Verify")
        self.send_and_verify_button.setAccessibleName(
            "Send request and verify expected result"
        )
        self.send_and_verify_button.setIcon(icon("verify", theme.PRIMARY))
        self.send_and_verify_button.clicked.connect(self._send_request)
        endpoint_header.addWidget(self.send_and_verify_button)
        self.save_request_button = QPushButton("Save Request")
        self.save_request_button.setIcon(icon("save"))
        self.save_request_button.clicked.connect(self._save_current_request)
        endpoint_header.addWidget(self.save_request_button)
        self.add_to_suite_button = QPushButton("Add to Test Suite")
        self.add_to_suite_button.setToolTip(
            "Adds this endpoint, with the values currently entered, as a case in the open suite"
        )
        self.add_to_suite_button.setIcon(icon("add-to-suite", theme.TEXT_MUTED))
        self.add_to_suite_button.clicked.connect(self._add_to_suite)
        endpoint_header.addWidget(self.add_to_suite_button)
        self.favorite_button = QPushButton("Favorite")
        self.favorite_button.setObjectName("favoriteButton")
        self.favorite_button.setProperty("favorite", False)
        self.favorite_button.setIcon(icon("star", theme.WARN))
        self.favorite_button.clicked.connect(self._toggle_favorite)
        endpoint_header.addWidget(self.favorite_button)
        self.endpoint_more_actions = QToolButton()
        self.endpoint_more_actions.setObjectName("endpointMenuButton")
        self.endpoint_more_actions.setText("More")
        self.endpoint_more_actions.setIcon(icon("more", theme.TEXT_MUTED))
        self.endpoint_more_actions.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.endpoint_more_actions.setMinimumWidth(76)
        self.endpoint_more_actions.setFixedHeight(self.send_button.sizeHint().height())
        self.endpoint_more_actions.setToolTip("More request actions")
        self.endpoint_more_actions.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        request_menu = QMenu(self.endpoint_more_actions)
        request_menu.addAction("Generate Python Request", self._show_python_code)
        request_menu.addAction("Generate cURL", self._show_curl_code)
        request_menu.addAction("Copy URL", self._copy_url)
        request_menu.addAction("Open Source File", self._open_source)
        self.endpoint_more_actions.setMenu(request_menu)
        endpoint_header.addWidget(self.endpoint_more_actions)
        self.endpoint_header_layout = endpoint_header
        details_layout.addLayout(endpoint_header)
        details_layout.addWidget(self.endpoint_source)
        authentication_row = QHBoxLayout()
        authentication_row.addWidget(QLabel("Authentication"))
        self.request_authentication = QComboBox()
        populate_auth_choices(self.request_authentication, self.app_settings.active.auth_profiles)
        self.request_authentication.setToolTip(
            "Configure sign-in under Settings > Environments > Authentication. "
            "No authentication omits known credential headers; Manual never renews."
        )
        authentication_row.addWidget(self.request_authentication, 1)
        details_layout.addLayout(authentication_row)

        self.endpoint_content_tabs = QTabWidget()
        self.endpoint_content_tabs.setObjectName("endpointTabs")
        request_page = QWidget()
        request_page_layout = QVBoxLayout(request_page)
        request_page_layout.setContentsMargins(8, 8, 8, 8)
        request_page_layout.setSpacing(8)
        verification_row = QHBoxLayout()
        verification_row.addWidget(QLabel("Expected status for verification"))
        self.expected_status = QComboBox()
        self.expected_status.setEditable(True)
        self.expected_status.addItems(
            ["200-299", "200", "201", "202", "204", "400", "401", "403", "404"]
        )
        self.expected_status.setToolTip(
            "Used by Send and verify from the endpoint More actions menu"
        )
        verification_row.addWidget(self.expected_status)
        verification_row.addStretch()
        request_page_layout.addLayout(verification_row)
        self.request_tabs = QTabWidget()
        self.request_tabs.setObjectName("requestBuilderTabs")

        parameters_page = QFrame()
        parameters_page.setObjectName("parametersCard")
        parameters_layout = QVBoxLayout(parameters_page)
        parameters_layout.setContentsMargins(10, 10, 10, 10)
        parameters_layout.setSpacing(8)
        parameters_heading = QHBoxLayout()
        parameters_title = QLabel("Parameters")
        parameters_title.setProperty("sectionTitle", True)
        parameters_heading.addWidget(parameters_title)
        self.parameter_count = QLabel("0")
        self.parameter_count.setObjectName("countBadge")
        parameters_heading.addWidget(self.parameter_count)
        parameters_heading.addStretch()
        parameters_layout.addLayout(parameters_heading)
        self.parameters = QTableWidget(0, 5)
        self.parameters.setHorizontalHeaderLabels(["Source", "Name", "Type", "Required", "Value"])
        self.parameters.setObjectName("parametersTable")
        self.parameters.verticalHeader().setVisible(False)
        self.parameters.verticalHeader().setDefaultSectionSize(38)
        self.parameters.setShowGrid(False)
        self.parameters.setAlternatingRowColors(False)
        self.parameters.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        parameters_layout.addWidget(self.parameters)
        self.parameters_seed_button = QPushButton("Seed parameter values")
        self.parameters_seed_button.clicked.connect(self._seed_parameters)
        parameters_layout.addWidget(self.parameters_seed_button)

        query_page = QWidget()
        query_layout = QVBoxLayout(query_page)
        query_layout.setContentsMargins(0, 0, 0, 0)
        self.query_enabled = QCheckBox("Include Filter query parameter")
        self.query_enabled.setChecked(True)
        query_layout.addWidget(self.query_enabled)
        self.query_builder = QueryBuilder()
        self.query_enabled.toggled.connect(self.query_builder.setEnabled)
        query_layout.addWidget(self.query_builder)
        self.query_tab_index = self.request_tabs.addTab(query_page, "Query")

        sort_page = QWidget()
        sort_layout = QVBoxLayout(sort_page)
        sort_layout.setContentsMargins(0, 0, 0, 0)
        self.sort_enabled = QCheckBox("Include Sort query parameter")
        self.sort_enabled.setChecked(True)
        sort_layout.addWidget(self.sort_enabled)
        self.sort_builder = SortBuilder()
        self.sort_enabled.toggled.connect(self.sort_builder.setEnabled)
        sort_layout.addWidget(self.sort_builder)
        self.sort_tab_index = self.request_tabs.addTab(sort_page, "Sort")

        self.form_builder = FormBuilder()
        self.form_tab_index = self.request_tabs.addTab(self.form_builder, "Form")

        self.payload_form = PayloadForm()
        payload_page = QWidget()
        payload_page_layout = QVBoxLayout(payload_page)
        payload_page_layout.addWidget(self.payload_form)
        payload_buttons = QHBoxLayout()
        self.payload_fields_buttons = {
            "seed": QPushButton(),
            "copy": QPushButton(),
        }
        self.payload_fields_buttons["seed"].setToolTip("Seed all payload fields")
        self.payload_fields_buttons["seed"].setAccessibleName(
            "Seed all payload fields"
        )
        self.payload_fields_buttons["seed"].clicked.connect(self.payload_form.seed)
        self.payload_fields_buttons["copy"].setToolTip("Copy payload form to JSON")
        self.payload_fields_buttons["copy"].setAccessibleName(
            "Copy payload form to JSON"
        )
        self.payload_fields_buttons["copy"].clicked.connect(self._form_to_json)
        payload_buttons.addWidget(self.payload_fields_buttons["seed"])
        payload_buttons.addWidget(self.payload_fields_buttons["copy"])
        payload_buttons.addStretch()
        self.payload_fields_layout = payload_buttons
        payload_page_layout.addLayout(payload_buttons)
        self.payload_tab_index = self.request_tabs.addTab(payload_page, "Payload fields")

        json_page = QWidget()
        json_layout = QVBoxLayout(json_page)
        json_row = QHBoxLayout()
        json_row.addWidget(QLabel("JSON payload sent with the request"))
        self.payload_seed_button = QPushButton()
        self.payload_seed_button.setToolTip("Generate or seed request payload data")
        self.payload_seed_button.setAccessibleName(
            "Generate or seed request payload data"
        )
        self.payload_seed_button.clicked.connect(self._seed_data)
        json_row.addStretch()
        json_row.addWidget(self.payload_seed_button)
        self.payload_seed_layout = json_row
        json_layout.addLayout(json_row)
        self.payload = RequestBodyEditor()
        json_layout.addWidget(self.payload)
        self.json_tab_index = self.request_tabs.addTab(json_page, "Payload JSON")

        request_builder_splitter = QSplitter(Qt.Orientation.Horizontal)
        request_builder_splitter.setObjectName("requestBuilderSplitter")
        request_builder_splitter.setChildrenCollapsible(False)
        request_builder_splitter.setHandleWidth(7)
        request_builder_splitter.addWidget(parameters_page)
        request_builder_splitter.addWidget(self.request_tabs)
        request_builder_splitter.setStretchFactor(0, 1)
        request_builder_splitter.setStretchFactor(1, 2)
        request_builder_splitter.setSizes(self.app_settings.request_builder_sizes)
        request_page_layout.addWidget(request_builder_splitter, 1)
        self.request_builder_splitter = request_builder_splitter

        self.endpoint_content_tabs.addTab(request_page, "Request")

        self.documentation = QPlainTextEdit()
        self.documentation.setReadOnly(True)
        self.endpoint_content_tabs.addTab(self.documentation, "Documentation")
        self.examples = QPlainTextEdit()
        self.examples.setReadOnly(True)
        self.endpoint_content_tabs.addTab(self.examples, "Examples")
        self.endpoint_history = QTableWidget(0, 5)
        self.endpoint_history.setHorizontalHeaderLabels(
            ["Timestamp", "Environment", "Status", "Duration", "URL"]
        )
        self.endpoint_history.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Stretch
        )
        self.endpoint_content_tabs.addTab(self.endpoint_history, "Test History")
        self.endpoint_history.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        attach_table_empty_state(
            self.endpoint_history,
            icon_name="renew",
            title="No history for this endpoint",
            guidance="Send this endpoint once and every later call is recorded here with its status and duration.",
        )
        self.endpoint_content_tabs.setMinimumHeight(430)
        self.response = ResponseViewer()
        self.response.setMinimumHeight(360)
        self.request_response_accordion = AccordionScrollArea(
            content_margins=(1, 12, 0, 12)
        )
        self.request_section = self.request_response_accordion.add_section(
            "Endpoint request and documentation",
            self.endpoint_content_tabs,
            expanded=self.app_settings.accordion_states.get(
                "endpoint_request", True
            ),
        )
        self.response_section = self.request_response_accordion.add_section(
            "Response",
            self.response,
            expanded=self.app_settings.accordion_states.get(
                "endpoint_response", True
            ),
        )
        details_layout.addWidget(self.request_response_accordion, 1)
        details_shell, left_shadow, top_shadow = inset_shadow_detail_pane(
            details,
            shell_name="requestWorkspaceShell",
        )
        self.endpoint_content_area = details
        self.endpoint_content_shell = details_shell
        self.endpoint_content_inset_shadow = left_shadow
        self.endpoint_content_top_inset_shadow = top_shadow
        # Covers the request workspace while no catalog is loaded: without an
        # endpoint list the Send/Save controls below have nothing to act on,
        # so the pane states the reason and offers the way out.
        self.workspace_empty_state = OverlayEmptyState(details)
        self.workspace_empty_state.set_content(
            icon_name="api-explorer",
            title="No API catalog",
            guidance="Please go to Settings to create or select a catalog.",
            action_text="Settings",
            action_callback=self._open_settings_for_catalog,
        )
        splitter.addWidget(details_shell)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        self.endpoint_splitter_handle = splitter.handle(1)
        self.endpoint_splitter_handle.setObjectName("endpointExplorerHandle")
        splitter.setSizes(self.app_settings.splitter_sizes)
        self.main_splitter = splitter

        self.workspace_tabs.addTab(splitter, "Endpoint explorer")
        self.suite_tab = SuiteTab(
            self.catalog,
            SUITES_DIR,
            self.workspace_store,
            self._environment,
        )
        self.workspace_tabs.addTab(self.suite_tab, "Test suites")
        self.data_runner_tab = DataRunnerTab(self.catalog, self._execution_environment_snapshot)
        self.workspace_tabs.addTab(self.data_runner_tab, "Data Runner")
        self.load_testing_tab = LoadTestingTab(self.catalog, self._execution_environment_snapshot)
        self.workspace_tabs.addTab(self.load_testing_tab, "Load Testing")
        self.saved_requests_page = SavedRequestsPage(self.saved_request_store)
        self.saved_requests_page.request_selected.connect(self._open_saved_request)
        self.workspace_tabs.addTab(self.saved_requests_page, "Saved Requests")
        self.collections_page = CollectionsPage(self.saved_request_store)
        self.collections_page.run_collection_requested.connect(self._run_collection)
        self.workspace_tabs.addTab(self.collections_page, "Collections")
        self.environment_page = EnvironmentManagerPage(
            self.app_settings, [service.name for service in self.services]
        )
        environment_labels = self.environment_page.findChildren(QLabel)
        if environment_labels:
            environment_labels[0].setObjectName("environmentPageTitle")
            environment_labels[0].setProperty("pageTitle", True)
            environment_labels[0].setProperty("workspaceTitle", True)
        if len(environment_labels) > 1:
            environment_labels[1].setObjectName("environmentPageDescription")
            environment_labels[1].setProperty("pageDescription", True)
        self.environment_page.changed.connect(self._refresh_environment_ui)
        self.environment_page.activated.connect(self._activate_environment)
        self.workspace_tabs.addTab(self.environment_page, "Environments")
        settings_page = QWidget()
        settings_layout = QVBoxLayout(settings_page)
        settings_heading = QLabel("Settings")
        settings_heading.setObjectName("settingsPageTitle")
        settings_heading.setProperty("pageTitle", True)
        settings_heading.setProperty("workspaceTitle", True)
        settings_layout.addWidget(settings_heading)
        settings_hint = QLabel(
            "Appearance is set from the header theme selector. Base URLs, "
            "credentials, variables, SSL verification, and the request timeout "
            "all live on the active environment in the Environments workspace."
        )
        settings_hint.setObjectName("settingsPageDescription")
        settings_hint.setProperty("pageDescription", True)
        settings_hint.setWordWrap(True)
        settings_layout.addWidget(settings_hint)

        catalog_box = QFrame()
        catalog_box.setObjectName("settingsCard")
        catalog_layout = QVBoxLayout(catalog_box)
        catalog_heading = QLabel("API catalog")
        catalog_heading.setProperty("sectionTitle", True)
        catalog_layout.addWidget(catalog_heading)
        catalog_description = QLabel(
            "The catalog defines every service, module, and endpoint this "
            "application can call. Load a catalog built with the Catalog "
            "Builder to test a different set of services without rebuilding."
        )
        catalog_description.setWordWrap(True)
        catalog_layout.addWidget(catalog_description)
        self.catalog_path_label = QLabel()
        self.catalog_path_label.setWordWrap(True)
        catalog_layout.addWidget(self.catalog_path_label)
        catalog_buttons = QHBoxLayout()
        load_catalog_button = QPushButton("Load catalog...")
        load_catalog_button.setIcon(icon("save", "#0878F9"))
        load_catalog_button.clicked.connect(self._choose_catalog)
        catalog_buttons.addWidget(load_catalog_button)
        reload_button = QPushButton("Reload")
        reload_button.setToolTip("Re-read the current catalog from disk")
        reload_button.clicked.connect(lambda: self._load_catalog_from(self._catalog_path()))
        catalog_buttons.addWidget(reload_button)
        default_button = QPushButton("Use bundled catalog")
        default_button.clicked.connect(lambda: self._load_catalog_from(CATALOG_PATH, True))
        catalog_buttons.addWidget(default_button)
        builder_button = QPushButton("Open Catalog Builder")
        builder_button.setIcon(icon("settings", "#0878F9"))
        builder_button.clicked.connect(self._open_catalog_builder)
        catalog_buttons.addWidget(builder_button)
        catalog_buttons.addStretch()
        catalog_layout.addLayout(catalog_buttons)
        settings_layout.addWidget(catalog_box)

        settings_layout.addStretch()
        self.workspace_tabs.addTab(settings_page, "Settings")
        self.settings_page = settings_page
        self._refresh_catalog_label()
        self.refresh_catalog_presence()

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        navigation_panel = QWidget()
        navigation_panel.setObjectName("navigationPanel")
        navigation_panel.setFixedWidth(145)
        navigation_layout = QVBoxLayout(navigation_panel)
        navigation_layout.setContentsMargins(0, 0, 0, 8)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigationRail")
        self.navigation.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.navigation.setTextElideMode(Qt.TextElideMode.ElideRight)
        for name, icon_name in (
            ("API Explorer", "api-explorer"),
            ("Test Suites", "test-suites"),
            ("Data Runner", "data-runner"),
            ("Load Testing", "load-testing"),
            ("Saved Requests", "bookmark"),
            ("Collections", "folder"),
            ("Environments", "globe"),
            ("Settings", "settings"),
        ):
            item = QListWidgetItem(icon(icon_name, "#DCE9F8"), name)
            item.setToolTip(name)
            self.navigation.addItem(item)
        self.navigation.currentRowChanged.connect(self.workspace_tabs.setCurrentIndex)
        self.workspace_tabs.currentChanged.connect(self._workspace_tab_changed)
        self.navigation.setCurrentRow(0)
        navigation_layout.addWidget(self.navigation, 1)
        method_counts: dict[str, int] = {}
        total = 0
        for service in self.services:
            for endpoint in service.endpoints:
                total += 1
                method_counts[endpoint.method] = method_counts.get(endpoint.method, 0) + 1
        stats = QLabel(
            "Total Endpoints\n"
            f"<b>{total}</b><br><br>"
            + "<br>".join(
                f"<span style='color:{theme.method_color(method)}'><b>{method}</b></span>"
                f" &nbsp; {count}"
                for method, count in sorted(method_counts.items())
            )
        )
        stats.setObjectName("navigationStats")
        stats.setTextFormat(Qt.TextFormat.RichText)
        stats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        navigation_layout.addWidget(stats)
        version = QLabel("v2.1.0")
        version.setObjectName("navigationVersion")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        navigation_layout.addWidget(version)
        body_layout.addWidget(navigation_panel)
        body_layout.addWidget(self.workspace_tabs, 1)
        layout.addWidget(body, 1)
        self.setCentralWidget(central)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._send_request)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.endpoint_search.setFocus)
        self._allow_splitter_shrink()

    def _allow_splitter_shrink(self) -> None:
        """Let splitter panes compress instead of forcing the window wider."""
        for splitter in self.findChildren(QSplitter):
            if splitter.orientation() is not Qt.Orientation.Horizontal:
                continue
            for index in range(splitter.count()):
                pane = splitter.widget(index)
                policy = pane.sizePolicy()
                policy.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
                pane.setSizePolicy(policy)
                if pane.property("preserveMinimumWidth") is not True:
                    pane.setMinimumWidth(0)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().showEvent(event)
        if not self._splitter_sizes_restored:
            self._splitter_sizes_restored = True
            QTimer.singleShot(0, self._restore_splitter_sizes)

    def _restore_splitter_sizes(self) -> None:
        sizes = list(self.app_settings.request_builder_sizes)
        if sizes and all(size > 0 for size in sizes):
            self.request_builder_splitter.setSizes(sizes)

    def _workspace_tab_changed(self, index: int) -> None:
        """Keep the left navigation highlight in sync with programmatic tab changes."""
        if index < 0 or index >= self.navigation.count():
            return
        if self.navigation.currentRow() == index:
            return
        blocked = self.navigation.blockSignals(True)
        self.navigation.setCurrentRow(index)
        self.navigation.blockSignals(blocked)

    def _load_startup_catalog(self, raw_settings: dict[str, Any]) -> Catalog:
        """Loads the startup catalog, tolerating builds that ship without one.

        The executable can be packaged without ``data/api_catalog.json`` so a
        team supplies its own. A missing or damaged file must not stop the
        window from opening: the user still needs Settings and the Catalog
        Builder to load or generate one, which an exception at construction
        time would make unreachable.
        """
        path = self._startup_catalog_path(raw_settings)
        try:
            return load_catalog(path)
        except (OSError, ValueError, KeyError) as exc:
            self._startup_catalog_error = f"{type(exc).__name__}: {exc}"
            return EMPTY_CATALOG

    def _startup_catalog_path(self, raw_settings: dict[str, Any]) -> Path:
        """Honours a user-loaded catalog, falling back to the bundled one."""
        configured = str(raw_settings.get("catalog_path", "")).strip()
        if configured:
            candidate = Path(configured)
            if candidate.is_file():
                return candidate
        return CATALOG_PATH

    def _catalog_path(self) -> Path:
        """The catalog in use: a user-loaded one, or the bundled default."""
        configured = self.app_settings.catalog_path.strip()
        if configured and Path(configured).is_file():
            return Path(configured)
        return CATALOG_PATH

    def _open_settings_for_catalog(self) -> None:
        """Takes the user to Settings, where a catalog can be loaded or built."""
        page = getattr(self, "settings_page", None)
        if page is None:
            return
        index = self.workspace_tabs.indexOf(page)
        if index < 0:
            return
        # Driving the rail keeps its highlight in step; it owns the tab index.
        navigation = getattr(self, "navigation", None)
        if navigation is not None and navigation.count() > index:
            navigation.setCurrentRow(index)
        else:
            self.workspace_tabs.setCurrentIndex(index)

    def refresh_catalog_presence(self) -> None:
        """Shows or hides the no-catalog messaging across the explorer page.

        Replaces the startup dialog: an empty explorer and a workspace that
        explains itself are less intrusive than a modal the user must dismiss
        before seeing either.
        """
        overlay = getattr(self, "workspace_empty_state", None)
        if overlay is not None:
            overlay.set_active(not self.services)
        self._sync_endpoint_tree_empty_state(bool(self.services))

    def _refresh_app_title(self) -> None:
        """Shows the catalog's own name beside the icon, or the product name."""
        label = getattr(self, "app_title", None)
        if label is None:
            return
        label.setText(display_name(getattr(self.catalog, "name", "")))

    def _refresh_catalog_label(self) -> None:
        label = getattr(self, "catalog_path_label", None)
        if label is None:
            return
        path = self._catalog_path()
        if not path.is_file():
            label.setText(
                f"No catalog loaded.\nThis build ships without one — use "
                f"Load catalog or Open Catalog Builder to supply it.\n"
                f"Expected at: {path}"
            )
            return
        origin = "bundled" if path == CATALOG_PATH else "loaded from Settings"
        label.setText(
            f"{path}\n{len(self.services)} services and "
            f"{sum(len(service.endpoints) for service in self.services)} "
            f"endpoints ({origin})."
        )

    def _choose_catalog(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "Load API catalog", str(self._catalog_path().parent), "Catalog (*.json)"
        )
        if selected:
            self._load_catalog_from(Path(selected))

    def _open_catalog_builder(self) -> None:
        from .catalog_builder_ui import CatalogBuilderWindow

        window = CatalogBuilderWindow(self._catalog_path(), self)
        window.setWindowFlag(Qt.WindowType.Window, True)
        window.catalog_saved.connect(self._catalog_builder_saved)
        # Held on the window so Python does not collect it while it is shown.
        self._builder_window = window
        window.show()

    def _catalog_builder_saved(self, path: str) -> None:
        if (
            QMessageBox.question(
                self,
                "Load saved catalog",
                f"The Catalog Builder saved:\n{path}\n\nLoad it now?",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self._load_catalog_from(Path(path))

    def _load_catalog_from(self, path: Path, bundled: bool = False) -> bool:
        """Swaps in a different catalog and rebuilds everything derived from it."""
        try:
            catalog = load_catalog(path)
        except (OSError, ValueError, KeyError) as exc:
            QMessageBox.critical(
                self,
                "Cannot load catalog",
                f"{path}\n\n{type(exc).__name__}: {exc}",
            )
            return False
        if not catalog.services:
            QMessageBox.warning(
                self, "Empty catalog", f"{path} contains no services."
            )
            return False

        self.catalog = catalog
        self.services = catalog.services
        self.current_endpoint = None
        self._request_drafts.clear()
        self._request_auth_modes.clear()
        self.app_settings.catalog_path = "" if bundled else str(path)

        service_defaults = {
            service.name: service.default_base_url for service in self.services
        }
        for profile in self.app_settings.environments.values():
            for name, default_url in service_defaults.items():
                profile.base_urls.setdefault(name, default_url)
        if self.app_settings.active_service not in service_defaults:
            self.app_settings.active_service = next(iter(service_defaults))

        self.base_url_inputs = {
            name: QLineEdit(self.app_settings.active.base_urls.get(name, ""))
            for name in service_defaults
        }
        # Favourites and recents point at endpoint ids that may not exist in
        # the new catalog, so drop the ones that no longer resolve.
        self._populate_services()
        known = set(self.endpoints_by_id)
        self.app_settings.favorites = [
            item for item in self.app_settings.favorites if item in known
        ]
        self.app_settings.recent_endpoints = [
            item for item in self.app_settings.recent_endpoints if item in known
        ]

        self.suite_tab.refresh_catalog(catalog)
        self.data_runner_tab.refresh_catalog(catalog)
        self.load_testing_tab.refresh_catalog(catalog)
        self.environment_page.set_service_names(list(service_defaults))

        self._sync_compatibility_controls()
        self._refresh_app_title()
        self._refresh_catalog_label()
        self.refresh_catalog_presence()
        self._save_settings()
        QMessageBox.information(
            self,
            "Catalog loaded",
            f"Loaded {len(self.services)} services and {len(known)} endpoints.",
        )
        return True

    def _environment(self) -> dict[str, Any]:
        """Current credentials and base URLs, shared with the suite runner."""
        self._sync_profile_from_compatibility_controls()
        return {
            "base_urls": {
                name: box.text().strip() for name, box in self.base_url_inputs.items()
            },
            "access_token": self.access_token.text(),
            "api_key": self.api_key.text(),
            "verify_ssl": self.verify_ssl.isChecked(),
            "request_timeout": self.request_timeout.value(),
            "variables": dict(self.app_settings.active.variables),
            "custom_headers": dict(self.app_settings.active.custom_headers),
            "auth_context": AuthenticationContext.from_environment(self.app_settings.active),
        }

    def _execution_environment_snapshot(self) -> ExecutionEnvironmentSnapshot:
        """Builds the Data Runner's frozen environment context from live UI state."""
        env = self._environment()
        return ExecutionEnvironmentSnapshot(
            environment_id=self.app_settings.active_environment,
            environment_name=self.app_settings.active_environment,
            base_urls=env["base_urls"],
            verify_ssl=env["verify_ssl"],
            timeout=float(env["request_timeout"]),
            variables=env["variables"],
            custom_headers=env["custom_headers"],
            access_token=env["access_token"],
            api_key=env["api_key"],
            auth_context=env["auth_context"],
        )

    def _add_to_suite(self) -> None:
        endpoint = self.current_endpoint
        if endpoint is None:
            QMessageBox.information(self, "Select endpoint", "Select an endpoint first.")
            return
        try:
            payload = (
                json.loads(self.payload.toPlainText())
                if self.payload.toPlainText().strip()
                else None
            )
        except json.JSONDecodeError as exc:
            QMessageBox.warning(self, "Invalid payload", f"Payload is not valid JSON: {exc}")
            return
        try:
            self._prepared_request()
        except (ValueError, AuthError) as exc:
            QMessageBox.warning(self, "Invalid request", str(exc))
            return
        name = self.suite_tab.prompt_case_name(endpoint, self)
        if name is None:
            return
        self.suite_tab.add_case_for_endpoint(endpoint, self._current_values(), payload, name)
        self.suite_tab.expected_status.setCurrentText(self.expected_status.currentText())
        if self.suite_tab.current_case is not None:
            self.suite_tab.current_case.authentication = self.request_authentication.currentData() or "inherit"
            self.suite_tab.refresh_authentication_choices()
        self.workspace_tabs.setCurrentWidget(self.suite_tab)

    def _current_values(self) -> dict[str, str]:
        values = {}
        for row in range(self.parameters.rowCount()):
            source = self.parameters.item(row, 0).text()
            name = self.parameters.item(row, 1).text()
            values[f"{source}:{name}"] = self.parameters.item(row, 4).text()
            if source == "query":
                values[parameter_enabled_key(source, name)] = (
                    "true"
                    if self.parameters.item(row, 0).checkState()
                    == Qt.CheckState.Checked
                    else "false"
                )
        for name in getattr(self, "_builder_parameters", set()):
            values[f"query:{name}"] = (
                self.query_builder.filter_string()
                if name == "Filter"
                else self.sort_builder.sort_string()
            )
            enabled = self.query_enabled if name == "Filter" else self.sort_enabled
            values[parameter_enabled_key("query", name)] = (
                "true" if enabled.isChecked() else "false"
            )
        values.update(self.form_builder.values())
        return values

    def _populate_services(self) -> None:
        self.endpoint_tree.clear()
        self.endpoint_items.clear()
        self.endpoints_by_id = {
            endpoint.id: endpoint
            for service in self.services
            for endpoint in service.endpoints
        }
        for service in self.services:
            service_item = QTreeWidgetItem([service.name, str(len(service.endpoints))])
            service_font = service_item.font(0)
            service_font.setBold(True)
            service_item.setFont(0, service_font)
            service_item.setFont(1, service_font)
            service_item.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
            service_item.setData(0, 257, service.name.lower())
            service_item.setData(0, 259, "service")
            service_item.setData(1, 259, len(service.endpoints))
            self.endpoint_tree.addTopLevelItem(service_item)
            controllers: dict[str, QTreeWidgetItem] = {}
            controller_counts: dict[str, int] = {}
            for endpoint in service.endpoints:
                controller_counts[endpoint.controller] = (
                    controller_counts.get(endpoint.controller, 0) + 1
                )
            for endpoint in service.endpoints:
                controller_item = controllers.get(endpoint.controller)
                if controller_item is None:
                    controller_item = QTreeWidgetItem(
                        [endpoint.controller, str(controller_counts[endpoint.controller])]
                    )
                    controller_item.setData(
                        0, 257, f"{service.name} {endpoint.controller}".lower()
                    )
                    controller_item.setData(0, 259, "controller")
                    controller_item.setData(
                        1, 259, controller_counts[endpoint.controller]
                    )
                    controller_font = controller_item.font(0)
                    controller_font.setBold(True)
                    controller_item.setFont(0, controller_font)
                    controller_item.setTextAlignment(
                        1, Qt.AlignmentFlag.AlignCenter
                    )
                    service_item.addChild(controller_item)
                    controllers[endpoint.controller] = controller_item
                item = QTreeWidgetItem(
                    [f"{endpoint.action}\n{endpoint.path}", endpoint.method]
                )
                item.setData(0, 256, endpoint.id)
                item.setData(
                    0,
                    257,
                    " ".join(
                        (
                            endpoint.service,
                            endpoint.controller,
                            endpoint.action,
                            endpoint.method,
                            endpoint.path,
                        )
                    ).lower(),
                )
                item.setData(0, 258, endpoint.method)
                item.setData(1, 258, endpoint.method)
                item.setData(0, 259, "endpoint")
                item.setData(1, 259, endpoint.method)
                item.setToolTip(
                    0, f"{endpoint.action}\n{endpoint.method} {endpoint.path}"
                )
                item.setToolTip(1, f"HTTP method: {endpoint.method}")
                item.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)
                item.setForeground(1, QColor(theme.method_color(endpoint.method)))
                controller_item.addChild(item)
                self.endpoint_items[endpoint.id] = item
                self._set_endpoint_favorite_state(item, endpoint.id)
        service_selector = getattr(self, "service_selector", None)
        if service_selector is not None:
            blocked = service_selector.blockSignals(True)
            service_selector.clear()
            service_selector.addItems([service.name for service in self.services])
            service_selector.setCurrentText(self.app_settings.active_service)
            service_selector.blockSignals(blocked)

    def _endpoint_selected(self) -> None:
        selected = self.endpoint_tree.selectedItems()
        if not selected:
            return
        endpoint_id = selected[0].data(0, 256)
        if not endpoint_id:
            return
        if self.current_endpoint is not None and self.current_endpoint.id != endpoint_id:
            self._request_auth_modes[self.current_endpoint.id] = self.request_authentication.currentData() or "inherit"
            self._request_drafts[self.current_endpoint.id] = (
                self._current_values(),
                self.payload.toPlainText(),
            )
        endpoint = self.endpoints_by_id[endpoint_id]
        draft = self._request_drafts.get(endpoint.id)
        draft_values = draft[0] if draft else {}
        self.current_endpoint = endpoint
        self.app_settings.active_service = endpoint.service
        self._refresh_environment_banner()
        self._remember_recent_endpoint(endpoint.id)
        self.endpoint_title.setText(
            f"{endpoint.service}  >  {endpoint.controller}  >  {endpoint.action}"
        )
        self.endpoint_method.setText(endpoint.method)
        self.endpoint_method.setProperty("method", endpoint.method)
        self.endpoint_method.style().unpolish(self.endpoint_method)
        self.endpoint_method.style().polish(self.endpoint_method)
        self.endpoint_route.setText(endpoint.path)
        self.endpoint_route.setToolTip(endpoint.path)
        self.endpoint_source.setText(
            f"{endpoint.action} · {endpoint.source_file}:{endpoint.source_line}"
        )
        self.endpoint_source.setToolTip(self.endpoint_source.text())
        self.endpoint_title.setToolTip(self.endpoint_title.text())
        self._refresh_favorite_button()

        filter_schema = self.catalog.filter_schema(endpoint.filter_entity)
        self.query_builder.set_schema(filter_schema)
        self.sort_builder.set_schema(filter_schema)
        draft_payload = endpoint.payload
        if draft and draft[1].strip():
            try:
                draft_payload = json.loads(draft[1])
            except json.JSONDecodeError:
                draft_payload = endpoint.payload
        self.payload_form.set_schema(
            self.catalog.payload_schema(endpoint.payload_schema), draft_payload
        )
        form_schema = self.catalog.form_schema(endpoint.form_schema)
        self.form_builder.set_schema(form_schema, draft_values)

        self._builder_parameters = {
            parameter.name
            for parameter in endpoint.parameters
            if filter_schema is not None
            and parameter.source == "query"
            and parameter.name in ("Filter", "Sort")
        }
        # Form fields are owned by the Form tab, not the parameter grid.
        self._table_parameters = [
            parameter
            for parameter in endpoint.parameters
            if parameter.name not in self._builder_parameters
            and not (form_schema is not None and parameter.source == "form")
        ]
        self.parameters.setRowCount(0)
        self.parameters.setRowCount(len(self._table_parameters))
        self.parameter_count.setText(str(len(self._table_parameters)))
        self._file_pickers = {}
        for row, parameter in enumerate(self._table_parameters):
            is_file = parameter.source == "form" and "file" in parameter.type.lower()
            values = [
                parameter.source,
                parameter.name,
                parameter.type,
                "yes" if parameter.required or parameter.source == "path" else "no",
                (
                    draft_values.get(f"{parameter.source}:{parameter.name}", "")
                    if draft
                    else ("" if is_file else seed_parameter(parameter))
                ),
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if column < 4 or is_file:
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 0 and parameter.source in {"path", "query"}:
                    cell.setFlags(
                        cell.flags() & ~Qt.ItemFlag.ItemIsUserCheckable
                    )
                    cell.setCheckState(
                        Qt.CheckState.Checked
                        if parameter_is_enabled(
                            draft_values, parameter.source, parameter.name
                        )
                        else Qt.CheckState.Unchecked
                    )
                    if parameter.source == "query":
                        cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                self.parameters.setItem(row, column, cell)
            if is_file:
                picker = FilePicker()
                picker.setText(
                    draft_values.get(f"{parameter.source}:{parameter.name}", "")
                    if draft
                    else ""
                )
                # Mirror the chosen path into the cell so _current_values still reads it.
                picker.changed.connect(
                    lambda text, item=self.parameters.item(row, 4): item.setText(text)
                )
                self.parameters.setCellWidget(row, 4, picker)
                self._file_pickers[row] = picker
            elif parameter.values:
                chooser = ValuePicker(list(parameter.values))
                chooser.setText(values[4])
                chooser.changed.connect(
                    lambda text, item=self.parameters.item(row, 4): item.setText(text)
                )
                self.parameters.setCellWidget(row, 4, chooser)

        self.request_tabs.setTabEnabled(
            self.query_tab_index, "Filter" in self._builder_parameters
        )
        self.request_tabs.setTabEnabled(
            self.sort_tab_index, "Sort" in self._builder_parameters
        )
        self.query_enabled.setChecked(
            parameter_is_enabled(draft_values, "query", "Filter")
        )
        self.sort_enabled.setChecked(
            parameter_is_enabled(draft_values, "query", "Sort")
        )
        self.request_tabs.setTabEnabled(self.form_tab_index, form_schema is not None)
        self.request_tabs.setTabEnabled(
            self.payload_tab_index, endpoint.payload_schema is not None
        )
        if draft:
            self.query_builder.set_filter_string(draft_values.get("query:Filter", ""))
            self.sort_builder.set_sort_string(draft_values.get("query:Sort", ""))
            self.payload.setPlainText(draft[1])
        else:
            self.payload.setPlainText(
                "" if endpoint.payload is None else json.dumps(endpoint.payload, indent=2)
            )
        self.expected_status.setCurrentText(endpoint.expected_status)
        populate_auth_choices(
            self.request_authentication, self.app_settings.active.auth_profiles,
            self._request_auth_modes.get(endpoint.id, "inherit"),
        )
        self._show_endpoint_documentation(endpoint)
        self._show_endpoint_examples(endpoint)
        self._load_endpoint_history(endpoint.id)
        self.response.clear()

    def _prepared_request(self):
        endpoint = self.current_endpoint
        if endpoint is None:
            raise ValueError("Select an endpoint first.")
        base_url = self.base_url_inputs[endpoint.service].text().strip()
        if not base_url:
            raise ValueError(f"Provide the {endpoint.service} base URL.")
        payload = (
            json.loads(self.payload.toPlainText())
            if self.payload.toPlainText().strip()
            else None
        )
        values, payload = self._resolve_environment_values(
            self._current_values(), payload
        )
        context = AuthenticationContext.from_environment(self.app_settings.active)
        mode = self.request_authentication.currentData() or "inherit"
        prepared = prepare_endpoint_request(
            endpoint,
            base_url,
            self.access_token.text(),
            self.api_key.text(),
            values,
            payload,
            self.app_settings.active.custom_headers,
            omitted_headers=context.secret_names if context.is_disabled(endpoint.service, mode) else frozenset(),
            provided_headers=context.managed_header_names(endpoint.service, mode),
            provided_query=context.managed_query_names(endpoint.service, mode),
        )
        applied = context.apply(endpoint.service, prepared.url, prepared.headers, mode, preview=True)
        return replace(
            prepared,
            headers=redact_headers(applied.headers, context.secret_names),
            query={**prepared.query, **dict(applied.query)},
        )

    def _resolve_environment_values(
        self, values: dict[str, str], payload: Any
    ) -> tuple[dict[str, str], Any]:
        variables = self.app_settings.active.variables
        resolved_values = {
            key: str(item)
            for key, item in apply_variables(values, variables).items()
        }
        return resolved_values, apply_variables(payload, variables)

    def _copy_url(self) -> None:
        try:
            text = self._prepared_request().resolved_url
        except (ValueError, AuthError) as exc:
            QMessageBox.warning(self, "Cannot build URL", str(exc))
            return
        QApplication.clipboard().setText(text)

    def _show_curl_code(self) -> None:
        try:
            text = generate_curl(self._prepared_request())
        except (ValueError, AuthError) as exc:
            QMessageBox.warning(self, "Cannot generate cURL", str(exc))
            return
        self._show_generated_code("cURL request", text)

    def _save_current_request(self) -> None:
        endpoint = self.current_endpoint
        if endpoint is None:
            QMessageBox.information(self, "Select endpoint", "Select an endpoint first.")
            return
        try:
            payload = (
                json.loads(self.payload.toPlainText())
                if self.payload.toPlainText().strip()
                else None
            )
            self._prepared_request()
        except (ValueError, AuthError) as exc:
            QMessageBox.warning(self, "Invalid request", str(exc))
            return
        suggested = f"{endpoint.method} {endpoint.action}"
        name, accepted = QInputDialog.getText(
            self, "Save request", "Name", text=suggested
        )
        if not accepted or not name.strip():
            return
        request = SavedRequest(
            endpoint_id=endpoint.id,
            name=name.strip(),
            values=self._current_values(),
            payload=payload,
            expected_status=self.expected_status.currentText(),
            authentication=self.request_authentication.currentData() or "inherit",
        )
        self.saved_request_store.upsert_request(request)
        self.saved_requests_page.refresh()
        self.collections_page.refresh()

    def _open_saved_request(self, request_id: str) -> None:
        request = self.saved_request_store.requests.get(request_id)
        endpoint_item = (
            self.endpoint_items.get(request.endpoint_id) if request is not None else None
        )
        if request is None or endpoint_item is None:
            QMessageBox.warning(
                self,
                "Endpoint unavailable",
                "The saved request's endpoint is no longer present in the catalog.",
            )
            return
        self._request_drafts[request.endpoint_id] = (
            dict(request.values),
            "" if request.payload is None else json.dumps(request.payload, indent=2),
        )
        self.current_endpoint = None
        self.endpoint_tree.clearSelection()
        self.endpoint_tree.setCurrentItem(endpoint_item)
        self.expected_status.setCurrentText(request.expected_status)
        populate_auth_choices(
            self.request_authentication, self.app_settings.active.auth_profiles, request.authentication,
        )
        self.navigation.setCurrentRow(0)

    def _run_collection(self, request_ids: list[str]) -> None:
        cases = self._collection_cases(request_ids)
        if not cases:
            QMessageBox.warning(
                self, "Collection cannot run", "No collection entries match the API catalog."
            )
            return
        collection_id = self.collections_page.current_collection_id()
        collection = self.saved_request_store.collections.get(collection_id)
        self.suite_tab.suite = TestSuite(
            name=collection.name if collection else "Collection", cases=cases
        )
        self.suite_tab._load_suite_into_ui()
        self.navigation.setCurrentRow(1)
        self.suite_tab.run()

    def _collection_cases(self, request_ids: list[str]) -> list[TestCase]:
        requests = [
            self.saved_request_store.requests[item]
            for item in request_ids
            if item in self.saved_request_store.requests
        ]
        return [
            TestCase(
                endpoint_id=request.endpoint_id,
                name=request.name,
                values=dict(request.values),
                payload=request.payload,
                expected_status=request.expected_status,
                authentication=request.authentication,
            )
            for request in requests
            if request.endpoint_id in self.endpoint_items
        ]

    def _show_python_code(self) -> None:
        try:
            text = generate_python(self._prepared_request())
        except (ValueError, AuthError) as exc:
            QMessageBox.warning(self, "Cannot generate code", str(exc))
            return
        self._show_generated_code("Python request", text)

    def _show_generated_code(self, title: str, text: str) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(760, 480)
        layout = QVBoxLayout(dialog)
        editor = QPlainTextEdit(text)
        editor.setReadOnly(True)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _open_source(self) -> None:
        endpoint = self.current_endpoint
        if endpoint is None:
            return
        service = next(item for item in self.services if item.name == endpoint.service)
        source = ROOT.parent / service.repository / endpoint.source_file
        if not source.exists():
            QMessageBox.warning(self, "Source unavailable", f"Could not find {source}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(source)))

    def _toggle_favorite(self) -> None:
        if self.current_endpoint is None:
            return
        endpoint_id = self.current_endpoint.id
        if endpoint_id in self.app_settings.favorites:
            self.app_settings.favorites.remove(endpoint_id)
        else:
            self.app_settings.favorites.append(endpoint_id)
        self._set_endpoint_favorite_state(
            self.endpoint_items.get(endpoint_id), endpoint_id
        )
        self._refresh_favorite_button()
        self._filter_endpoints()
        self._save_settings()

    def _set_endpoint_favorite_state(
        self, item: QTreeWidgetItem | None, endpoint_id: str
    ) -> None:
        if item is None:
            return
        favorite = endpoint_id in self.app_settings.favorites
        item.setData(0, 260, favorite)
        item.setToolTip(
            0,
            ("Favorite endpoint\n" if favorite else "")
            + str(item.data(0, 257) or "").title(),
        )

    def _refresh_favorite_button(self) -> None:
        button = getattr(self, "favorite_button", None)
        endpoint = self.current_endpoint
        if button is None or endpoint is None:
            return
        favorite = endpoint.id in self.app_settings.favorites
        button.setText("Favorited" if favorite else "Favorite")
        button.setProperty("favorite", favorite)
        button.setToolTip(
            "Remove endpoint from favorites"
            if favorite
            else "Add endpoint to favorites"
        )
        button.style().unpolish(button)
        button.style().polish(button)

    def _show_endpoint_documentation(self, endpoint: Endpoint) -> None:
        service = next(item for item in self.services if item.name == endpoint.service)
        self.documentation.setPlainText(
            endpoint_documentation(
                endpoint, service, ROOT.parent, ROOT / "data" / "openapi"
            )
        )

    def _show_endpoint_examples(self, endpoint: Endpoint) -> None:
        values = {
            f"{parameter.source}:{parameter.name}": seed_parameter(parameter)
            for parameter in endpoint.parameters
            if "file" not in parameter.type.lower()
        }
        self.examples.setPlainText(
            json.dumps(
                {
                    "values": values,
                    "payload": endpoint.payload,
                    "expected_status": endpoint.expected_status,
                },
                indent=2,
            )
        )

    def _load_endpoint_history(self, endpoint_id: str) -> None:
        self.endpoint_history.setRowCount(0)
        for record in self.workspace_store.endpoint_history(endpoint_id, limit=100):
            row = self.endpoint_history.rowCount()
            self.endpoint_history.insertRow(row)
            values = (
                record.get("timestamp", ""),
                record.get("environment", ""),
                record.get("status_code", ""),
                f"{record.get('elapsed_ms', 0)} ms",
                record.get("url", ""),
            )
            for column, value in enumerate(values):
                self.endpoint_history.setItem(row, column, QTableWidgetItem(str(value)))

    def _form_to_json(self) -> None:
        payload = self.payload_form.payload()
        if payload is None:
            return
        self.payload.setPlainText(json.dumps(payload, indent=2))
        self.request_tabs.setCurrentIndex(self.json_tab_index)

    def _seed_parameters(self) -> None:
        for row, parameter in enumerate(getattr(self, "_table_parameters", [])):
            if row in getattr(self, "_file_pickers", {}):
                continue  # A file path cannot be seeded.
            seeded = seed_parameter(parameter)
            chooser = self.parameters.cellWidget(row, 4)
            if isinstance(chooser, ValuePicker):
                chooser.setText(seeded)  # The widget mirrors itself into the cell.
            else:
                self.parameters.item(row, 4).setText(seeded)

    def _seed_data(self) -> None:
        if self.current_endpoint is None:
            return
        self._seed_parameters()
        if self.query_builder.schema is not None:
            self.query_builder.seed_all()
        if self.form_builder.schema is not None:
            self.form_builder.seed()
        if self.current_endpoint.payload_schema is not None:
            self.payload_form.seed()
            self._form_to_json()
        elif self.current_endpoint.payload is not None:
            self.payload.setPlainText(
                json.dumps(refresh_payload(self.current_endpoint.payload), indent=2)
            )

    def _send_request(self) -> None:
        self._start_request(self.expected_status.currentText())

    def _send_without_verification(self) -> None:
        self._start_request("100-599")

    def _start_request(self, expected_status: str) -> None:
        if self._request_thread is not None:
            return
        endpoint = self.current_endpoint
        if endpoint is None:
            QMessageBox.information(self, "Select endpoint", "Select an endpoint to test.")
            return
        base_url = self.base_url_inputs[endpoint.service].text().strip()
        if not base_url:
            QMessageBox.warning(self, "Missing base URL", f"Provide the {endpoint.service} base URL.")
            return
        values = self._current_values()
        try:
            payload = json.loads(self.payload.toPlainText()) if self.payload.toPlainText().strip() else None
        except json.JSONDecodeError as exc:
            QMessageBox.warning(self, "Invalid payload", f"Payload is not valid JSON: {exc}")
            return
        values, payload = self._resolve_environment_values(values, payload)

        self._save_settings()
        self.send_button.setEnabled(False)
        self.send_and_verify_button.setEnabled(False)
        self.response.status_label.setText("Running...")
        arguments = (
            endpoint,
            base_url,
            self.access_token.text(),
            self.api_key.text(),
            values,
            payload,
            expected_status,
            self.request_timeout.value(),
            self.verify_ssl.isChecked(),
            dict(self.app_settings.active.custom_headers),
        )
        self._request_endpoint = endpoint
        self._request_environment = self.app_settings.active_environment
        self._request_thread = QThread()
        self._request_worker = RequestWorker(
            arguments, AuthenticationContext.from_environment(self.app_settings.active),
            self.request_authentication.currentData() or "inherit",
        )
        self._request_worker.moveToThread(self._request_thread)
        self._request_thread.started.connect(self._request_worker.run)
        self._request_worker.completed.connect(self._request_completed)
        self._request_worker.failed.connect(self._request_failed)
        self._request_worker.completed.connect(self._request_thread.quit)
        self._request_worker.failed.connect(self._request_thread.quit)
        self._request_thread.finished.connect(self._request_finished)
        self._request_thread.start()

    def _request_completed(self, result: ApiResult) -> None:
        endpoint = getattr(self, "_request_endpoint", self.current_endpoint)
        if endpoint is None:
            return
        append_history(
            self.workspace_store, endpoint, result,
            getattr(self, "_request_environment", self.app_settings.active_environment),
        )
        if self.current_endpoint is None or self.current_endpoint.id != endpoint.id:
            return
        if self.app_settings.active_environment != getattr(
            self, "_request_environment", self.app_settings.active_environment,
        ):
            return
        self._load_endpoint_history(endpoint.id)
        self.response.show_result(result)
        item = self.endpoint_items[endpoint.id]
        item.setForeground(0, QColor(theme.PASS if result.passed else theme.FAIL))

    def _request_failed(self, message: str) -> None:
        self.response.show_error(message)

    def _request_finished(self) -> None:
        self.send_button.setEnabled(True)
        self.send_and_verify_button.setEnabled(True)
        if self._request_worker is not None:
            self._request_worker.deleteLater()
        if self._request_thread is not None:
            self._request_thread.deleteLater()
        self._request_worker = None
        self._request_thread = None

    def _load_settings(self) -> dict[str, Any]:
        if not SETTINGS_PATH.exists():
            return {}
        try:
            with SETTINGS_PATH.open(encoding="utf-8") as stream:
                return json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Settings warning", f"Could not load settings: {exc}")
            return {}

    def _save_settings(self) -> None:
        self._sync_profile_from_compatibility_controls()
        self.app_settings.splitter_sizes = self.main_splitter.sizes()
        self.app_settings.request_builder_sizes = self.request_builder_splitter.sizes()
        self.app_settings.accordion_states.update(
            {
                "endpoint_request": self.request_section.is_expanded(),
                "endpoint_response": self.response_section.is_expanded(),
            }
        )
        self.app_settings.endpoint_column_widths = [
            self.endpoint_tree.columnWidth(index)
            for index in range(self.endpoint_tree.columnCount())
        ]
        self.app_settings.expanded_nodes = self._expanded_node_keys()
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SETTINGS_PATH.open("w", encoding="utf-8") as stream:
            json.dump(self.app_settings.to_dict(), stream, indent=2)

    def _sync_profile_from_compatibility_controls(self) -> None:
        profile = self.app_settings.active
        profile.base_urls = {
            name: edit.text().strip() for name, edit in self.base_url_inputs.items()
        }
        profile.verify_ssl = self.verify_ssl.isChecked()
        profile.request_timeout = self.request_timeout.value()

    def _sync_compatibility_controls(self) -> None:
        profile = self.app_settings.active
        for service, edit in self.base_url_inputs.items():
            edit.setText(profile.base_urls.get(service, ""))
        self.verify_ssl.setChecked(profile.verify_ssl)
        self.request_timeout.setValue(profile.request_timeout)
        self.access_token.setText(profile.access_token)
        self.api_key.setText(profile.api_key)
        populate_auth_choices(
            self.request_authentication, profile.auth_profiles,
            self.request_authentication.currentData() or "inherit",
        )
        self.suite_tab.refresh_authentication_choices()
        self._refresh_environment_banner()

    def _refresh_environment_banner(self) -> None:
        banner = getattr(self, "environment_banner", None)
        if banner is None:
            return
        profile = self.app_settings.active
        service = self.app_settings.active_service or (
            self.services[0].name if self.services else ""
        )
        url = profile.base_urls.get(service, "")
        valid, status = validate_base_url(url)
        environment_selector = getattr(self, "environment_selector", None)
        if environment_selector is not None:
            blocked = environment_selector.blockSignals(True)
            environment_selector.setCurrentText(profile.name)
            environment_selector.blockSignals(blocked)
        service_selector = getattr(self, "service_selector", None)
        if service_selector is not None:
            blocked = service_selector.blockSignals(True)
            service_selector.setCurrentText(service)
            service_selector.blockSignals(blocked)
        base_url_label = getattr(self, "base_url_label", None)
        if base_url_label is not None:
            base_url_label.setText(url or "No base URL configured")
            base_url_label.setToolTip(url or "No base URL configured")
        banner.setText(
            f"{status}"
        )
        banner.setProperty("connected", valid)
        banner.style().unpolish(banner)
        banner.style().polish(banner)

    def _environment_changed(self) -> None:
        self._sync_compatibility_controls()
        self._save_settings()

    def _refresh_environment_ui(self) -> None:
        self.environment_page.refresh()
        self._sync_compatibility_controls()
        self._save_settings()

    def _activate_environment(self, name: str) -> None:
        if name not in self.app_settings.environments:
            return
        self.app_settings.active_environment = name
        self._refresh_environment_ui()

    def _edit_environment(self) -> None:
        profile = self.app_settings.active
        dialog = EnvironmentEditor(
            profile, [service.name for service in self.services], self
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        dialog.apply()
        self._sync_compatibility_controls()
        self._save_settings()

    def _active_service_changed(self, service_name: str) -> None:
        if service_name not in {service.name for service in self.services}:
            return
        self.app_settings.active_service = service_name
        for index in range(self.endpoint_tree.topLevelItemCount()):
            item = self.endpoint_tree.topLevelItem(index)
            if item.text(0) == service_name:
                item.setExpanded(True)
                self.endpoint_tree.scrollToItem(item)
                break
        self._save_settings()

    def _filter_endpoints(self) -> None:
        query = self.endpoint_search.text().strip().lower()
        method = str(self.method_filter.currentData() or "")
        scope = str(self.scope_filter.currentData() or "")
        any_visible = False
        for service_index in range(self.endpoint_tree.topLevelItemCount()):
            service_item = self.endpoint_tree.topLevelItem(service_index)
            visible_service = False
            for controller_index in range(service_item.childCount()):
                controller_item = service_item.child(controller_index)
                visible_controller = False
                for endpoint_index in range(controller_item.childCount()):
                    item = controller_item.child(endpoint_index)
                    matches_text = not query or query in str(item.data(0, 257) or "")
                    matches_method = not method or item.data(0, 258) == method
                    endpoint_id = str(item.data(0, 256) or "")
                    matches_scope = (
                        not scope
                        or (
                            scope == "favorites"
                            and endpoint_id in self.app_settings.favorites
                        )
                        or (
                            scope == "recent"
                            and endpoint_id in self.app_settings.recent_endpoints
                        )
                    )
                    visible = matches_text and matches_method and matches_scope
                    item.setHidden(not visible)
                    visible_controller = visible_controller or visible
                controller_item.setHidden(not visible_controller)
                if visible_controller and (query or method):
                    controller_item.setExpanded(True)
                visible_service = visible_service or visible_controller
            service_item.setHidden(not visible_service)
            if visible_service and (query or method):
                service_item.setExpanded(True)
            any_visible = any_visible or visible_service
        self._sync_endpoint_tree_empty_state(any_visible)

    def _sync_endpoint_tree_empty_state(self, any_visible: bool) -> None:
        """Keeps the tree placeholder in step with what the filters leave visible.

        Filtering hides items rather than removing rows, so the model's row
        count stays non-zero and the overlay's own signal-driven sync cannot
        see it. Visibility is therefore set here explicitly.
        """
        state = getattr(self, "endpoint_tree_empty_state", None)
        if state is None:
            return
        if not self.services:
            state.set_content(
                icon_name="api-explorer",
                title="No APIs to show",
                guidance="No API catalog is loaded. Use Settings to create or select one.",
            )
        else:
            state.set_content(
                icon_name="search",
                title="No matching endpoints",
                guidance="No endpoint matches the current search, method, or scope filter.",
            )
        state.setVisible(not any_visible)
        state.raise_()

    def _restore_shell_state(self) -> None:
        expanded = set(self.app_settings.expanded_nodes)
        for service_index in range(self.endpoint_tree.topLevelItemCount()):
            service_item = self.endpoint_tree.topLevelItem(service_index)
            service_item.setExpanded(f"service:{service_item.text(0)}" in expanded)
            for controller_index in range(service_item.childCount()):
                controller = service_item.child(controller_index)
                key = f"controller:{service_item.text(0)}:{controller.text(0)}"
                controller.setExpanded(key in expanded)
        self._sync_compatibility_controls()

    def _expanded_node_keys(self) -> list[str]:
        keys: list[str] = []
        for service_index in range(self.endpoint_tree.topLevelItemCount()):
            service_item = self.endpoint_tree.topLevelItem(service_index)
            if service_item.isExpanded():
                keys.append(f"service:{service_item.text(0)}")
            for controller_index in range(service_item.childCount()):
                controller = service_item.child(controller_index)
                if controller.isExpanded():
                    keys.append(
                        f"controller:{service_item.text(0)}:{controller.text(0)}"
                    )
        return keys

    def _remember_recent_endpoint(self, endpoint_id: str) -> None:
        recent = [
            item for item in self.app_settings.recent_endpoints if item != endpoint_id
        ]
        self.app_settings.recent_endpoints = [endpoint_id, *recent][:20]

    def _theme_changed(self, name: str) -> None:
        theme.apply_theme(QApplication.instance(), name)
        self.app_settings.theme = name
        self.suite_tab.refresh_theme()
        self.data_runner_tab.refresh_theme()
        self.load_testing_tab.refresh_theme()
        self.environment_page.refresh_theme()
        self.response.refresh_theme()
        self.parameters_seed_button.setIcon(icon("seed", theme.TEXT, 18))
        self.payload_seed_button.setIcon(icon("seed", theme.TEXT, 18))
        self.payload.refresh_theme()
        self.form_builder.refresh_theme()
        self.payload_form.refresh_theme()
        for icon_name, button in self.payload_fields_buttons.items():
            button.setIcon(icon(icon_name, theme.TEXT, 18))
        self.query_builder.refresh_theme()
        self.sort_builder.refresh_theme()
        self.endpoint_tree.viewport().update()
        self._refresh_highlighters()
        self._save_settings()

    def _refresh_highlighters(self) -> None:
        """Re-run JSON syntax highlighting so colours follow the active palette."""
        for editor in self.findChildren(JsonTextEdit):
            highlighter = getattr(editor, "highlighter", None)
            if highlighter is not None:
                highlighter.rehighlight()

    def _show_help(self) -> None:
        QMessageBox.information(
            self,
            "Rest Tester API Tester",
            "Use API Explorer to configure and execute an endpoint. "
            "Ctrl+Enter sends and verifies the current request. "
            "Environment settings apply to explorer and suite runs.",
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        self._save_settings()
        super().closeEvent(event)


def _claim_windows_taskbar_identity() -> None:
    """Makes Windows group the app under its own icon rather than Python's.

    Without an explicit AppUserModelID the shell attributes the window to the
    host interpreter, so the task bar shows the Python icon no matter what
    ``setWindowIcon`` is given.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "RestTester"
        )
    except (AttributeError, OSError):
        pass


def migrate_workspace_data() -> None:
    """Imports the pre-SQLite data files once, at application start.

    This lives in the entry point rather than ``MainWindow`` because it
    renames the files it consumes: constructing a window must never move a
    user's data on disk as a side effect.
    """
    store = WorkspaceStore(WORKSPACE_DB_PATH)
    try:
        migrate_legacy_files(
            store,
            run_history=LEGACY_HISTORY_PATH,
            suite_history=LEGACY_SUITE_HISTORY_PATH,
            saved_requests=LEGACY_SAVED_REQUESTS_PATH,
        )
    finally:
        store.close()


def run() -> int:
    _claim_windows_taskbar_identity()
    migrate_workspace_data()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(app_icon())
    theme.apply_theme(app)

    splash = SplashScreen()
    splash.show()
    app.processEvents()
    splash_timer = QElapsedTimer()
    splash_timer.start()

    def report_startup_progress(value: int, status: str) -> None:
        splash.set_progress(value, status)
        app.processEvents()

    window = MainWindow(startup_progress=report_startup_progress)
    splash.set_progress(100, "Workspace ready")
    app.processEvents()
    remaining = max(0, MINIMUM_VISIBLE_MS - splash_timer.elapsed())

    def reveal_main_window() -> None:
        window.show()
        splash.finish(window)

    QTimer.singleShot(remaining, reveal_main_window)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(run())
