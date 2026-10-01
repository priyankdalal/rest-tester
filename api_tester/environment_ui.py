"""Environment editor and manager for the redesigned shell."""

from __future__ import annotations

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .authentication_ui import AuthenticationSettings
from .environment import (
    AppSettings,
    EnvironmentProfile,
    SECRET_HEADERS,
    has_secret_headers,
    validate_base_url,
)
from .icons import icon
from .widgets import KeyValueTable, attach_table_empty_state


CREDENTIAL_HINT = (
    "Add 'Authorization' and 'x-api-key' here to send them with every request "
    "in this environment. They are stored in plain text in data/settings.json "
    "and are redacted in the request log and generated code."
)


class EnvironmentEditor(QDialog):
    """Edits one profile, including the credentials sent as custom headers."""

    def __init__(
        self,
        profile: EnvironmentProfile,
        service_names: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle(f"Edit environment - {profile.name}")
        self.resize(820, 560)
        layout = QVBoxLayout(self)

        # A vertical nav rail (mirroring the main shell's left rail) instead of
        # a horizontal tab strip: the section names are long enough that a
        # single row crowds the dialog, and this keeps the shell's visual
        # language consistent.
        body = QHBoxLayout()
        body.setSpacing(0)

        self.section_rail = QListWidget()
        self.section_rail.setObjectName("navigationRail")
        self.section_rail.setProperty("dialogRail", True)
        self.section_rail.setFixedWidth(168)
        self.section_rail.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.section_rail.setTextElideMode(Qt.TextElideMode.ElideRight)

        self.sections = QStackedWidget()
        self.auth_settings = AuthenticationSettings(profile, service_names, self)
        self.custom_headers_page = self._build_headers(profile)
        self.custom_headers.changed.connect(
            lambda: self.auth_settings.set_custom_header_names(
                self.custom_headers.pairs()
            )
        )
        self._rail_icons: list[str] = []
        for title, icon_name, page in (
            ("Base URLs", "globe", self._build_urls(profile, service_names)),
            ("Variables", "code", self._build_variables(profile)),
            ("Custom Headers", "send", self.custom_headers_page),
            ("Transport", "settings", self._build_options(profile)),
            ("Authentication", "sign-in", self.auth_settings),
        ):
            item = QListWidgetItem(icon(icon_name, theme.NAV_TEXT, 18), title)
            item.setToolTip(title)
            self.section_rail.addItem(item)
            self.sections.addWidget(page)
            self._rail_icons.append(icon_name)
        self.section_rail.currentRowChanged.connect(self.sections.setCurrentIndex)
        self.section_rail.setCurrentRow(0)

        body.addWidget(self.section_rail)
        content = QWidget()
        content.setObjectName("environmentEditorContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 0)
        content_layout.addWidget(self.sections, 1)
        body.addWidget(content, 1)
        layout.addLayout(body, 1)

        self.warning = QLabel()
        self.warning.setObjectName("secretWarning")
        self.warning.setWordWrap(True)
        layout.addWidget(self.warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_warning()

    def _build_urls(
        self, profile: EnvironmentProfile, service_names: list[str]
    ) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        inner = QTabWidget()
        self.url_inputs: dict[str, QLineEdit] = {}
        for service in service_names:
            service_page = QWidget()
            form = QFormLayout(service_page)
            edit = QLineEdit(profile.base_urls.get(service, ""))
            edit.setPlaceholderText(f"https://{service.lower()}.example.com")
            form.addRow("Base URL", edit)
            self.url_inputs[service] = edit
            inner.addTab(service_page, service)
        outer.addWidget(inner)
        return page

    def _build_variables(self, profile: EnvironmentProfile) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        caption = QLabel(
            "Variables are substituted into suite cases and saved requests "
            "using {{name}}."
        )
        caption.setWordWrap(True)
        layout.addWidget(caption)
        self.variables = KeyValueTable(
            "Variable", "Value", "trialExperimentId", "470"
        )
        self.variables.set_pairs(profile.variables)
        layout.addWidget(self.variables, 1)
        return page

    def _build_headers(self, profile: EnvironmentProfile) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        caption = QLabel(CREDENTIAL_HINT)
        caption.setWordWrap(True)
        layout.addWidget(caption)
        self.custom_headers = KeyValueTable(
            "Header", "Value", "Authorization", "Bearer eyJ..."
        )
        self.custom_headers.set_pairs(profile.custom_headers)
        self.custom_headers.changed.connect(self._update_warning)
        layout.addWidget(self.custom_headers, 1)
        presets = QHBoxLayout()
        for name, value in (
            ("Authorization", "Bearer "),
            ("x-api-key", ""),
        ):
            button = QPushButton(f"Add {name}")
            button.clicked.connect(
                lambda _, header=name, seed=value: self._add_header(header, seed)
            )
            presets.addWidget(button)
        presets.addStretch()
        layout.addLayout(presets)
        return page

    def _add_header(self, name: str, value: str) -> None:
        if name.lower() in {key.lower() for key in self.custom_headers.pairs()}:
            return
        self.custom_headers.add_row(name, value)

    def _build_options(self, profile: EnvironmentProfile) -> QWidget:
        page = QWidget()
        options = QFormLayout(page)
        self.verify_ssl = QCheckBox("Verify SSL certificates")
        self.verify_ssl.setChecked(profile.verify_ssl)
        self.verify_ssl.setToolTip(
            "Clear this for local services using a self-signed certificate."
        )
        options.addRow("Transport", self.verify_ssl)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 3600)
        self.timeout.setSuffix(" seconds")
        self.timeout.setValue(profile.request_timeout)
        options.addRow("Request timeout", self.timeout)
        return page

    def _update_warning(self) -> None:
        if has_secret_headers(self.custom_headers.pairs()):
            self.warning.setText(
                "This environment stores credentials in plain text in "
                "data/settings.json. Do not commit that file."
            )
        else:
            self.warning.setText("")

    def apply(self) -> None:
        self.profile.base_urls = {
            name: edit.text().strip() for name, edit in self.url_inputs.items()
        }
        self.profile.verify_ssl = self.verify_ssl.isChecked()
        self.profile.request_timeout = self.timeout.value()
        self.profile.variables = self.variables.pairs()
        self.profile.custom_headers = self.custom_headers.pairs()
        # Authentication metadata (profiles/bindings) is only ever written to
        # the real environment here, on Save; the widget keeps its own draft
        # copies until this point and validates them as it applies.
        self.auth_settings.apply(self.profile)

    def _accept(self) -> None:
        if self.auth_settings.is_busy():
            QMessageBox.information(
                self,
                "Authentication in progress",
                "A sign-in or renewal is still running. Wait for it to "
                "finish before saving.",
            )
            return
        try:
            self.apply()
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid environment", str(exc))
            return
        self.auth_settings.stop()
        self.accept()

    def reject(self) -> None:  # noqa: D102 - Qt override
        if self.auth_settings.is_busy():
            QMessageBox.information(
                self,
                "Authentication in progress",
                "A sign-in or renewal is still running. Wait for it to "
                "finish before closing.",
            )
            return
        self.auth_settings.discard()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.auth_settings.is_busy():
            QMessageBox.information(
                self,
                "Authentication in progress",
                "A sign-in or renewal is still running. Wait for it to "
                "finish before closing.",
            )
            event.ignore()
            return
        event.accept()
        # Route through reject() so cancelling via the window's close
        # button discards the draft the same way the Cancel button does.
        if self.result() != QDialog.DialogCode.Accepted:
            self.reject()


class EnvironmentManagerPage(QWidget):
    """Creates, clones, deletes, and edits named environment profiles."""

    changed = pyqtSignal()
    activated = pyqtSignal(str)

    def __init__(
        self,
        settings: AppSettings,
        service_names: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.service_names = service_names
        layout = QVBoxLayout(self)
        heading = QLabel("Environments")
        heading.setProperty("pageTitle", True)
        layout.addWidget(heading)
        hint = QLabel(
            "Everything about a target lives here: one base URL per service, "
            "the access token and x-api-key as custom headers, variables, SSL "
            "verification, and the request timeout. Tick a row's 'Use' box to "
            "make that environment active."
        )
        hint.setWordWrap(True)
        hint.setProperty("pageDescription", True)
        layout.addWidget(hint)

        toolbar = QHBoxLayout()
        self.new_button = QPushButton("New environment")
        self.new_button.setObjectName("environmentNewButton")
        self.new_button.setProperty("accent", True)
        self.new_button.setToolTip("Create a new, empty environment")
        self.new_button.clicked.connect(self.create)
        toolbar.addWidget(self.new_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, 4)
        self.table.setObjectName("environmentTable")
        self.table.setHorizontalHeaderLabels(["Use", "Environment", "Summary", "Actions"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 52)
        self.table.setColumnWidth(1, 190)
        self.table.setColumnWidth(3, 112)
        self.table.itemDoubleClicked.connect(lambda _: self.edit_selected())
        self.table.itemSelectionChanged.connect(self._refresh_summary)
        self.empty_state = attach_table_empty_state(
            self.table,
            icon_name="globe",
            title="No environments yet",
            guidance="Create an environment to store a base URL, variables and headers.",
        )
        layout.addWidget(self.table, 1)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("environmentSummary")
        layout.addWidget(self.summary)

        # Per-row widgets are rebuilt on every refresh(); these keep the
        # current generation addressable for theming and tests.
        self._use_boxes: dict[str, QCheckBox] = {}
        self._clone_buttons: dict[str, QPushButton] = {}
        self._edit_buttons: dict[str, QPushButton] = {}
        self._delete_buttons: dict[str, QPushButton] = {}
        self.refresh()

    def set_service_names(self, service_names: list[str]) -> None:
        """Re-points the page at the services of a newly loaded catalog."""
        self.service_names = service_names
        self.refresh()

    def refresh(self) -> None:
        selected = self.selected_name()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self._use_boxes = {}
        self._clone_buttons = {}
        self._edit_buttons = {}
        self._delete_buttons = {}

        names = list(self.settings.environments)
        only_one = len(names) <= 1
        for row, name in enumerate(names):
            self.table.insertRow(row)
            is_active = name == self.settings.active_environment

            use_box = QCheckBox()
            use_box.setChecked(is_active)
            use_box.setToolTip(
                f"'{name}' is the active environment"
                if is_active
                else f"Use '{name}' as the active environment"
            )
            use_box.setAccessibleName(f"Use environment {name}")
            # Activation is exclusive: the active row's tick stays latched on
            # (unchecking is silently reverted in _on_use_toggled) so the app
            # can never end up with no active environment. The box is left
            # *enabled* so it renders as a normal ticked box rather than a
            # greyed-out one that reads as "unavailable".
            use_box.toggled.connect(
                lambda checked, env=name: self._on_use_toggled(env, checked)
            )
            self.table.setCellWidget(row, 0, self._center(use_box))
            self._use_boxes[name] = use_box

            name_item = QTableWidgetItem(name)
            if is_active:
                font = name_item.font()
                font.setBold(True)
                name_item.setFont(font)
            self.table.setItem(row, 1, name_item)

            self.table.setItem(row, 2, QTableWidgetItem(self._summary_text(name)))

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            actions_layout.setSpacing(2)
            clone_button = self._row_button(f"Clone '{name}'")
            clone_button.clicked.connect(lambda _, env=name: self.clone(env))
            edit_button = self._row_button(f"Edit '{name}'")
            edit_button.clicked.connect(lambda _, env=name: self.edit_selected(env))
            delete_button = self._row_button(f"Delete '{name}'", danger=True)
            delete_button.setEnabled(not only_one)
            if only_one:
                delete_button.setToolTip("At least one environment is required.")
            delete_button.clicked.connect(lambda _, env=name: self.delete_selected(env))
            for button in (clone_button, edit_button, delete_button):
                actions_layout.addWidget(button)
            actions_layout.addStretch()
            self.table.setCellWidget(row, 3, actions)
            self._clone_buttons[name] = clone_button
            self._edit_buttons[name] = edit_button
            self._delete_buttons[name] = delete_button

        self.table.blockSignals(False)

        target = selected or self.settings.active_environment
        row = self._row_for(target)
        if row is not None:
            self.table.selectRow(row)
        elif self.table.rowCount():
            self.table.selectRow(0)
        self.refresh_theme()
        self._refresh_summary()

    @staticmethod
    def _center(widget: QWidget) -> QWidget:
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.addStretch()
        holder_layout.addWidget(widget)
        holder_layout.addStretch()
        return holder

    @staticmethod
    def _row_button(tooltip: str, *, danger: bool = False) -> QPushButton:
        """A borderless icon-only row action matching the app-wide convention."""
        button = QPushButton()
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(32, 28)
        button.setIconSize(QSize(16, 16))
        if danger:
            button.setProperty("danger", True)
        return button

    def refresh_theme(self) -> None:
        """Re-tints every icon so they follow the active palette."""
        self.new_button.setIcon(icon("new", theme.TEXT_INVERSE, 16))
        for button in self._clone_buttons.values():
            button.setIcon(icon("duplicate", theme.TEXT, 16))
        for button in self._edit_buttons.values():
            # Same glyph as the header pill's environment key: both open the
            # environment editor, so they must not read as different actions.
            button.setIcon(icon("sliders-v", theme.TEXT, 16))
        for button in self._delete_buttons.values():
            button.setIcon(icon("trash", theme.FAIL, 16))
        self.empty_state.refresh_theme()

    def _row_for(self, name: str) -> int | None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item is not None and item.text() == name:
                return row
        return None

    def _on_use_toggled(self, name: str, checked: bool) -> None:
        if checked:
            self.activate_selected(name)
            return
        # Unticking the active row is meaningless (exactly one environment is
        # always active), so revert it instead of leaving the app unbound.
        if name == self.settings.active_environment:
            box = self._use_boxes.get(name)
            if box is not None:
                box.blockSignals(True)
                box.setChecked(True)
                box.blockSignals(False)

    def _summary_text(self, name: str) -> str:
        profile = self.settings.environments.get(name)
        if profile is None:
            return ""
        configured = sum(
            1 for url in profile.base_urls.values() if validate_base_url(url)[0]
        )
        total = len(profile.base_urls) or len(self.service_names)
        return (
            f"{configured}/{total} base URLs  |  {len(profile.variables)} variables  |  "
            f"{len(profile.custom_headers)} headers"
        )

    def _refresh_summary(self) -> None:
        name = self.selected_name()
        profile = self.settings.environments.get(name)
        if profile is None:
            self.summary.setText("")
            return
        configured = sum(
            1 for url in profile.base_urls.values() if validate_base_url(url)[0]
        )
        credentials = sorted(
            header
            for header in profile.custom_headers
            if header.lower() in SECRET_HEADERS
        )
        self.summary.setText(
            f"{configured} of {len(profile.base_urls) or len(self.service_names)} "
            f"base URLs configured  |  {len(profile.variables)} variables  |  "
            f"{len(profile.custom_headers)} headers"
            + (f"  |  credentials: {', '.join(credentials)}" if credentials else "")
            + f"  |  timeout {profile.request_timeout}s  |  "
            + ("SSL verified" if profile.verify_ssl else "SSL verification off")
        )

    def selected_name(self) -> str:
        row = self.table.currentRow() if hasattr(self, "table") else -1
        if row < 0:
            return ""
        item = self.table.item(row, 1)
        return item.text() if item is not None else ""

    def create(self) -> None:
        name, accepted = QInputDialog.getText(self, "New environment", "Name")
        name = name.strip()
        if not accepted or not name:
            return
        if name in self.settings.environments:
            QMessageBox.warning(self, "Duplicate environment", f"{name} already exists.")
            return
        self.settings.environments[name] = EnvironmentProfile(
            name=name,
            base_urls={service: "" for service in self.service_names},
        )
        self.refresh()
        self.changed.emit()

    def clone(self, name: str | None = None) -> None:
        source = name or self.selected_name()
        if not source:
            return
        new_name, accepted = QInputDialog.getText(
            self, "Clone environment", "Name", text=f"{source} Copy"
        )
        new_name = new_name.strip()
        if not accepted or not new_name:
            return
        if new_name in self.settings.environments:
            QMessageBox.warning(
                self, "Duplicate environment", f"{new_name} already exists."
            )
            return
        current = self.settings.environments[source]
        self.settings.environments[new_name] = current.clone(new_name)
        self.refresh()
        self.changed.emit()
        if current.auth_profiles:
            QMessageBox.information(
                self,
                "Authentication profiles cloned",
                f"'{new_name}' was cloned with {len(current.auth_profiles)} "
                "authentication profile(s) configured, but no signed-in "
                "session or stored secret carries over - those are kept "
                "separately per environment. Sign in again or re-enter "
                "secrets for the new environment if it needs them.",
            )

    def edit_selected(self, name: str | None = None) -> None:
        name = name or self.selected_name()
        if not name:
            return
        dialog = EnvironmentEditor(
            self.settings.environments[name], self.service_names, self
        )
        if dialog.exec() == dialog.DialogCode.Accepted:
            self.refresh()
            self.changed.emit()

    def activate_selected(self, name: str | None = None) -> None:
        name = name or self.selected_name()
        if name:
            self.activated.emit(name)
            self.refresh()

    def delete_selected(self, name: str | None = None) -> None:
        name = name or self.selected_name()
        if not name:
            return
        if len(self.settings.environments) == 1:
            QMessageBox.warning(
                self, "Cannot delete", "At least one environment is required."
            )
            return
        del self.settings.environments[name]
        if self.settings.active_environment == name:
            self.settings.active_environment = next(iter(self.settings.environments))
        self.refresh()
        self.changed.emit()
