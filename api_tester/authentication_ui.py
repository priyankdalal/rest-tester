"""Authentication settings UI for an environment.

Builds the "Authentication" tab hosted by
:class:`api_tester.environment_ui.EnvironmentEditor`: a list of managed
authentication profiles plus a per-service binding table. It edits **draft**
copies of ``EnvironmentProfile.auth_profiles``/``auth_bindings`` and only
writes them back through :meth:`AuthenticationSettings.apply`, which the
editor calls from its own ``apply()`` when the user clicks Save. Cancelling
the environment editor never touches the real environment.

This module only arranges UI around the contract implemented by
:mod:`api_tester.authentication` (``AuthProfile``, ``AuthBinding``,
``AuthenticationManager``, ``get_auth_manager``, ``AuthError``,
``InteractionRequired``). It does not re-implement any sign-in, token
storage, or PKCE/browser logic - that all lives in the authentication
manager, which can be substituted for a fake in tests via the ``manager``
constructor argument.
"""

from __future__ import annotations

import copy
from typing import Callable

from PyQt6.QtCore import QEvent, QObject, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .auth_strategies import METHOD_LABELS, all_strategies, get_strategy
from .authentication import (
    AuthBinding,
    AuthError,
    AuthProfile,
    InteractionRequired,
    get_auth_manager,
)
from . import theme
from .icons import icon
from .widgets import form_caption, attach_table_empty_state


#: Methods that identify a caller (bound as a service's "Identity"). API-key
#: style methods are deliberately excluded: a key is never a caller identity.
IDENTITY_METHODS = tuple(
    strategy.id for strategy in all_strategies() if not strategy.is_api_key_like
)
#: Methods that can only ever be bound as a service's "API key".
API_KEY_METHOD = "api_key"
API_KEY_METHODS = tuple(
    strategy.id for strategy in all_strategies() if strategy.is_api_key_like
)
ALL_METHODS = IDENTITY_METHODS + API_KEY_METHODS

#: Methods where the manager keeps a browser/silent sign-in session that
#: can be renewed or cleared, as opposed to a plain static secret.
SESSION_METHODS = tuple(
    strategy.id for strategy in all_strategies() if strategy.is_token_based
)
#: Methods with an interactive (browser or device-code) sign-in step.
INTERACTIVE_METHODS = tuple(
    strategy.id for strategy in all_strategies() if strategy.is_interactive
)
#: Methods where the manager stores manually entered secrets (optionally
#: encrypted on disk via `persistent`).
SECRET_METHODS = tuple(
    strategy.id for strategy in all_strategies() if strategy.secret_fields
)
#: Every method for which `AuthProfile.persistent` controls real,
#: manager-side encrypted storage - either of the static/client secret
#: (`SECRET_METHODS`) or of the browser/silent sign-in session
#: (`SESSION_METHODS`). There is no method where "Remember" is a no-op.
PERSISTENCE_METHODS = tuple(sorted(set(SECRET_METHODS) | set(SESSION_METHODS)))

#: Sentinel stored in an AuthBinding field meaning "no managed profile".
NONE_ID = ""

METHOD_HINTS = {
    "manual_bearer": (
        "Sends the token you paste below as 'Authorization: Bearer <token>'. "
        "There is nothing to sign in to or renew - update the token here "
        "when it expires."
    ),
    "api_key": (
        "Sends the key below in the header named here (default 'x-api-key') "
        "on every request bound to this profile."
    ),
    "entra_pkce": (
        "Opens a browser for an interactive Microsoft Entra ID sign-in "
        "(PKCE). Requires an approved app registration; Authority is the "
        "tenant/authority URL and Client ID/Scopes come from that "
        "registration."
    ),
    "b2c_pkce": (
        "Opens a browser for an interactive Azure AD B2C sign-in (PKCE). "
        "Requires an approved app registration; Authority is the B2C "
        "policy authority and Client ID/Scopes come from that "
        "registration."
    ),
    "client_credentials": (
        "Signs in silently as the application itself (no browser). "
        "Requires an approved app registration; Authority, Client ID, "
        "Scopes, and a client secret are all required."
    ),
}


def _method_label(method: str) -> str:
    return METHOD_LABELS.get(method, method)


class AuthProfileDialog(QDialog):
    """Adds or edits a single :class:`AuthProfile`.

    The secret field is always shown blank: it reflects nothing already
    stored (this dialog never reads the plaintext secret back out of the
    authentication manager), and leaving it blank on an edit means "keep the
    current secret/session unchanged".
    """

    def __init__(
        self,
        profile: AuthProfile | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._editing = profile is not None
        self._profile = copy.deepcopy(profile) if profile is not None else AuthProfile()
        self.secret_text: str | None = None
        self.secret_values: dict[str, str] | None = None
        self.result_profile: AuthProfile | None = None

        self.setWindowTitle("Edit profile" if self._editing else "Add profile")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.name_input = QLineEdit(self._profile.name)
        form.addRow("Name", self.name_input)

        self.method_input = QComboBox()
        for method in ALL_METHODS:
            self.method_input.addItem(_method_label(method), method)
        index = self.method_input.findData(self._profile.method)
        self.method_input.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("Method", self.method_input)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setProperty("fieldCaption", True)
        layout.addWidget(self.hint)

        self.authority_input = QLineEdit(self._profile.authority)
        form.addRow("Authority", self.authority_input)

        self.client_id_input = QLineEdit(self._profile.client_id)
        form.addRow("Client ID", self.client_id_input)

        self.scopes_input = QLineEdit(", ".join(self._profile.scopes))
        form.addRow("Scopes", self.scopes_input)

        self.header_name_input = QLineEdit(self._profile.header_name or "x-api-key")
        form.addRow("Header name", self.header_name_input)

        self.secret_input = QLineEdit()
        self.secret_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret_input.setPlaceholderText(
            "Leave blank to keep the current value" if self._editing else ""
        )
        self._secret_label = QLabel()
        form.addRow(self._secret_label, self.secret_input)

        self.persistent_input = QCheckBox("Remember between restarts")
        self.persistent_input.setChecked(self._profile.persistent)
        self.persistent_input.setToolTip(
            "Off by default: the session is kept in memory only for this run."
        )
        form.addRow("Persistence", self.persistent_input)

        self.timeout_input = QSpinBox()
        self.timeout_input.setRange(1, 3600)
        self.timeout_input.setSuffix(" seconds")
        self.timeout_input.setValue(self._profile.login_timeout)
        form.addRow("Sign-in timeout", self.timeout_input)

        self.redirect_uri_input = QLineEdit(self._profile.redirect_uri)
        form.addRow("Redirect URI", self.redirect_uri_input)

        # Rows for everything the selected method declares beyond the fixed
        # set above. Built from the strategy so a new method needs no UI code.
        self._dynamic_host = QWidget()
        self._dynamic_form = QFormLayout(self._dynamic_host)
        self._dynamic_form.setContentsMargins(0, 0, 0, 0)
        self._dynamic_inputs: dict[str, QWidget] = {}
        self._dynamic_secret_inputs: dict[str, QLineEdit] = {}
        layout.addWidget(self._dynamic_host)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setObjectName("secretWarning")
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.method_input.currentIndexChanged.connect(self._sync_fields)
        self._sync_fields()

    #: Configuration the dialog already renders with a dedicated row.
    _FIXED_CONFIG = frozenset(
        {"authority", "client_id", "scopes", "header_name", "login_timeout", "redirect_uri"}
    )

    def _sync_fields(self) -> None:
        method = self.method_input.currentData()
        if not method:
            return
        self.hint.setText(METHOD_HINTS.get(method, ""))
        strategy = get_strategy(method)
        declared = {field.name: field for field in strategy.config_fields}

        for name, widget in (
            ("authority", self.authority_input),
            ("client_id", self.client_id_input),
            ("scopes", self.scopes_input),
            ("header_name", self.header_name_input),
            ("login_timeout", self.timeout_input),
            ("redirect_uri", self.redirect_uri_input),
        ):
            field = declared.get(name)
            _set_row_visible(widget, field is not None)
            if field is not None:
                self._apply_field_text(widget, field)

        # A single secret keeps the dedicated row, and with it the "leave
        # blank to keep the current value" behaviour; several are rendered
        # dynamically below so each one is entered separately.
        secret_fields = strategy.secret_fields
        single_secret = len(secret_fields) == 1
        if single_secret:
            self._secret_label.setText(secret_fields[0].label)
        _set_row_visible(self.secret_input, single_secret)
        self._secret_label.setVisible(single_secret)

        self._rebuild_dynamic_rows(strategy, single_secret)

        # `persistent` maps to real, manager-side encrypted storage for
        # every supported method - either a static/client secret
        # (SECRET_METHODS) or a browser/silent sign-in session
        # (SESSION_METHODS) - so the control is never hidden, only its
        # wording changes to describe what is actually being remembered.
        _set_row_visible(self.persistent_input, method in PERSISTENCE_METHODS)
        # The label stays short because QCheckBox cannot word-wrap: a long
        # one sets a minimum width the user can never shrink the dialog
        # below. What is actually remembered goes in the tooltip.
        if method in SECRET_METHODS and method in SESSION_METHODS:
            remembered = "this credential and its sign-in session"
        elif method in SECRET_METHODS:
            remembered = "this secret"
        else:
            remembered = "this sign-in"
        self.persistent_input.setToolTip(
            f"Stores {remembered} encrypted on disk so it survives a restart. "
            "Off by default: it is otherwise kept in memory for this run only."
        )
        self.persistent_input.setAccessibleDescription(self.persistent_input.toolTip())

    @staticmethod
    def _apply_field_text(widget: QWidget, field) -> None:
        """Takes a fixed row's wording from the selected method's declaration.

        Without this the dedicated rows keep whatever text was hardcoded for
        the method they were originally written for - an API key would be
        labelled "Header name" even when placed in the query string, and
        every provider would show a Microsoft-specific placeholder.
        """
        label = _row_label(widget)
        if label is not None:
            label.setText(field.label)
        if isinstance(widget, QLineEdit):
            widget.setPlaceholderText(field.placeholder)
        widget.setToolTip(field.help or "")

    def _rebuild_dynamic_rows(self, strategy, single_secret: bool) -> None:
        """Renders the rows the fixed layout above does not already cover."""
        while self._dynamic_form.rowCount():
            self._dynamic_form.removeRow(0)
        self._dynamic_inputs.clear()
        self._dynamic_secret_inputs.clear()

        options = self._profile.options
        for field in strategy.config_fields:
            if field.name in self._FIXED_CONFIG:
                continue
            widget = _build_config_widget(field, options.get(field.name, field.default))
            if field.help:
                widget.setToolTip(field.help)
            self._dynamic_form.addRow(field.label, widget)
            self._dynamic_inputs[field.name] = widget

        if not single_secret:
            for secret in strategy.secret_fields:
                editor = QLineEdit()
                editor.setEchoMode(QLineEdit.EchoMode.Password)
                editor.setPlaceholderText(
                    secret.placeholder
                    or ("Leave blank to keep the current value" if self._editing else "")
                )
                if secret.help:
                    editor.setToolTip(secret.help)
                label = secret.label if secret.required else f"{secret.label} (optional)"
                self._dynamic_form.addRow(label, editor)
                self._dynamic_secret_inputs[secret.name] = editor
        self._dynamic_host.setVisible(self._dynamic_form.rowCount() > 0)

    def _collect_options(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for name, widget in self._dynamic_inputs.items():
            if isinstance(widget, QComboBox):
                values[name] = widget.currentData()
            elif isinstance(widget, QSpinBox):
                values[name] = widget.value()
            elif isinstance(widget, QCheckBox):
                values[name] = widget.isChecked()
            elif isinstance(widget, QPlainTextEdit):
                values[name] = widget.toPlainText().strip()
            else:
                values[name] = widget.text().strip()
        return values

    def _collect_secrets(self) -> dict[str, str]:
        return {
            name: editor.text()
            for name, editor in self._dynamic_secret_inputs.items()
            if editor.text()
        }

    def _accept(self) -> None:
        method = self.method_input.currentData()
        scopes = [
            scope.strip()
            for scope in self.scopes_input.text().split(",")
            if scope.strip()
        ]
        profile = AuthProfile(
            id=self._profile.id,
            name=self.name_input.text().strip() or "Authentication",
            method=method,
            authority=self.authority_input.text().strip(),
            client_id=self.client_id_input.text().strip(),
            scopes=scopes,
            header_name=self.header_name_input.text().strip() or "x-api-key",
            persistent=self.persistent_input.isChecked(),
            login_timeout=self.timeout_input.value(),
            redirect_uri=(
                self.redirect_uri_input.text().strip()
                if method in INTERACTIVE_METHODS
                else ""
            ),
            options=self._collect_options(),
        )
        try:
            profile.validate()
        except ValueError as exc:
            self.error_label.setText(str(exc))
            return
        self.result_profile = profile
        secrets = self._collect_secrets()
        # Which input holds the secret follows from the method's declared
        # fields, not from widget visibility -- a dialog that has not been
        # shown yet reports every row as hidden.
        fields = profile.strategy().secret_fields
        if len(fields) == 1 and self.secret_input.text():
            secrets = {fields[0].name: self.secret_input.text()}
        self.secret_values = secrets or None
        self.secret_text = self.secret_input.text() or None
        self.accept()


def _build_config_widget(field, value) -> QWidget:
    """Creates the editor a :class:`ConfigField` describes."""
    if field.kind == "choice":
        combo = QComboBox()
        for label, data in field.choices:
            combo.addItem(label, data)
        index = combo.findData(value if value else field.default)
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo
    if field.kind == "int":
        spin = QSpinBox()
        spin.setRange(1, 86400)
        try:
            spin.setValue(int(value or field.default or 1))
        except (TypeError, ValueError):
            spin.setValue(int(field.default or 1))
        return spin
    if field.kind == "bool":
        box = QCheckBox()
        box.setChecked(bool(value if value != "" else field.default))
        return box
    if field.kind == "multiline":
        editor = QPlainTextEdit()
        editor.setPlainText(str(value or field.default or ""))
        editor.setFixedHeight(72)
        editor.setPlaceholderText(field.placeholder)
        return editor
    line = QLineEdit(str(value if value not in (None, "") else (field.default or "")))
    line.setPlaceholderText(field.placeholder)
    return line


def _row_label(field_widget: QWidget) -> QWidget | None:
    """Finds the label paired with a form row.

    The rows live in a QFormLayout nested inside an outer QVBoxLayout, so
    ``field_widget.parentWidget().layout()`` is the *outer* layout and has no
    ``labelForField``. Looking it up that way silently found nothing and left
    orphaned labels on screen next to hidden inputs.
    """
    for form in field_widget.window().findChildren(QFormLayout):
        label = form.labelForField(field_widget)
        if label is not None:
            return label
    return None


def _set_row_visible(field_widget: QWidget, visible: bool) -> None:
    field_widget.setVisible(visible)
    label = _row_label(field_widget)
    if label is not None:
        label.setVisible(visible)


class _AuthWorker(QThread):
    """Runs one blocking authentication-manager call off the GUI thread.

    Only emits signals; it never touches any widget directly, so the slots
    connected to ``succeeded``/``failed`` are the only place UI state
    changes as a result of the operation.
    """

    succeeded = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(
        self, operation: Callable[[], None], parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._operation = operation

    def run(self) -> None:  # noqa: D102 - Qt override
        try:
            self._operation()
        except InteractionRequired as exc:
            self.failed.emit(str(exc) or "Interactive sign-in is required.")
        except AuthError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit()


class AuthenticationSettings(QWidget):
    """Editable authentication profiles and per-service bindings.

    All edits happen on draft copies of ``profile.auth_profiles``/
    ``auth_bindings``. Call :meth:`apply` to copy the drafts back onto the
    real environment (validating along the way) - this is only done when
    the hosting :class:`~api_tester.environment_ui.EnvironmentEditor` is
    saved. Call :meth:`discard` if the editor is cancelled instead, so any
    secret already sent to the manager for a profile that was only ever
    added to this draft gets cleaned up rather than left orphaned.
    """

    def __init__(
        self,
        profile,
        service_names: list[str],
        parent: QWidget | None = None,
        manager=None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager if manager is not None else get_auth_manager()
        self._environment_id = profile.id
        self._service_names = list(service_names)
        self._custom_header_names: set[str] = {
            name.lower() for name in profile.custom_headers
        }
        self._profiles: dict[str, AuthProfile] = {
            profile_id: copy.deepcopy(item)
            for profile_id, item in profile.auth_profiles.items()
        }
        self._bindings: dict[str, AuthBinding] = {
            service: copy.deepcopy(profile.auth_bindings.get(service)) or AuthBinding()
            for service in self._service_names
        }
        self._new_profile_ids: set[str] = set()
        self._workers: list[_AuthWorker] = []
        self._busy_count = 0

        layout = QVBoxLayout(self)
        layout.addWidget(
            form_caption(
                "Managed profiles cover browser (Microsoft Entra ID / Azure AD "
                "B2C) sign-in, service-to-service client credentials, a "
                "manual bearer token, and API keys. Bind a profile to each "
                "service below; unbound services keep using this "
                "environment's custom headers."
            )
        )

        profiles_row = QHBoxLayout()
        layout.addLayout(profiles_row, 1)
        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(lambda *_: self._sync_buttons())
        profiles_row.addWidget(self.profile_list, 1)
        profile_buttons = QVBoxLayout()
        self.add_button = QPushButton()
        self.add_button.setToolTip("Add authentication profile")
        self.add_button.setAccessibleName("Add authentication profile")
        self.add_button.clicked.connect(self._add_profile)
        profile_buttons.addWidget(self.add_button)
        self.edit_button = QPushButton()
        self.edit_button.setToolTip("Edit selected authentication profile")
        self.edit_button.setAccessibleName(
            "Edit selected authentication profile"
        )
        self.edit_button.clicked.connect(self._edit_profile)
        profile_buttons.addWidget(self.edit_button)
        self.remove_button = QPushButton()
        self.remove_button.setToolTip("Remove selected authentication profile")
        self.remove_button.setAccessibleName(
            "Remove selected authentication profile"
        )
        self.remove_button.setProperty("danger", True)
        self.remove_button.clicked.connect(self._remove_profile)
        profile_buttons.addWidget(self.remove_button)
        profile_buttons.addStretch()
        self.inspect_button = QPushButton()
        self.inspect_button.setToolTip(
            "Show the credential values being sent and recent authentication activity"
        )
        self.inspect_button.setAccessibleName("Open authentication inspector")
        self.inspect_button.clicked.connect(self._open_inspector)
        profile_buttons.addWidget(self.inspect_button)
        profiles_row.addLayout(profile_buttons)

        layout.addWidget(form_caption("Service bindings"))
        self.bindings_table = QTableWidget(
            len(self._service_names),
            6,
        )
        self.bindings_table.setHorizontalHeaderLabels(
            ["Service", "Identity", "API key", "Disabled", "Status", "Session"]
        )
        header = self.bindings_table.horizontalHeader()
        header.setSectionsMovable(False)
        header.setMinimumSectionSize(80)
        for column in range(self.bindings_table.columnCount()):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((145, 125, 120, 90, 225, 150)):
            self.bindings_table.setColumnWidth(column, width)
        self.bindings_table.verticalHeader().setVisible(False)
        self.bindings_table.verticalHeader().setDefaultSectionSize(36)
        self.bindings_empty_state = attach_table_empty_state(
            self.bindings_table,
            icon_name="api-explorer",
            title="No services to bind",
            guidance="Load an API catalog to choose how each service authenticates.",
        )
        layout.addWidget(self.bindings_table, 2)

        self.conflict_warning = QLabel()
        self.conflict_warning.setWordWrap(True)
        self.conflict_warning.setObjectName("secretWarning")
        layout.addWidget(self.conflict_warning)

        self._identity_combos: dict[str, QComboBox] = {}
        self._api_key_combos: dict[str, QComboBox] = {}
        self._disabled_boxes: dict[str, QCheckBox] = {}
        self._status_labels: dict[str, QLabel] = {}
        self._sign_in_buttons: dict[str, QPushButton] = {}
        self._renew_buttons: dict[str, QPushButton] = {}
        self._clear_buttons: dict[str, QPushButton] = {}
        self._build_binding_rows()
        self.refresh_theme()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_statuses)
        self._status_timer.start(4000)

        self._refresh_profile_list()
        self._refresh_statuses()
        self._update_conflict_warning()

    # -- profile list ------------------------------------------------
    def _selected_profile_id(self) -> str | None:
        item = self.profile_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _refresh_profile_list(self) -> None:
        selected = self._selected_profile_id()
        self.profile_list.clear()
        for profile_id, profile in self._profiles.items():
            item = QListWidgetItem(f"{profile.name} ({_method_label(profile.method)})")
            item.setData(Qt.ItemDataRole.UserRole, profile_id)
            self.profile_list.addItem(item)
            if profile_id == selected:
                self.profile_list.setCurrentItem(item)
        self._sync_buttons()
        self._refresh_binding_choices()

    def _sync_buttons(self) -> None:
        has_selection = self._selected_profile_id() is not None
        self.edit_button.setEnabled(has_selection)
        self.remove_button.setEnabled(has_selection)

    def _add_profile(self) -> None:
        dialog = AuthProfileDialog(None, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile = dialog.result_profile
        self._profiles[profile.id] = profile
        self._new_profile_ids.add(profile.id)
        self._store_secret_if_given(profile, dialog.secret_values)
        self._refresh_profile_list()
        self._update_conflict_warning()

    def _edit_profile(self) -> None:
        profile_id = self._selected_profile_id()
        if profile_id is None:
            return
        dialog = AuthProfileDialog(self._profiles[profile_id], self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        profile = dialog.result_profile
        profile.id = profile_id
        self._profiles[profile_id] = profile
        self._store_secret_if_given(profile, dialog.secret_values)
        self._refresh_profile_list()
        self._refresh_statuses()
        self._update_conflict_warning()

    def _store_secret_if_given(
        self, profile: AuthProfile, secret_values: dict[str, str] | None
    ) -> None:
        if not secret_values:
            return
        try:
            self._manager.set_secrets(self._environment_id, profile, secret_values)
        except (AuthError, ValueError) as exc:
            QMessageBox.warning(self, "Could not store secret", str(exc))

    def _remove_profile(self) -> None:
        profile_id = self._selected_profile_id()
        if profile_id is None:
            return
        profile = self._profiles[profile_id]
        used_by = [
            service
            for service, binding in self._bindings.items()
            if binding.identity == profile_id or binding.api_key == profile_id
        ]
        message = f"Delete '{profile.name}'? Any stored secret or session is cleared."
        if used_by:
            message += f" It is currently bound to: {', '.join(used_by)}."
        if QMessageBox.question(
            self, "Delete profile", message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self._manager.clear_session(self._environment_id, profile)
        except AuthError:
            pass
        del self._profiles[profile_id]
        self._new_profile_ids.discard(profile_id)
        for binding in self._bindings.values():
            if binding.identity == profile_id:
                binding.identity = NONE_ID
            if binding.api_key == profile_id:
                binding.api_key = NONE_ID
        self._refresh_profile_list()

    # -- service bindings ---------------------------------------------
    @property
    def manager(self):
        """The authentication manager these settings drive."""
        return self._manager

    @property
    def environment_id(self) -> str:
        return self._environment_id

    @property
    def profiles(self) -> dict[str, AuthProfile]:
        """The draft profiles, keyed by id."""
        return self._profiles

    def _open_inspector(self) -> None:
        from .auth_inspector_ui import AuthInspectorDialog

        AuthInspectorDialog(self, self).exec()

    def _build_binding_rows(self) -> None:
        for row, service in enumerate(self._service_names):
            self.bindings_table.setItem(row, 0, QTableWidgetItem(service))
            self.bindings_table.item(row, 0).setFlags(Qt.ItemFlag.ItemIsEnabled)

            identity_combo = QComboBox()
            identity_combo.currentIndexChanged.connect(
                lambda _, svc=service: self._on_identity_changed(svc)
            )
            self.bindings_table.setCellWidget(row, 1, identity_combo)
            self._identity_combos[service] = identity_combo

            api_key_combo = QComboBox()
            api_key_combo.currentIndexChanged.connect(
                lambda _, svc=service: self._on_api_key_changed(svc)
            )
            self.bindings_table.setCellWidget(row, 2, api_key_combo)
            self._api_key_combos[service] = api_key_combo

            disabled_box = QCheckBox()
            disabled_box.setChecked(self._bindings[service].disabled)
            disabled_box.toggled.connect(
                lambda checked, svc=service: self._on_disabled_changed(svc, checked)
            )
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.addStretch()
            holder_layout.addWidget(disabled_box)
            holder_layout.addStretch()
            self.bindings_table.setCellWidget(row, 3, holder)
            self._disabled_boxes[service] = disabled_box

            status_label = QLabel("-")
            self.bindings_table.setCellWidget(row, 4, status_label)
            self._status_labels[service] = status_label

            actions = QWidget()
            actions_layout = QHBoxLayout(actions)
            actions_layout.setContentsMargins(0, 0, 0, 0)
            sign_in = QPushButton()
            sign_in.setToolTip(f"Sign in to {service}")
            sign_in.setAccessibleName(f"Sign in to {service}")
            sign_in.clicked.connect(lambda _, svc=service: self._sign_in(svc))
            renew = QPushButton()
            renew.setToolTip(f"Renew {service} session now")
            renew.setAccessibleName(f"Renew {service} session now")
            renew.clicked.connect(lambda _, svc=service: self._renew(svc))
            clear = QPushButton()
            clear.setToolTip(f"Clear {service} session")
            clear.setAccessibleName(f"Clear {service} session")
            clear.clicked.connect(lambda _, svc=service: self._clear_session(svc))
            for button in (sign_in, renew, clear):
                button.setFixedSize(32, 28)
                button.setIconSize(QSize(16, 16))
                actions_layout.addWidget(button, 1)
            self.bindings_table.setCellWidget(row, 5, actions)
            self._sign_in_buttons[service] = sign_in
            self._renew_buttons[service] = renew
            self._clear_buttons[service] = clear

    def refresh_theme(self) -> None:
        self.add_button.setIcon(icon("add", theme.TEXT, 18))
        self.edit_button.setIcon(icon("edit", theme.TEXT, 18))
        self.remove_button.setIcon(icon("trash", theme.FAIL, 18))
        self.inspect_button.setIcon(icon("eye", theme.TEXT, 18))
        for button in self._sign_in_buttons.values():
            button.setIcon(icon("sign-in", theme.TEXT, 16))
        for button in self._renew_buttons.values():
            button.setIcon(icon("renew", theme.TEXT, 16))
        for button in self._clear_buttons.values():
            button.setIcon(icon("sign-out", theme.FAIL, 16))
        self.bindings_empty_state.refresh_theme()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange and hasattr(self, "add_button"):
            self.refresh_theme()

    def _refresh_binding_choices(self) -> None:
        for service in self._service_names:
            binding = self._bindings[service]
            identity_combo = self._identity_combos[service]
            identity_combo.blockSignals(True)
            identity_combo.clear()
            identity_combo.addItem(
                "No managed authentication (legacy headers only)", NONE_ID
            )
            for profile_id, prof in self._profiles.items():
                if prof.method != API_KEY_METHOD:
                    identity_combo.addItem(prof.name, profile_id)
            index = identity_combo.findData(binding.identity)
            identity_combo.setCurrentIndex(index if index >= 0 else 0)
            identity_combo.blockSignals(False)

            api_key_combo = self._api_key_combos[service]
            api_key_combo.blockSignals(True)
            api_key_combo.clear()
            api_key_combo.addItem("No API key", NONE_ID)
            for profile_id, prof in self._profiles.items():
                if prof.method == API_KEY_METHOD:
                    api_key_combo.addItem(prof.name, profile_id)
            index = api_key_combo.findData(binding.api_key)
            api_key_combo.setCurrentIndex(index if index >= 0 else 0)
            api_key_combo.blockSignals(False)

            self._sync_action_buttons(service)
        self._refresh_statuses()

    def _on_identity_changed(self, service: str) -> None:
        self._bindings[service].identity = (
            self._identity_combos[service].currentData() or NONE_ID
        )
        self._sync_action_buttons(service)
        self._refresh_statuses()
        self._update_conflict_warning()

    def _on_api_key_changed(self, service: str) -> None:
        self._bindings[service].api_key = (
            self._api_key_combos[service].currentData() or NONE_ID
        )
        self._refresh_statuses()
        self._update_conflict_warning()

    def _on_disabled_changed(self, service: str, checked: bool) -> None:
        self._bindings[service].disabled = checked
        self._sync_action_buttons(service)
        self._refresh_statuses()

    def _identity_profile(self, service: str) -> AuthProfile | None:
        return self._profiles.get(self._bindings[service].identity)

    def _sync_action_buttons(self, service: str) -> None:
        binding = self._bindings[service]
        profile = self._identity_profile(service)
        active = bool(profile) and not binding.disabled
        can_sign_in = active and profile.method in INTERACTIVE_METHODS
        can_renew = active and profile.method in SESSION_METHODS
        self._sign_in_buttons[service].setEnabled(can_sign_in and self._busy_count == 0)
        self._renew_buttons[service].setEnabled(can_renew and self._busy_count == 0)
        self._clear_buttons[service].setEnabled(bool(profile) and self._busy_count == 0)

    def _update_conflict_warning(self) -> None:
        conflicts = []
        for service, binding in self._bindings.items():
            managed_headers = set()
            identity = self._profiles.get(binding.identity)
            if identity is not None:
                managed_headers.add(
                    identity.header_name.lower()
                    if identity.method == API_KEY_METHOD
                    else "authorization"
                )
            key_profile = self._profiles.get(binding.api_key)
            if key_profile is not None:
                managed_headers.add(key_profile.header_name.lower())
            overlap = managed_headers & self._custom_header_names
            if overlap:
                conflicts.append(f"{service} ({', '.join(sorted(overlap))})")
        if conflicts:
            self.conflict_warning.setText(
                "Custom headers on this environment overlap with managed "
                "authentication headers and will be rejected at request time: "
                + "; ".join(conflicts)
                + ". Remove the conflicting custom header or unbind the profile."
            )
        else:
            self.conflict_warning.setText("")

    def set_custom_header_names(self, names) -> None:
        """Keeps the conflict check in sync with the Custom Headers tab."""
        self._custom_header_names = {name.lower() for name in names}
        self._update_conflict_warning()

    # -- status / session actions --------------------------------------
    def _refresh_statuses(self) -> None:
        for service in self._service_names:
            binding = self._bindings[service]
            label = self._status_labels[service]
            if binding.disabled:
                label.setText("Disabled")
                continue
            profile = self._identity_profile(service)
            if profile is None:
                label.setText("Not configured")
                continue
            try:
                label.setText(self._manager.status(self._environment_id, profile))
            except AuthError as exc:
                label.setText(f"Error: {exc}")

    def _run_worker(
        self,
        service: str,
        operation: Callable[[], None],
        busy_message: str,
    ) -> None:
        if self._busy_count > 0:
            return
        self._busy_count += 1
        self._set_all_actions_enabled(False)
        self._status_labels[service].setText(busy_message)
        worker = _AuthWorker(operation, self)

        def _done() -> None:
            self._busy_count = max(0, self._busy_count - 1)
            if worker in self._workers:
                self._workers.remove(worker)
            for svc in self._service_names:
                self._sync_action_buttons(svc)
            self._refresh_statuses()

        def _on_succeeded() -> None:
            _done()

        def _on_failed(message: str) -> None:
            _done()
            QMessageBox.warning(self, "Authentication", message)

        worker.succeeded.connect(_on_succeeded)
        worker.failed.connect(_on_failed)
        self._workers.append(worker)
        worker.start()

    def _set_all_actions_enabled(self, enabled: bool) -> None:
        if enabled:
            for service in self._service_names:
                self._sync_action_buttons(service)
        else:
            for button in (
                *self._sign_in_buttons.values(),
                *self._renew_buttons.values(),
                *self._clear_buttons.values(),
            ):
                button.setEnabled(False)

    def _sign_in(self, service: str) -> None:
        profile = self._identity_profile(service)
        if profile is None:
            return
        env_id, manager = self._environment_id, self._manager
        self._run_worker(
            service, lambda: manager.sign_in(env_id, profile), "Signing in..."
        )

    def _renew(self, service: str) -> None:
        profile = self._identity_profile(service)
        if profile is None:
            return
        env_id, manager = self._environment_id, self._manager
        self._run_worker(
            service, lambda: manager.renew(env_id, profile), "Renewing..."
        )

    def _clear_session(self, service: str) -> None:
        profile = self._identity_profile(service)
        if profile is None:
            return
        try:
            self._manager.clear_session(self._environment_id, profile)
        except AuthError as exc:
            QMessageBox.warning(self, "Authentication", str(exc))
        self._refresh_statuses()

    # -- lifecycle -------------------------------------------------------
    def is_busy(self) -> bool:
        """True while a Sign in/Renew worker thread is still running."""
        return self._busy_count > 0

    def stop(self) -> None:
        """Stops the live-status timer. Safe to call more than once."""
        self._status_timer.stop()

    def discard(self) -> None:
        """Called when the environment editor is cancelled.

        Metadata edits are only ever held in the draft, so there is nothing
        to undo there. But if a secret was set for a profile added during
        this edit, it was sent straight to the authentication manager (a
        store separate from settings.json); since that profile's metadata
        never makes it into the real environment, clear its session/secret
        so nothing is left behind for an id no environment references.
        """
        for profile_id in self._new_profile_ids:
            profile = self._profiles.get(profile_id)
            if profile is None:
                continue
            try:
                self._manager.clear_session(self._environment_id, profile)
            except AuthError:
                pass
        self.stop()

    def apply(self, target) -> None:
        """Copies the draft profiles/bindings onto ``target`` (the real
        environment profile). Raises ``ValueError`` on invalid input,
        which :class:`~api_tester.environment_ui.EnvironmentEditor` shows
        to the user instead of saving.
        """
        for profile in self._profiles.values():
            profile.validate()
        for service, binding in self._bindings.items():
            identity_profile = self._profiles.get(binding.identity)
            if binding.identity and (
                identity_profile is None or identity_profile.method == API_KEY_METHOD
            ):
                raise ValueError(f"{service}: identity cannot use an API-key profile.")
            key_profile = self._profiles.get(binding.api_key)
            if binding.api_key and (
                key_profile is None or key_profile.method != API_KEY_METHOD
            ):
                raise ValueError(
                    f"{service}: API key binding must use an API-key profile."
                )
        target.auth_profiles = {
            profile_id: copy.deepcopy(item)
            for profile_id, item in self._profiles.items()
        }
        target.auth_bindings = {
            service: copy.deepcopy(item) for service, item in self._bindings.items()
        }
