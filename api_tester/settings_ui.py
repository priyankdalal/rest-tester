"""Application settings window with Catalog and AI connection sections."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import urlparse

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QStackedWidget, QVBoxLayout, QWidget, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView,
)

from . import theme
from .icons import icon, solid_icon
from .ai.config import (
    AiConnection, KEY_ENVIRONMENTS, PROVIDER_LABELS, PROVIDERS,
    delete_ai_key, load_ai_settings, save_ai_key, save_ai_settings,
)
from .ai.controller import connection_test_task
from .ai.ui import _keep_alive


class SettingsDialog(QDialog):
    ai_changed = pyqtSignal()

    def __init__(self, catalog_page: QWidget, settings_path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.resize(940, 680)
        self._path = settings_path
        self.settings = load_ai_settings(settings_path)
        self.settings.ensure_connections()
        self._selected_id = self.settings.active_connection_id
        self._keys: dict[str, str] = {}
        self._probe = None
        self._editor = None
        layout = QVBoxLayout(self)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.section_rail = QListWidget()
        self.section_rail.setObjectName("navigationRail")
        self.section_rail.setProperty("dialogRail", True)
        self.section_rail.setFixedWidth(168)
        self.section_rail.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.sections = QStackedWidget()
        from .ai.usage_dashboard import UsageDashboard

        self.usage_page = UsageDashboard()
        for title, page in (("Catalog", catalog_page), ("AI Settings", self._build_ai_page()),
                            ("AI Usage", self.usage_page)):
            item = QListWidgetItem(title)
            item.setToolTip(title)
            self.section_rail.addItem(item)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setWidget(page)
            self.sections.addWidget(scroll)
        self.section_rail.currentRowChanged.connect(self.sections.setCurrentIndex)
        self.section_rail.currentRowChanged.connect(
            lambda row: self.usage_page.refresh() if row == 2 else None
        )
        self.section_rail.setCurrentRow(0)
        body.addWidget(self.section_rail)
        body.addWidget(self.sections, 1)
        layout.addLayout(body, 1)
        footer = QHBoxLayout()
        footer.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        footer.addWidget(close)
        layout.addLayout(footer)
        self.refresh_theme()
        self._saved_state = self.settings.to_dict()

    def refresh_theme(self) -> None:
        for row, name in enumerate(("folder", "settings", "test-suites")):
            self.section_rail.item(row).setIcon(icon(name, theme.NAV_TEXT, 18))
        if hasattr(self, "table"):
            self._refresh_table()

    def _build_ai_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 12, 18, 12)
        title = QLabel("AI Settings")
        title.setProperty("pageTitle", True)
        layout.addWidget(title)
        hint = QLabel(
            "Manage saved AI connections like environments. Add or edit a connection, "
            "then tick its Use box to make it active. Only one connection is used by Ask AI."
        )
        hint.setProperty("pageDescription", True)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        toolbar = QHBoxLayout()
        add = QPushButton("Add provider")
        add.setObjectName("environmentNewButton")
        add.setProperty("accent", True)
        add.clicked.connect(self._new_connection)
        toolbar.addWidget(add)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("environmentTable")
        self.table.setHorizontalHeaderLabels(["Use", "Connection", "Provider", "Model / Endpoint", "Actions"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 64)
        self.table.setColumnWidth(1, 150)
        self.table.setColumnWidth(2, 130)
        self.table.setColumnWidth(4, 90)
        self.table.itemDoubleClicked.connect(
            lambda item: self._edit_connection(str(self.table.item(item.row(), 1).data(Qt.ItemDataRole.UserRole)))
        )
        layout.addWidget(self.table, 1)
        self.manager_status = QLabel()
        self.manager_status.setWordWrap(True)
        layout.addWidget(self.manager_status)
        self._editor_page = self._build_editor_page()
        self._editor_page.setParent(self)
        self._editor_page.hide()
        self._refresh_table()
        return page

    def _refresh_table(self) -> None:
        self.table.setRowCount(0)
        self._use_boxes = {}
        self._edit_buttons = {}
        self._delete_buttons = {}
        for row, connection in enumerate(self.settings.connections):
            self.table.insertRow(row)
            active = connection.id == self.settings.active_connection_id
            use = QCheckBox()
            use.setChecked(active)
            use.setAccessibleName(f"Use AI connection {connection.name}")
            use.toggled.connect(lambda checked, cid=connection.id: self._use_connection(cid, checked))
            container = QWidget()
            center = QHBoxLayout(container)
            center.setContentsMargins(0, 0, 0, 0)
            center.setAlignment(Qt.AlignmentFlag.AlignCenter)
            center.addWidget(use)
            self.table.setCellWidget(row, 0, container)
            self._use_boxes[connection.id] = use
            name = QTableWidgetItem(connection.name)
            name.setData(Qt.ItemDataRole.UserRole, connection.id)
            font = name.font()
            font.setBold(active)
            name.setFont(font)
            self.table.setItem(row, 1, name)
            self.table.setItem(row, 2, QTableWidgetItem(PROVIDER_LABELS[connection.provider]))
            summary = f"{connection.planner_model or 'Not configured'}\n{connection.endpoint or 'No endpoint'}"
            item = QTableWidgetItem(summary)
            item.setToolTip(summary)
            self.table.setItem(row, 3, item)
            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(2)
            for label, glyph, callback in (
                ("Edit", "edit", self._edit_connection),
                ("Delete", "trash", self._delete_saved_connection),
            ):
                button = QPushButton()
                button.setObjectName("environmentRowAction")
                button.setFixedSize(28, 28)
                button.setIcon(
                    solid_icon(glyph, theme.FAIL, 16)
                    if label == "Delete" else icon(glyph, theme.TEXT_MUTED, 16)
                )
                button.setToolTip(f"{label} '{connection.name}'")
                button.setAccessibleName(f"{label} AI connection {connection.name}")
                button.clicked.connect(lambda _checked=False, cid=connection.id, action=callback: action(cid))
                if label == "Delete":
                    button.setProperty("danger", True)
                    button.setEnabled(not active)
                    if active:
                        button.setToolTip("Use another connection before deleting the active one.")
                    self._delete_buttons[connection.id] = button
                else:
                    self._edit_buttons[connection.id] = button
                actions_layout.addWidget(button)
            self.table.setCellWidget(row, 4, actions)
            self.table.setRowHeight(row, 58)
        active = self.settings.for_connection(self.settings.active_connection_id)
        self.manager_status.setText(f"Active: {active.connection_name} / {PROVIDER_LABELS[active.provider]} / {active.planner_model}")

    def _select_for_action(self, connection_id: str) -> None:
        self._selected_id = connection_id
        self._load_profile()
        self._refresh_connections()

    def _edit_connection(self, connection_id: str) -> None:
        if self._probe is not None:
            self.manager_status.setText("Wait for the connection check to finish before opening another editor.")
            return
        snapshot = deepcopy(self.settings)
        self._select_for_action(connection_id)
        self._open_editor(snapshot)

    def _open_editor(self, snapshot) -> None:
        editor = QDialog(self)
        self._editor = editor
        editor.setWindowTitle(f"Edit AI connection - {self.selected_connection.name}")
        editor.resize(800, 740)
        layout = QVBoxLayout(editor)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._editor_page)
        self._editor_page.show()
        layout.addWidget(scroll)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(editor.reject)
        footer = QHBoxLayout()
        footer.addStretch()
        footer.addWidget(cancel)
        layout.addLayout(footer)
        self.status.clear()
        result = editor.exec()
        if result != QDialog.DialogCode.Accepted:
            if self._probe is not None:
                self._probe.cancel()
            self.settings = snapshot
            self._keys.clear()
        self._selected_id = self.settings.active_connection_id
        self._load_profile()
        self._refresh_connections()
        self.timeout_input.setValue(self.settings.timeout_seconds)
        self.repairs_input.setValue(self.settings.max_repairs)
        self._editor_page = scroll.takeWidget()
        self._editor_page.setParent(self)
        self._editor_page.hide()
        self._editor = None
        self._refresh_table()
        editor.deleteLater()

    def _use_connection(self, connection_id: str, checked: bool) -> None:
        if not checked:
            self._refresh_table()
            return
        self._select_for_action(connection_id)
        if not self.save(activate=True):
            message = self.status.text()
            self._refresh_table()
            self.manager_status.setText(message)

    def _delete_saved_connection(self, connection_id: str) -> None:
        snapshot = deepcopy(self.settings)
        self._select_for_action(connection_id)
        self._delete_connection()
        if len(self.settings.connections) == len(snapshot.connections):
            return
        if not self.save():
            self.settings = snapshot
            message = self.status.text()
            self._refresh_table()
            self.manager_status.setText(message)

    def _build_editor_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 12, 18, 12)
        title = QLabel("AI connection")
        title.setProperty("pageTitle", True)
        layout.addWidget(title)
        description = QLabel(
            "Configure named AI connections like environments. You can keep multiple connections "
            "for any provider; only the active connection is used by Ask AI. "
            "Selecting a connection for editing does not activate it."
        )
        description.setWordWrap(True)
        description.setProperty("pageDescription", True)
        layout.addWidget(description)
        self.active_label = QLabel()
        self.active_label.setWordWrap(True)
        layout.addWidget(self.active_label)
        form = QFormLayout()
        self.name_input = QLineEdit()
        form.addRow("Connection name", self.name_input)
        self.provider_input = QComboBox()
        for provider in PROVIDERS:
            self.provider_input.addItem(PROVIDER_LABELS[provider], provider)
        self.provider_input.setCurrentIndex(PROVIDERS.index(self.selected_connection.provider))
        form.addRow("Provider", self.provider_input)
        self.endpoint_input = QLineEdit()
        form.addRow("Endpoint", self.endpoint_input)
        self.model_input = QComboBox()
        self.model_input.setEditable(True)
        self.model_label = QLabel("Model")
        form.addRow(self.model_label, self.model_input)
        self.version_input = QLineEdit()
        self.version_label = QLabel("Azure API version")
        form.addRow(self.version_label, self.version_input)
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_label = QLabel("API key")
        form.addRow(self.key_label, self.key_input)
        self.key_hint = QLabel()
        self.key_hint.setWordWrap(True)
        form.addRow(self.key_hint)
        self.key_environment_input = QLineEdit()
        self.key_environment_label = QLabel("Key environment variable")
        form.addRow(self.key_environment_label, self.key_environment_input)
        self.think_input = QCheckBox("Enable reasoning (slower)")
        form.addRow(self.think_input)
        self.context_input = QSpinBox()
        self.context_input.setRange(2048, 262144)
        self.context_input.setSingleStep(2048)
        self.context_label = QLabel("Ollama context window")
        form.addRow(self.context_label, self.context_input)
        self.timeout_input = QSpinBox()
        self.timeout_input.setRange(10, 1800)
        self.timeout_input.setSuffix(" s")
        self.timeout_input.setValue(self.settings.timeout_seconds)
        form.addRow("Request timeout", self.timeout_input)
        self.repairs_input = QSpinBox()
        self.repairs_input.setRange(0, 50)
        self.repairs_input.setValue(self.settings.max_repairs)
        self.repairs_input.setToolTip(
            "Additional repair/refinement model calls after the initial plan. "
            "Each call can consume tokens or incur provider charges."
        )
        form.addRow("Maximum repair rounds", self.repairs_input)
        layout.addLayout(form)
        self.hosted_input = QCheckBox("Allow prompts and selected API catalog metadata to be sent to hosted AI")
        self.hosted_input.setChecked(self.settings.allow_hosted)
        layout.addWidget(self.hosted_input)
        privacy = QLabel(
            "Ollama stays local. Azure OpenAI, OpenAI and Claude send your prompt and relevant "
            "endpoint metadata to the selected provider and may incur charges. Do not include "
            "confidential data. API keys are stored using OS encryption, never in settings JSON; "
            "known credentials are masked before planning."
        )
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        self.provider_hint = QLabel()
        self.provider_hint.setWordWrap(True)
        layout.addWidget(self.provider_hint)
        actions = QHBoxLayout()
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("accent", True)
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button)
        self.test_button = QPushButton("Test connection")
        self.test_button.setToolTip(
            "Hosted providers send one billed probe (up to 128 output tokens for Sarvam, "
            "4,096 for other providers, including reasoning). Ollama lists available models."
        )
        self.test_button.clicked.connect(self._test_connection)
        actions.addWidget(self.test_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()
        self.provider_input.currentIndexChanged.connect(self._provider_changed)
        self._load_profile()
        self._refresh_connections()
        self.ai_page = page
        return page

    @property
    def selected_connection(self) -> AiConnection:
        return next(item for item in self.settings.connections if item.id == self._selected_id)

    def _stash_profile(self) -> None:
        connection = self.selected_connection
        connection.name = self.name_input.text().strip()
        connection.endpoint = self.endpoint_input.text().strip().rstrip("/")
        connection.planner_model = self.model_input.currentText().strip()
        connection.api_version = self.version_input.text().strip()
        connection.think = self.think_input.isChecked()
        connection.context_window = self.context_input.value()
        connection.allow_hosted = self.hosted_input.isChecked()
        connection.key_environment = self.key_environment_input.text().strip()
        self._keys[connection.credential_id] = self.key_input.text().strip()
        self.settings.timeout_seconds = self.timeout_input.value()
        self.settings.max_repairs = self.repairs_input.value()

    def _provider_changed(self) -> None:
        self._stash_profile()
        connection = self.selected_connection
        provider = str(self.provider_input.currentData())
        fresh = AiConnection.create(connection.name, provider)
        fresh.id = connection.id
        self._keys.pop(connection.credential_id, None)
        self.settings.connections[self.settings.connections.index(connection)] = fresh
        self._load_profile()
        self._refresh_connections()
        self.status.clear()

    def _load_profile(self) -> None:
        profile = self.selected_connection
        provider = profile.provider
        self.name_input.setText(profile.name)
        self.provider_input.blockSignals(True)
        self.provider_input.setCurrentIndex(PROVIDERS.index(provider))
        self.provider_input.blockSignals(False)
        self.endpoint_input.setText(profile.endpoint)
        self.model_input.clear()
        self.model_input.addItem(profile.planner_model)
        self.model_input.setCurrentText(profile.planner_model)
        self.version_input.setText(profile.api_version)
        self.key_input.setText(self._keys.get(profile.credential_id, ""))
        self.key_input.setPlaceholderText("Leave blank to keep this connection's saved key")
        self.key_environment_input.setText(profile.key_environment)
        self.key_environment_input.setPlaceholderText(f"Optional, e.g. {KEY_ENVIRONMENTS.get(provider, '')}")
        self.think_input.setChecked(profile.think)
        self.context_input.setValue(profile.context_window)
        self.hosted_input.setChecked(profile.allow_hosted)
        hosted = provider != "ollama"
        for widget in (self.key_input, self.key_label, self.key_hint, self.hosted_input,
                       self.key_environment_input, self.key_environment_label):
            widget.setVisible(hosted)
        for widget in (self.version_label, self.version_input):
            widget.setVisible(provider == "azure")
        for widget in (self.think_input, self.context_label, self.context_input):
            widget.setVisible(not hosted)
        self.model_label.setText("Deployment name" if provider == "azure" else "Model ID")
        self.key_hint.setText(
            "Keys are stored separately for each connection. Optionally specify an environment "
            "variable; its value takes precedence over this connection's saved key."
        )
        hints = {
            "ollama": "Use the Ollama server URL and an installed model tag, e.g. qwen3:14b. Test connection discovers installed models.",
            "azure": "Use your Azure OpenAI resource URL (https://<resource>.openai.azure.com), deployment name, and API version. Choose a chat model supporting JSON mode.",
            "openai": "Use https://api.openai.com/v1 and a chat model supporting JSON mode, such as gpt-4.1-mini.",
            "claude": "Use https://api.anthropic.com and a Claude model ID available to your account. Plans use structured tool output.",
            "sarvam": "Use https://api.sarvam.ai/v1 with sarvam-105b (or sarvam-105b-conversations) and a Sarvam API subscription key. Plans use JSON mode.",
        }
        self.provider_hint.setText(hints[provider])

    def _refresh_connections(self) -> None:
        active = self.settings.for_connection(self.settings.active_connection_id)
        self.active_label.setText(
            f"Active: {active.connection_name} / {PROVIDER_LABELS[active.provider]} / {active.planner_model}"
        )

    def _unique_name(self, base: str) -> str:
        names = {item.name.casefold() for item in self.settings.connections}
        candidate, suffix = base, 2
        while candidate.casefold() in names:
            candidate = f"{base} {suffix}"
            suffix += 1
        return candidate

    def _new_connection(self) -> None:
        if self._probe is not None:
            self.manager_status.setText("Wait for the connection check to finish before adding another connection.")
            return
        snapshot = deepcopy(self.settings)
        connection = AiConnection.create(self._unique_name("New connection"))
        self.settings.connections.append(connection)
        self._selected_id = connection.id
        self._load_profile()
        self._refresh_connections()
        self._open_editor(snapshot)

    def _delete_connection(self) -> None:
        connection = self.selected_connection
        if connection.id == self.settings.active_connection_id:
            self.status.setText("Activate another connection before deleting this one.")
            return
        if QMessageBox.question(self, "Delete AI connection", f"Delete '{connection.name}'?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        self._keys.pop(connection.credential_id, None)
        self.settings.connections.remove(connection)
        self._selected_id = self.settings.active_connection_id
        self._load_profile()
        self._refresh_connections()
        self.status.setText("Connection removed from the draft. Save connections to apply the deletion.")

    def _validate_connection(self, connection: AiConnection) -> bool:
        problem = ""
        try:
            parsed = urlparse(connection.endpoint)
            valid_endpoint = bool(parsed.hostname) and parsed.scheme in {"http", "https"}
        except ValueError:
            valid_endpoint = False
        if not valid_endpoint:
            problem = "Enter a valid HTTP(S) endpoint."
        elif connection.provider != "ollama" and (
            parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            problem = "Hosted endpoints require an HTTPS base URL without credentials, query, or fragment."
        elif not connection.planner_model:
            problem = "Enter a model ID or deployment name."
        elif connection.provider == "azure" and not connection.api_version:
            problem = "Enter an Azure API version."
        elif connection.provider != "ollama" and not connection.allow_hosted:
            problem = "Allow hosted AI for this connection before using it."
        if problem:
            self.status.setText(f"{connection.name}: {problem}")
            return False
        return True

    def save(self, _checked: bool = False, *, activate: bool = False) -> bool:
        self._stash_profile()
        active_id = self._selected_id if activate else self.settings.active_connection_id
        names: set[str] = set()
        for connection in self.settings.connections:
            problem = ""
            if not connection.name or connection.name.casefold() in names:
                problem = "Connection names must be non-empty and unique."
            elif connection.key_environment and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", connection.key_environment):
                problem = "Enter a valid environment variable name."
            if problem:
                self.status.setText(f"{connection.name or 'Unnamed connection'}: {problem}")
                return False
            names.add(connection.name.casefold())
            # Inactive connections may be saved as drafts until ready to use.
            if connection.id == active_id:
                if not self._validate_connection(connection):
                    return False
        settings = self.settings.for_connection(active_id)
        try:
            for connection in settings.connections:
                key = self._keys.get(connection.credential_id, "")
                if key and connection.provider != "ollama":
                    save_ai_key(connection.provider, key, connection.credential_id)
            save_ai_settings(self._path, settings)
        except Exception as exc:
            # Encryption backends have platform-specific exception types.
            self.status.setText(f"Could not save AI settings securely ({type(exc).__name__}).")
            return False
        self.settings = settings
        self._keys.clear()
        self.key_input.clear()
        previous_keys = {item["credential_id"] for item in self._saved_state["connections"]}
        retained_keys = {item.credential_id for item in settings.connections}
        cleanup_failed = False
        for credential_id in previous_keys - retained_keys:
            try:
                delete_ai_key(credential_id)
            except OSError:
                cleanup_failed = True
        self._saved_state = self.settings.to_dict()
        self._refresh_connections()
        self.status.setText(
            f"Saved. Only '{settings.connection_name}' is active for Ask AI."
            + (" An old encrypted key could not be removed from disk." if cleanup_failed else "")
        )
        self.ai_changed.emit()
        self._refresh_table()
        if self._editor is not None:
            self._editor.accept()
        return True

    def _test_connection(self) -> None:
        if self._probe is not None:
            return
        self._stash_profile()
        if not self._validate_connection(self.selected_connection):
            return
        self.status.setText("Connecting...")
        self.ai_page.setEnabled(False)
        key = self._keys.get(self.selected_connection.credential_id, "")
        probe = _keep_alive(connection_test_task(
            self.settings.for_connection(self._selected_id), api_key=key
        ))
        probe.succeeded.connect(self._connection_result)
        probe.failed.connect(self.status.setText)
        probe.done.connect(self._connection_done)
        self._probe = probe
        probe.start()

    def _connection_result(self, report) -> None:
        if self._editor is None:
            return
        self.status.setText(report.message)
        if report.models:
            current = self.model_input.currentText()
            self.model_input.clear()
            self.model_input.addItems(list(report.models))
            self.model_input.setCurrentText(current)

    def _connection_done(self) -> None:
        self._probe = None
        self.ai_page.setEnabled(True)
        self._refresh_connections()

    def closeEvent(self, event) -> None:
        if self.settings.to_dict() != self._saved_state or any(self._keys.values()):
            choice = QMessageBox.question(
                self, "Unsaved AI connections", "Save changes to your AI connections?",
                QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if choice == QMessageBox.StandardButton.Cancel or (
                choice == QMessageBox.StandardButton.Save and not self.save()
            ):
                event.ignore()
                return
            if choice == QMessageBox.StandardButton.Discard:
                self.settings = load_ai_settings(self._path)
                self.settings.ensure_connections()
                self._keys.clear()
                self._selected_id = self.settings.active_connection_id
                self._load_profile()
                self._refresh_connections()
                self._saved_state = self.settings.to_dict()
        super().closeEvent(event)
