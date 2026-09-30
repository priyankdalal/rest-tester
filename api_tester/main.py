from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from PyQt6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QItemSelectionModel,
    QObject,
    QPropertyAnimation,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QDesktopServices, QKeySequence, QMouseEvent, QShortcut
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
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from . import theme
from .branding import APP_NAME, display_name
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
from .environment import AppSettings
from .authentication import AuthError
from .request_editor import RequestEditor
from .request_auth import (
    AuthenticationContext,
    ConnectionState,
    populate_auth_choices,
)
from .data_runner.ui import DataRunnerTab
from .load_testing.ui import LoadTestingTab
from .environment_ui import EnvironmentEditor, EnvironmentManagerPage
from .execution.models import ExecutionEnvironmentSnapshot
from .auth_log import activity_log
from .icons import app_icon, app_pixmap, badged_icon, icon
from .documentation import endpoint_documentation
from .seeding import seed_parameter
from .saved_requests import (
    CollectionsPage,
    RequestCollection,
    SavedRequest,
    SavedRequestStore,
    SavedRequestsPage,
)
from .suite import TestCase, TestSuite, apply_variables
from .suite_ui import SuiteTab
from .splash import MINIMUM_VISIBLE_MS, SplashScreen
from .viewers import JsonTextEdit, ResponseViewer
from .widgets import (
    AccordionSection,
    AccordionScrollArea,
    expected_status_combo,
    ElidingLabel,
    HeaderConnectionPill,
    IconTextItemDelegate,
    OverlayEmptyState,
    attach_table_empty_state,
    inset_shadow_detail_pane,
    make_icon_text_item,
    set_icon_text_items_collapsed,
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


class EndpointTreeRow(QWidget):
    clicked = pyqtSignal()

    def __init__(self, endpoint: Endpoint, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("endpointTreeRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 6, 4)
        layout.setSpacing(10)

        self.method = QLabel(endpoint.method)
        self.method.setObjectName("endpointMethodPill")
        self.method.setProperty("method", endpoint.method.upper())
        self.method.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.method.setFixedSize(66, 32)
        self.method.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.method)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)
        self.action = ElidingLabel(endpoint.action)
        self.action.setObjectName("endpointTreeAction")
        self.action.setMinimumWidth(0)
        self.action.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.action.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.path = ElidingLabel(endpoint.path)
        self.path.setObjectName("endpointTreePath")
        self.path.setMinimumWidth(0)
        self.path.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.path.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        text_layout.addWidget(self.action)
        text_layout.addWidget(self.path)
        layout.addLayout(text_layout, 1)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt signature
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class EndpointTreeDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index) -> None:
        styled_option = QStyleOptionViewItem(option)
        styled_option.state &= ~QStyle.StateFlag.State_HasFocus
        super().paint(painter, styled_option, index)


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


class ConnectionProbe(QThread):
    """Resolves a service's authentication readiness off the GUI thread.

    ``AuthenticationContext.connection_state`` can block — a token method may
    consult its cached session — so the indicator is never computed inline.
    """

    resolved = pyqtSignal(int, object)

    def __init__(
        self,
        token: int,
        context: AuthenticationContext,
        service: str,
        mode: str,
        manual_headers: dict[str, str],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._token = token
        self._context = context
        self._service = service
        self._mode = mode
        self._manual_headers = dict(manual_headers)

    def run(self) -> None:
        try:
            state = self._context.connection_state(
                self._service, self._mode, self._manual_headers
            )
        except Exception as exc:  # pragma: no cover - defensive
            state = ConnectionState(False, "Unavailable", str(exc))
        self.resolved.emit(self._token, state)


def _request_editor_state(name: str) -> property:
    """Read-through to per-endpoint state that is rebuilt on every load."""
    return property(lambda self: getattr(self.request_editor, name))


class MainWindow(QMainWindow):
    _payload_schema = _request_editor_state("_payload_schema")
    _builder_parameters = _request_editor_state("_builder_parameters")
    _builder_value_edits = _request_editor_state("_builder_value_edits")
    _builder_buttons = _request_editor_state("_builder_buttons")
    _path_parameter_list = _request_editor_state("_path_parameter_list")
    _query_parameter_list = _request_editor_state("_query_parameter_list")
    _file_pickers = _request_editor_state("_file_pickers")

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
        self.endpoint_rows: dict[str, EndpointTreeRow] = {}
        self.base_url_inputs: dict[str, QLineEdit] = {}
        self._request_thread: QThread | None = None
        self._request_worker: RequestWorker | None = None
        self._connection_probes: set[ConnectionProbe] = set()
        self._notifications_seen = 0
        self._connection_token = 0
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
        self._build_ui()
        report_progress(70, "Preparing service workspace")
        self._populate_services()
        report_progress(82, "Restoring workspace")
        self._restore_shell_state()
        report_progress(90, "Applying interface theme")
        theme.apply_theme(QApplication.instance(), self.app_settings.theme)
        self._refresh_header_buttons()
        self.suite_tab.refresh_theme()
        self.response.refresh_theme()
        self.request_editor.refresh_theme()
        self.connection_pill.refresh_theme()
        self._refresh_connection_state()
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
        self.connection_pill = HeaderConnectionPill()
        self.connection_pill.edit_requested.connect(self._edit_environment)
        header_layout.addWidget(self.connection_pill)
        header_layout.addSpacing(8)
        # The connection state and its environment-edit action now live in the
        # header. These aliases keep the request-workspace call sites and
        # integrations working against the relocated controls.
        self.connection_indicator = self.connection_pill
        self.edit_environment_button = self.connection_pill.edit_button
        self.notifications_button = QToolButton()
        self.notifications_button.setObjectName("headerIconButton")
        self.notifications_button.setIconSize(QSize(18, 18))
        self.notifications_button.setFixedSize(32, 32)
        self.notifications_button.setToolTip("Recent authentication activity")
        self.notifications_button.setAccessibleName("Notifications")
        self.notifications_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.notifications_menu = QMenu(self.notifications_button)
        self.notifications_menu.aboutToShow.connect(
            self._populate_notifications_menu
        )
        self.notifications_button.setMenu(self.notifications_menu)
        header_layout.addWidget(self.notifications_button)
        self.theme_toggle_button = QToolButton()
        self.theme_toggle_button.setObjectName("headerIconButton")
        self.theme_toggle_button.setIconSize(QSize(18, 18))
        self.theme_toggle_button.setFixedSize(32, 32)
        self.theme_toggle_button.clicked.connect(self._toggle_theme)
        header_layout.addWidget(self.theme_toggle_button)
        self._refresh_header_buttons()
        help_button = QPushButton("?")
        help_button.setObjectName("iconButton")
        help_button.setToolTip("Help and keyboard shortcuts")
        help_button.clicked.connect(self._show_help)
        header_layout.addWidget(help_button)
        layout.addWidget(header)

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
        explorer_description = ElidingLabel(
            "Browse services, controllers, and endpoints from the active API catalog."
        )
        explorer_description.setObjectName("apiExplorerPageDescription")
        explorer_description.setProperty("pageDescription", True)
        explorer_description.setWordWrap(False)
        explorer_layout.addWidget(explorer_description)
        self.endpoint_search = QLineEdit()
        self.endpoint_search.setAccessibleName("Search endpoints")
        self.endpoint_search.setPlaceholderText("Search endpoints...")
        self.endpoint_search.addAction(
            icon("search"), QLineEdit.ActionPosition.LeadingPosition
        )
        self.endpoint_search.textChanged.connect(self._filter_endpoints)
        explorer_layout.addWidget(self.endpoint_search)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(8)
        self.method_filter = QComboBox()
        self.method_filter.setAccessibleName("Filter endpoints by HTTP method")
        self.method_filter.addItem("All methods", "")
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            self.method_filter.addItem(method, method)
        self.method_filter.currentIndexChanged.connect(self._filter_endpoints)
        filter_row.addWidget(self.method_filter, 1)
        self.scope_filter = QComboBox()
        self.scope_filter.setAccessibleName("Filter favorite or recent endpoints")
        self.scope_filter.addItem("All endpoints", "")
        self.scope_filter.addItem("Favorites", "favorites")
        self.scope_filter.addItem("Recent", "recent")
        self.scope_filter.currentIndexChanged.connect(self._filter_endpoints)
        filter_row.addWidget(self.scope_filter, 1)
        explorer_layout.addLayout(filter_row)
        self.endpoint_tree = QTreeWidget()
        self.endpoint_tree.setObjectName("endpointTree")
        self.endpoint_tree.setProperty("shellRegion", "explorer")
        self.endpoint_tree.setAccessibleName("Rest Tester endpoint catalog")
        self.endpoint_tree.setItemDelegate(EndpointTreeDelegate(self.endpoint_tree))
        self.endpoint_tree.setHeaderLabels(["Action / path", "Method / count"])
        self.endpoint_tree.setHeaderHidden(True)
        self.endpoint_tree.setRootIsDecorated(True)
        self.endpoint_tree.setAnimated(False)
        self.endpoint_tree.itemSelectionChanged.connect(self._endpoint_selected)
        endpoint_header_view = self.endpoint_tree.header()
        endpoint_header_view.setStretchLastSection(False)
        endpoint_header_view.setMinimumSectionSize(40)
        endpoint_header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        endpoint_header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.endpoint_tree.setColumnWidth(1, 52)
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
        endpoint_workspace = QWidget(details)
        endpoint_workspace_layout = QVBoxLayout(endpoint_workspace)
        endpoint_workspace_layout.setContentsMargins(0, 0, 0, 0)
        endpoint_workspace_layout.setSpacing(8)
        self.endpoint_workspace = endpoint_workspace
        self.endpoint_title = QLabel("Select an endpoint")
        self.endpoint_title.setObjectName("endpointBreadcrumb")
        self.endpoint_title.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        endpoint_workspace_layout.addWidget(self.endpoint_title)
        endpoint_header_widget = QWidget(endpoint_workspace)
        endpoint_header = QHBoxLayout(endpoint_header_widget)
        endpoint_header.setContentsMargins(0, 0, 0, 0)
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
        self.send_button = QToolButton()
        self.send_button.setObjectName("endpointSendButton")
        self.send_button.setText("Send")
        self.send_button.setAccessibleName("Send request without verification")
        self.send_button.setIcon(icon("send", "#ffffff"))
        self.send_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.send_button.setProperty("accent", True)
        self.send_button.clicked.connect(self._send_without_verification)
        self.send_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup
        )
        send_menu = QMenu(self.send_button)
        send_menu.addAction(
            icon("verify", theme.PRIMARY),
            "Send and Verify",
            self._send_request,
        )
        self.send_button.setMenu(send_menu)
        endpoint_header.addWidget(self.send_button)
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
        self.endpoint_more_actions.setFixedHeight(
            self.send_button.sizeHint().height()
        )
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
        self.endpoint_action_buttons = (
            self.send_button,
            self.save_request_button,
            self.add_to_suite_button,
            self.favorite_button,
            self.endpoint_more_actions,
        )
        self._set_endpoint_actions_enabled(False)
        self.endpoint_header_layout = endpoint_header
        endpoint_workspace_layout.addWidget(endpoint_header_widget)
        authentication_row = QHBoxLayout()
        authentication_row.setSpacing(8)
        authentication_label = QLabel("Authentication")
        authentication_label.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        authentication_row.addWidget(authentication_label)
        self.request_authentication = QComboBox()
        populate_auth_choices(self.request_authentication, self.app_settings.active.auth_profiles)
        self.request_authentication.setToolTip(
            "Configure sign-in under Settings > Environments > Authentication. "
            "No authentication omits known credential headers; Manual never renews."
        )
        self.request_authentication.currentIndexChanged.connect(
            self._authentication_mode_changed
        )
        authentication_row.addWidget(self.request_authentication, 1)
        authentication_widget = QWidget(endpoint_workspace)
        authentication_widget.setLayout(authentication_row)
        endpoint_workspace_layout.addWidget(authentication_widget)

        self.endpoint_content_tabs = QTabWidget()
        self.endpoint_content_tabs.setObjectName("endpointTabs")
        request_page = QWidget()
        request_page_layout = QVBoxLayout(request_page)
        request_page_layout.setContentsMargins(8, 8, 8, 8)
        request_page_layout.setSpacing(8)
        verification_row = QHBoxLayout()
        verification_row.addWidget(QLabel("Expected status for verification"))
        self.expected_status = expected_status_combo()
        self.expected_status.setToolTip(
            "Used by Send and verify from the endpoint More actions menu"
        )
        verification_row.addWidget(self.expected_status)
        verification_row.addStretch()
        request_page_layout.addLayout(verification_row)
        self.request_editor = RequestEditor(self)
        self._alias_request_editor()
        request_page_layout.addWidget(self.request_editor, 1)

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
        # The identity frame draws a 1px border, so its inner content starts one
        # pixel in. Match that here to keep the accordion sections aligned with
        # the endpoint method/route row above them.
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
        endpoint_workspace_layout.addWidget(self.request_response_accordion, 1)
        details_layout.addWidget(endpoint_workspace, 1)
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
        navigation_panel.setFixedWidth(180)
        self.navigation_panel = navigation_panel
        self._navigation_collapsed = False
        self._navigation_width_animation: QPropertyAnimation | None = None
        navigation_layout = QVBoxLayout(navigation_panel)
        navigation_layout.setContentsMargins(0, 0, 0, 8)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigationRail")
        self.navigation.setItemDelegate(IconTextItemDelegate(self.navigation))
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
            item = make_icon_text_item(name, icon_name)
            item.setToolTip(name)
            self.navigation.addItem(item)
        self.navigation.currentRowChanged.connect(self.workspace_tabs.setCurrentIndex)
        self.workspace_tabs.currentChanged.connect(self._workspace_tab_changed)
        self.navigation.setCurrentRow(0)
        navigation_layout.addWidget(self.navigation, 1)
        self.navigation_stats = QLabel()
        self.navigation_stats.setObjectName("navigationStats")
        self.navigation_stats.setTextFormat(Qt.TextFormat.RichText)
        self.navigation_stats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_navigation_stats()
        navigation_layout.addWidget(self.navigation_stats)
        footer = QWidget()
        footer.setObjectName("navigationFooter")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(4, 4, 4, 4)
        footer_layout.setSpacing(2)
        self.endpoint_info_button = QToolButton()
        self.endpoint_info_button.setObjectName("navigationInfoButton")
        self.endpoint_info_button.setIcon(icon("info-circle", theme.NAV_MUTED, 17))
        self.endpoint_info_button.setIconSize(QSize(17, 17))
        self.endpoint_info_button.setFixedSize(24, 24)
        self.endpoint_info_button.setToolTip("Show API endpoint counts")
        self.endpoint_info_button.setAccessibleName("Show API endpoint counts")
        self.endpoint_info_menu = QMenu(self.endpoint_info_button)
        self.endpoint_info_menu.aboutToShow.connect(self._populate_endpoint_counts_menu)
        self.endpoint_info_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.endpoint_info_button.setMenu(self.endpoint_info_menu)
        footer_layout.addWidget(
            self.endpoint_info_button, 0, Qt.AlignmentFlag.AlignHCenter
        )
        self.navigation_environment_panel = QWidget()
        self.navigation_environment_panel.setObjectName("navigationFooter")
        environment_layout = QVBoxLayout(self.navigation_environment_panel)
        environment_layout.setContentsMargins(8, 4, 8, 2)
        environment_layout.setSpacing(4)
        environment_caption_row = QHBoxLayout()
        environment_caption_row.setContentsMargins(0, 0, 0, 0)
        environment_caption = QLabel("Environment")
        environment_caption.setObjectName("navigationEnvironmentCaption")
        environment_caption_row.addWidget(environment_caption, 1)
        self.navigation_environment_settings = QToolButton()
        self.navigation_environment_settings.setObjectName("navigationFooterButton")
        self.navigation_environment_settings.setIcon(
            icon("settings", theme.NAV_MUTED, 14)
        )
        self.navigation_environment_settings.setIconSize(QSize(14, 14))
        self.navigation_environment_settings.setFixedSize(22, 22)
        self.navigation_environment_settings.setToolTip("Manage environments")
        self.navigation_environment_settings.setAccessibleName("Manage environments")
        self.navigation_environment_settings.clicked.connect(
            lambda: self.workspace_tabs.setCurrentWidget(self.environment_page)
        )
        environment_caption_row.addWidget(self.navigation_environment_settings)
        environment_layout.addLayout(environment_caption_row)
        self.navigation_environment = QComboBox()
        self.navigation_environment.setObjectName("navigationEnvironment")
        self.navigation_environment.setToolTip("Active environment")
        self.navigation_environment.setAccessibleName("Active environment")
        self.navigation_environment.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.navigation_environment.setMinimumContentsLength(8)
        self.navigation_environment.activated.connect(
            self._navigation_environment_activated
        )
        environment_layout.addWidget(self.navigation_environment)
        footer_layout.addWidget(self.navigation_environment_panel)
        self._refresh_navigation_environment()
        self.navigation_version_row = QWidget()
        self.navigation_version_row.setObjectName("navigationVersionRow")
        version_row_layout = QHBoxLayout(self.navigation_version_row)
        version_row_layout.setContentsMargins(0, 0, 0, 0)
        version_row_layout.setSpacing(4)
        version_row_layout.addStretch()
        self.navigation_version = QLabel("v2.1.0")
        self.navigation_version.setObjectName("navigationVersion")
        self.navigation_version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        version_row_layout.addWidget(self.navigation_version)
        self.navigation_collapse_button = QToolButton()
        self.navigation_collapse_button.setObjectName("navigationFooterButton")
        self.navigation_collapse_button.setIcon(icon("chevron-left", theme.NAV_MUTED, 16))
        self.navigation_collapse_button.setIconSize(QSize(16, 16))
        self.navigation_collapse_button.setFixedSize(24, 24)
        self.navigation_collapse_button.setToolTip("Collapse navigation rail")
        self.navigation_collapse_button.setAccessibleName("Collapse navigation rail")
        self.navigation_collapse_button.clicked.connect(
            self._toggle_navigation_collapsed
        )
        version_row_layout.addWidget(self.navigation_collapse_button)
        version_row_layout.addStretch()
        footer_layout.addWidget(self.navigation_version_row)
        navigation_layout.addWidget(footer)
        self.endpoint_info_button.hide()
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

    def _toggle_navigation_collapsed(self) -> None:
        if (
            self._navigation_width_animation is not None
            and self._navigation_width_animation.state()
            == QPropertyAnimation.State.Running
        ):
            return
        collapsed = not self._navigation_collapsed
        self._navigation_collapsed = collapsed
        current_width = self.navigation_panel.width()
        target_width = 68 if collapsed else 180
        self.navigation_panel.setMinimumWidth(68)
        self.navigation_panel.setMaximumWidth(current_width)
        animation = QPropertyAnimation(
            self.navigation_panel, b"maximumWidth", self
        )
        animation.setDuration(240)
        animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        animation.setStartValue(current_width)
        animation.setEndValue(target_width)
        self._navigation_width_animation = animation
        self.navigation_collapse_button.setEnabled(False)
        animation.finished.connect(
            lambda: self._finish_navigation_width_animation(
                animation, target_width
            )
        )
        animation.start()

        if self._navigation_collapsed:
            set_icon_text_items_collapsed(self.navigation, True)
            self.navigation_stats.hide()
            self.navigation_environment_panel.hide()
            self.endpoint_info_button.show()
            self.navigation_version.hide()
            self.navigation_collapse_button.setIcon(
                icon("chevron-right", theme.NAV_MUTED, 16)
            )
            self.navigation_collapse_button.setToolTip("Expand navigation rail")
            self.navigation_collapse_button.setAccessibleName(
                "Expand navigation rail"
            )
        else:
            set_icon_text_items_collapsed(self.navigation, False)
            self.navigation_stats.show()
            self.navigation_environment_panel.show()
            self.endpoint_info_button.hide()
            self.navigation_version.show()
            self.navigation_collapse_button.setIcon(
                icon("chevron-left", theme.NAV_MUTED, 16)
            )
            self.navigation_collapse_button.setToolTip("Collapse navigation rail")
            self.navigation_collapse_button.setAccessibleName(
                "Collapse navigation rail"
            )

    def _finish_navigation_width_animation(
        self, animation: QPropertyAnimation, width: int
    ) -> None:
        if animation is not self._navigation_width_animation:
            return
        self.navigation_panel.setFixedWidth(width)
        self.navigation_collapse_button.setEnabled(True)
        self._navigation_width_animation = None
        animation.deleteLater()

    def _populate_endpoint_counts_menu(self) -> None:
        self.endpoint_info_menu.clear()
        self.endpoint_info_menu.addSection("API endpoint counts")
        method_counts: dict[str, int] = {}
        total = 0
        for service in self.services:
            for endpoint in service.endpoints:
                total += 1
                method_counts[endpoint.method] = (
                    method_counts.get(endpoint.method, 0) + 1
                )
        total_label = QLabel(f"Total endpoints: {total:,}")
        total_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        total_label.setMinimumWidth(170)
        total_action = QWidgetAction(self.endpoint_info_menu)
        total_action.setDefaultWidget(total_label)
        self.endpoint_info_menu.addAction(total_action)
        rows = QWidget()
        rows_layout = QVBoxLayout(rows)
        rows_layout.setContentsMargins(10, 4, 10, 6)
        rows_layout.setSpacing(3)
        for method, count in sorted(method_counts.items()):
            row = QLabel(
                f"<span style='color:{theme.method_color(method)}'><b>{method}</b></span>"
                f" &nbsp; {count:,}"
            )
            row.setTextFormat(Qt.TextFormat.RichText)
            rows_layout.addWidget(row)
        action = QWidgetAction(self.endpoint_info_menu)
        action.setDefaultWidget(rows)
        self.endpoint_info_menu.addAction(action)

    def _refresh_navigation_stats(self) -> None:
        method_counts: dict[str, int] = {}
        total = 0
        for service in self.services:
            for endpoint in service.endpoints:
                total += 1
                method_counts[endpoint.method] = (
                    method_counts.get(endpoint.method, 0) + 1
                )
        self.navigation_stats.setText(
            "Total Endpoints<br>"
            f"<b>{total:,}</b><br><br>"
            + "<br>".join(
                f"<span style='color:{theme.method_color(method)}'><b>{method}</b></span>"
                f" &nbsp; {count:,}"
                for method, count in sorted(method_counts.items())
            )
        )

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
        return self.request_editor.values()

    def _populate_services(self) -> None:
        self.endpoint_tree.clear()
        self.endpoint_items.clear()
        self.endpoint_rows.clear()
        self._refresh_navigation_stats()
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
                item = QTreeWidgetItem(["", ""])
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
                item.setData(0, 259, "endpoint")
                item.setData(
                    0, 261, f"{endpoint.action}\n{endpoint.method} {endpoint.path}"
                )
                item.setToolTip(
                    0, f"{endpoint.action}\n{endpoint.method} {endpoint.path}"
                )
                controller_item.addChild(item)
                self.endpoint_items[endpoint.id] = item
                row = EndpointTreeRow(endpoint, self.endpoint_tree)
                row.clicked.connect(
                    lambda tree_item=item: self._select_endpoint_item(tree_item)
                )
                menu_container = QWidget(self.endpoint_tree)
                menu_container.setObjectName("endpointActionCell")
                menu_container.setAttribute(
                    Qt.WidgetAttribute.WA_TranslucentBackground
                )
                menu_container.setAutoFillBackground(False)
                menu_layout = QHBoxLayout(menu_container)
                menu_layout.setContentsMargins(0, 0, 4, 0)
                menu_layout.setAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
                menu_button = QToolButton(menu_container)
                menu_button.setObjectName("endpointActionMenu")
                menu_button.setIcon(icon("more-vertical", theme.TEXT_MUTED, 18))
                menu_button.setIconSize(QSize(18, 18))
                menu_button.setFixedSize(28, 30)
                menu_button.setToolTip("Endpoint actions")
                menu_button.setAccessibleName(f"Actions for {endpoint.action}")
                menu_layout.addWidget(menu_button)
                menu = QMenu(menu_button)
                menu.aboutToShow.connect(
                    lambda endpoint_id=endpoint.id, endpoint_menu=menu:
                    self._populate_endpoint_actions_menu(
                        endpoint_menu, endpoint_id
                    )
                )
                menu_button.setMenu(menu)
                menu_button.setPopupMode(
                    QToolButton.ToolButtonPopupMode.InstantPopup
                )
                self.endpoint_tree.setItemWidget(item, 0, row)
                self.endpoint_tree.setItemWidget(item, 1, menu_container)
                self.endpoint_rows[endpoint.id] = row
                self._set_endpoint_favorite_state(item, endpoint.id)

    def _endpoint_selected(self) -> None:
        selected = self.endpoint_tree.selectedItems()
        selected_endpoint_id = (
            str(selected[0].data(0, 256) or "") if selected else ""
        )
        for endpoint_id, row in self.endpoint_rows.items():
            row.set_selected(endpoint_id == selected_endpoint_id)
        endpoint_id = selected_endpoint_id
        if not endpoint_id:
            if self.current_endpoint is not None:
                current_id = self.current_endpoint.id
                self._request_auth_modes[current_id] = (
                    self.request_authentication.currentData() or "inherit"
                )
                self._request_drafts[current_id] = (
                    self._current_values(),
                    self.payload.toPlainText(),
                )
                self.current_endpoint = None
            self._set_endpoint_actions_enabled(False)
            self._refresh_connection_state()
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
        self._set_endpoint_actions_enabled(True)
        self.app_settings.active_service = endpoint.service
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
        self.endpoint_title.setToolTip(self.endpoint_title.text())
        self._refresh_favorite_button()

        self.request_editor.load(
            self.catalog,
            endpoint,
            draft_values if draft else None,
            draft[1] if draft else None,
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
        self.response_section.clear_outcome()
        self._refresh_connection_state()

    def _select_endpoint_item(self, item: QTreeWidgetItem) -> None:
        self.endpoint_tree.setCurrentItem(
            item,
            0,
            QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )
        if self.current_endpoint is None or self.current_endpoint.id != item.data(0, 256):
            self._endpoint_selected()

    def _set_endpoint_actions_enabled(self, enabled: bool) -> None:
        for button in getattr(self, "endpoint_action_buttons", ()):
            button.setEnabled(enabled)

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
        self._select_endpoint_item(endpoint_item)
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
        self._toggle_endpoint_favorite(self.current_endpoint.id)

    def _toggle_endpoint_favorite(self, endpoint_id: str) -> None:
        if endpoint_id not in self.endpoints_by_id:
            return
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
        description = str(item.data(0, 261) or item.text(0))
        item.setToolTip(
            0, ("Favorite endpoint\n" if favorite else "") + description
        )

    def _populate_endpoint_actions_menu(
        self, menu: QMenu, endpoint_id: str
    ) -> None:
        menu.clear()
        suite_action = menu.addAction("Add to Test Suite")
        suite_action.triggered.connect(
            lambda _checked=False, item_id=endpoint_id:
            self._add_endpoint_to_suite(item_id)
        )

        collection_menu = menu.addMenu("Add to Collection")
        collections = sorted(
            self.saved_request_store.collections.values(),
            key=lambda collection: collection.name.lower(),
        )
        if collections:
            for collection in collections:
                action = collection_menu.addAction(collection.name)
                action.triggered.connect(
                    lambda _checked=False, item_id=endpoint_id,
                    collection_id=collection.id:
                    self._add_endpoint_to_collection(item_id, collection_id)
                )
        else:
            collection_menu.addAction("No collections available").setEnabled(False)
        collection_menu.addSeparator()
        create_action = collection_menu.addAction("New Collection...")
        create_action.triggered.connect(
            lambda _checked=False, item_id=endpoint_id:
            self._create_collection_for_endpoint(item_id)
        )

        menu.addSeparator()
        favorite_action = menu.addAction(
            "Remove from Favorites"
            if endpoint_id in self.app_settings.favorites
            else "Add to Favorites"
        )
        favorite_action.triggered.connect(
            lambda _checked=False, item_id=endpoint_id:
            self._toggle_endpoint_favorite(item_id)
        )

    def _activate_endpoint(self, endpoint_id: str) -> bool:
        item = self.endpoint_items.get(endpoint_id)
        if item is None:
            return False
        self._select_endpoint_item(item)
        return self.current_endpoint is not None and self.current_endpoint.id == endpoint_id

    def _add_endpoint_to_suite(self, endpoint_id: str) -> None:
        if self._activate_endpoint(endpoint_id):
            self._add_to_suite()

    def _add_endpoint_to_collection(
        self, endpoint_id: str, collection_id: str
    ) -> None:
        if not self._activate_endpoint(endpoint_id):
            return
        collection = self.saved_request_store.collections.get(collection_id)
        endpoint = self.current_endpoint
        if collection is None or endpoint is None:
            QMessageBox.warning(
                self,
                "Collection unavailable",
                "The selected collection or endpoint is no longer available.",
            )
            return
        try:
            payload = (
                json.loads(self.payload.toPlainText())
                if self.payload.toPlainText().strip()
                else None
            )
        except json.JSONDecodeError as exc:
            QMessageBox.warning(
                self, "Invalid payload", f"Payload is not valid JSON: {exc}"
            )
            return
        request = SavedRequest(
            endpoint_id=endpoint.id,
            name=f"{endpoint.method} {endpoint.action}",
            values=self._current_values(),
            payload=payload,
            expected_status=self.expected_status.currentText(),
            authentication=self.request_authentication.currentData() or "inherit",
        )
        self.saved_request_store.upsert_request(request)
        self.saved_request_store.add_to_collection(collection_id, request.id)
        self.saved_requests_page.refresh()
        self.collections_page.refresh()
        self.statusBar().showMessage(
            f"Added {endpoint.action} to {collection.name}", 5000
        )

    def _create_collection_for_endpoint(self, endpoint_id: str) -> None:
        name, accepted = QInputDialog.getText(
            self, "New collection", "Name"
        )
        if not accepted or not name.strip():
            return
        collection = RequestCollection(name=name.strip())
        self.saved_request_store.upsert_collection(collection)
        self._add_endpoint_to_collection(endpoint_id, collection.id)

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

    # The request tabs live in RequestEditor; these keep the window's API.

    def _form_to_json(self, base: object = None) -> None:
        self.request_editor.form_to_json(base)

    def _open_payload_fields(self) -> None:
        self.request_editor.open_payload_fields()

    def _open_filter_builder(self) -> None:
        self.request_editor.open_filter_builder()

    def _open_sort_builder(self) -> None:
        self.request_editor.open_sort_builder()

    def _seed_path_parameters(self) -> None:
        self.request_editor.seed_path_parameters()

    def _seed_query_parameters(self) -> None:
        self.request_editor.seed_query_parameters()

    def _seed_parameters(self) -> None:
        self.request_editor.seed_parameters()

    def _seed_data(self) -> None:
        self.request_editor.seed_data()

    def _refresh_request_icons(self) -> None:
        self.request_editor.refresh_icons()

    def _alias_request_editor(self) -> None:
        editor = self.request_editor
        for name in (
            "request_tabs", "path_parameters", "query_parameters",
            "path_seed_button", "query_seed_button", "query_builder",
            "sort_builder", "filter_dialog", "sort_dialog", "form_builder",
            "payload", "payload_fields_button", "payload_seed_button",
            "payload_seed_layout", "payload_form", "payload_fields_buttons",
            "payload_fields_layout", "payload_fields_dialog", "path_tab_index",
            "query_tab_index", "form_tab_index", "json_tab_index",
        ):
            setattr(self, name, getattr(editor, name))

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
        self.response.status_label.setText("Running...")
        self.response_section.clear_outcome()
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
        self._flag_response_outcome(result)
        item = self.endpoint_items[endpoint.id]
        item.setForeground(0, QColor(theme.PASS if result.passed else theme.FAIL))

    def _flag_response_outcome(self, result: ApiResult) -> None:
        status_code = int(getattr(result, "status_code", 0) or 0)
        if status_code:
            reason = (getattr(result, "reason", "") or "").strip()
            text = f"{status_code} {reason}".strip()
            color = theme.status_color(status_code)
        else:
            text, color = "No response", theme.FAIL
        self.response_section.show_outcome(text, color)

    def _request_failed(self, message: str) -> None:
        self.response.show_error(message)
        self.response_section.show_outcome("Error", theme.FAIL)

    def _request_finished(self) -> None:
        self._set_endpoint_actions_enabled(self.current_endpoint is not None)
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
        self._refresh_connection_state()

    def _authentication_mode_changed(self) -> None:
        """Keeps the draft mode and the connection pill in step with the combo."""
        if self.current_endpoint is not None:
            self._request_auth_modes[self.current_endpoint.id] = (
                self.request_authentication.currentData() or "inherit"
            )
        self._refresh_connection_state()

    def _edit_environment(self) -> None:
        dialog = EnvironmentEditor(
            self.app_settings.active, [service.name for service in self.services], self
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        dialog.apply()
        self._sync_compatibility_controls()
        self._save_settings()

    def _refresh_connection_state(self) -> None:
        """Re-resolves the Connected/Disconnected pill for the active service.

        A probe already in flight is superseded rather than awaited, and a
        stale reply is dropped by token, so rapid endpoint switching cannot
        leave the pill showing another endpoint's service.
        """
        indicator = getattr(self, "connection_indicator", None)
        if indicator is None:
            return
        service = (
            self.current_endpoint.service
            if self.current_endpoint is not None
            else self.app_settings.active_service
        )
        if not service:
            indicator.set_state(
                False, "No service", "Select an endpoint to check its authentication."
            )
            return
        mode = self.request_authentication.currentData() or "inherit"
        context = AuthenticationContext.from_environment(self.app_settings.active)
        self._connection_token += 1
        token = self._connection_token
        indicator.set_state(
            indicator.is_connected(),
            "Checking…",
            f"Checking authentication for {service}…",
        )
        probe = ConnectionProbe(
            token, context, service, str(mode),
            dict(self.app_settings.active.custom_headers), self,
        )
        probe.resolved.connect(self._connection_resolved)
        probe.finished.connect(lambda probe=probe: self._connection_probe_finished(probe))
        self._connection_probes.add(probe)
        probe.start()

    def _connection_probe_finished(self, probe: ConnectionProbe) -> None:
        """Releases a finished probe so no reference outlives its C++ object."""
        self._connection_probes.discard(probe)
        probe.deleteLater()

    def _connection_resolved(self, token: int, state: object) -> None:
        if token != self._connection_token or not isinstance(state, ConnectionState):
            return
        detail = state.detail or state.summary
        if state.summary and state.detail:
            detail = f"{state.summary}\n{state.detail}"
        self.connection_indicator.set_state(state.connected, state.label, detail)

    def _refresh_navigation_environment(self) -> None:
        """Mirrors the saved environments and the active one into the rail."""
        combo = getattr(self, "navigation_environment", None)
        if combo is None:
            return
        names = list(self.app_settings.environments)
        active = self.app_settings.active_environment
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(names)
        combo.setCurrentIndex(names.index(active) if active in names else -1)
        combo.setEnabled(bool(names))
        combo.blockSignals(False)
        combo.setToolTip(f"Active environment: {active}" if active else "Active environment")

    def _navigation_environment_activated(self, index: int) -> None:
        name = self.navigation_environment.itemText(index)
        if name and name != self.app_settings.active_environment:
            self._activate_environment(name)

    def _refresh_environment_ui(self) -> None:
        self._refresh_navigation_environment()
        self.environment_page.refresh()
        self._sync_compatibility_controls()
        self._save_settings()

    def _activate_environment(self, name: str) -> None:
        if name not in self.app_settings.environments:
            return
        self.app_settings.active_environment = name
        self._refresh_environment_ui()

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

    def _current_theme_is_dark(self) -> bool:
        """The palette actually on screen, resolving a stored ``System``."""
        return theme.is_dark_mode(QApplication.instance(), self.app_settings.theme)

    def _toggle_theme(self) -> None:
        self._theme_changed("Light" if self._current_theme_is_dark() else "Dark")

    def _refresh_header_buttons(self) -> None:
        """Repaints the header icons for the active palette and unread count."""
        dark = self._current_theme_is_dark()
        self.theme_toggle_button.setIcon(
            icon("sun" if dark else "moon", theme.TEXT_MUTED, 18)
        )
        target = "light" if dark else "dark"
        self.theme_toggle_button.setToolTip(f"Turn the lights {'on' if dark else 'off'}")
        self.theme_toggle_button.setAccessibleName(f"Switch to {target} theme")
        unread = max(0, len(activity_log) - self._notifications_seen)
        if unread:
            self.notifications_button.setIcon(
                badged_icon("bell", theme.TEXT_MUTED, theme.PRIMARY, 18)
            )
            self.notifications_button.setToolTip(
                f"{unread} new authentication event{'s' if unread != 1 else ''}"
            )
        else:
            self.notifications_button.setIcon(icon("bell", theme.TEXT_MUTED, 18))
            self.notifications_button.setToolTip("Recent authentication activity")

    def _populate_notifications_menu(self) -> None:
        """Fills the bell menu from the shared authentication activity log."""
        menu = self.notifications_menu
        menu.clear()
        entries = activity_log.entries()[:12]
        if not entries:
            empty = menu.addAction("No activity yet")
            empty.setEnabled(False)
        else:
            for entry in entries:
                action = menu.addAction(f"{entry.when}   {entry.event} · {entry.profile}")
                action.setToolTip(entry.summary() or entry.method)
                action.setEnabled(False)
            menu.addSeparator()
            menu.addAction("Clear activity", self._clear_notifications)
        self._notifications_seen = len(activity_log)
        self._refresh_header_buttons()

    def _clear_notifications(self) -> None:
        activity_log.clear()
        self._notifications_seen = 0
        self._refresh_header_buttons()

    def _theme_changed(self, name: str) -> None:
        theme.apply_theme(QApplication.instance(), name)
        self.app_settings.theme = name
        self._refresh_header_buttons()
        self._refresh_navigation_stats()
        self.endpoint_info_button.setIcon(
            icon("info-circle", theme.NAV_MUTED, 17)
        )
        self.navigation_environment_settings.setIcon(
            icon("settings", theme.NAV_MUTED, 14)
        )
        self.navigation_collapse_button.setIcon(
            icon(
                "chevron-right" if self._navigation_collapsed else "chevron-left",
                theme.NAV_MUTED,
                16,
            )
        )
        self.suite_tab.refresh_theme()
        self.data_runner_tab.refresh_theme()
        self.load_testing_tab.refresh_theme()
        self.environment_page.refresh_theme()
        self.response.refresh_theme()
        self.request_editor.refresh_theme()
        self.connection_pill.refresh_theme()
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
        # A token method may still be consulting its session; let each probe
        # end so Qt never destroys a running QThread.
        for probe in list(self._connection_probes):
            if probe.isRunning():
                probe.wait(2000)
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
