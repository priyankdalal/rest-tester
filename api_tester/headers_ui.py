"""Request overrides and a credential-safe effective-header preview."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QHeaderView, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .catalog import Endpoint
from .headers import merge_headers, request_headers, validate_headers
from .request_auth import SECRET_HEADER_NAMES
from .suite import apply_variables
from .widgets import attach_table_empty_state


class HeadersEditor(QWidget):
    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.endpoint: Endpoint | None = None
        self._loading = False
        self._environment: dict[str, str] = {}
        self._variables: dict[str, str] = {}
        self._managed: frozenset[str] = frozenset()
        self._omitted: frozenset[str] = frozenset()
        self._secrets = SECRET_HEADER_NAMES
        self._context_error = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        hint = QLabel(
            "Environment defaults < request overrides (names are case-insensitive; last row wins). "
            "Unticked rows do not override defaults. Managed authentication conflicts are blocked."
        )
        hint.setWordWrap(True)
        self.hint = hint
        layout.addWidget(hint)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Use", "Header", "Value"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 190)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.itemChanged.connect(self._changed)
        self.empty_state = attach_table_empty_state(
            self.table, icon_name="fields", title="No request headers",
            guidance="Add a header to override this request only. Environment defaults still apply.",
        )
        layout.addWidget(self.table, 2)
        actions = QHBoxLayout()
        self.add_button = QPushButton("Add header")
        self.add_button.clicked.connect(lambda: self.add_row(focus=True))
        self.delete_button = QPushButton("Remove selected")
        self.delete_button.clicked.connect(self.remove_selected)
        actions.addWidget(self.add_button)
        actions.addWidget(self.delete_button)
        actions.addStretch()
        layout.addLayout(actions)
        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setProperty("fieldCaption", True)
        layout.addWidget(self.warning)
        preview_title = QLabel(
            "Effective configured headers (credentials masked; no sign-in performed). "
            "The HTTP client adds body/transport headers when sending."
        )
        preview_title.setWordWrap(True)
        self.preview_title = preview_title
        layout.addWidget(preview_title)
        self.effective = QTableWidget(0, 3)
        self.effective.setHorizontalHeaderLabels(["Header", "Value", "Source"])
        self.effective.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.effective.verticalHeader().setVisible(False)
        self.effective.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.effective.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.effective.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.effective, 1)
        safety = QLabel(
            "Known credential values are masked. Saved Requests omit known credential headers; "
            "suite files can store header values in plain text. Use {{variables}} for secrets."
        )
        safety.setWordWrap(True)
        safety.setProperty("fieldCaption", True)
        layout.addWidget(safety)

    def load(self, endpoint: Endpoint | None, values: dict[str, str]) -> None:
        self._loading = True
        self.endpoint = endpoint
        self.table.setRowCount(0)
        remaining = {key[7:]: value for key, value in values.items() if key.startswith("header:")}
        for parameter in endpoint.parameters if endpoint else ():
            if parameter.source != "header":
                continue
            name = next(
                (key for key in remaining if key.lower() == parameter.name.lower()),
                parameter.name,
            )
            value = remaining.pop(name, "")
            self.add_row(
                parameter.name, value,
                values.get(f"enabled:header:{name}", "true").lower() != "false",
                declared=True, required=parameter.required,
            )
        for name, value in remaining.items():
            self.add_row(name, value, values.get(f"enabled:header:{name}", "true").lower() != "false")
        self.setEnabled(endpoint is not None)
        self._loading = False
        self.refresh_preview()

    def add_row(
        self, name: str = "", value: str = "", enabled: bool = True,
        *, declared: bool = False, required: bool = False, focus: bool = False,
    ) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        include = QTableWidgetItem()
        include.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsUserCheckable
        )
        include.setCheckState(Qt.CheckState.Checked if enabled else Qt.CheckState.Unchecked)
        self.table.setItem(row, 0, include)
        key = QTableWidgetItem(name)
        key.setData(Qt.ItemDataRole.UserRole, declared)
        if declared:
            key.setFlags(key.flags() & ~Qt.ItemFlag.ItemIsEditable)
            key.setToolTip("Required catalog header" if required else "Catalog header")
        self.table.setItem(row, 1, key)
        edit = QLineEdit(value)
        edit.setAccessibleName(f"{name or 'Custom'} header value")
        edit.setPlaceholderText("Value or {{variable}}")
        edit.setEchoMode(
            QLineEdit.EchoMode.Password if name.lower() in self._secrets
            else QLineEdit.EchoMode.Normal
        )
        edit.textChanged.connect(self._changed)
        self.table.setCellWidget(row, 2, edit)
        self._changed()
        if focus:
            self.table.setCurrentCell(row, 1)
            self.table.editItem(key)

    def remove_selected(self) -> None:
        rows = {index.row() for index in self.table.selectedIndexes()}
        if not rows and self.table.currentRow() >= 0:
            rows = {self.table.currentRow()}
        self._loading = True
        for row in sorted(rows, reverse=True):
            key = self.table.item(row, 1)
            if key is not None and key.data(Qt.ItemDataRole.UserRole):
                self.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
            else:
                self.table.removeRow(row)
        self._loading = False
        self._changed()

    def values(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            key = self.table.item(row, 1)
            edit = self.table.cellWidget(row, 2)
            include = self.table.item(row, 0)
            if key is None or not isinstance(edit, QLineEdit) or include is None:
                continue
            name, value = key.text().strip(), edit.text()
            if not name and not value:
                continue
            # Keep last-row precedence even when duplicate names differ in casing.
            old_name = next(
                (key[7:] for key in result if key.startswith("header:")
                 and key[7:].lower() == name.lower()),
                None,
            )
            if old_name is not None:
                result.pop(f"header:{old_name}", None)
                result.pop(f"enabled:header:{old_name}", None)
            result[f"header:{name}"] = value
            result[f"enabled:header:{name}"] = (
                "true" if include.checkState() == Qt.CheckState.Checked else "false"
            )
        return result

    def set_context(
        self, environment: dict[str, str], variables: dict[str, str],
        managed: frozenset[str] = frozenset(), omitted: frozenset[str] = frozenset(),
        secrets: frozenset[str] = SECRET_HEADER_NAMES, error: str = "",
    ) -> None:
        self._environment = dict(environment)
        self._variables = dict(variables)
        self._managed, self._omitted, self._secrets = managed, omitted, secrets
        self._context_error = error
        self.refresh_preview()

    def _changed(self, *_args: object) -> None:
        if self._loading:
            return
        self.refresh_preview()
        self.changed.emit()

    def refresh_preview(self) -> None:
        values = self.values()
        own = request_headers(self.endpoint, values)
        combined = merge_headers({"Accept": "application/json"}, self._environment, own)
        sources = {"accept": "Default"}
        sources.update({name.lower(): "Environment" for name in self._environment})
        sources.update({name.lower(): "Request" for name in own})
        resolved = {
            name: str(value)
            for name, value in apply_variables(combined, self._variables).items()
        }
        issues = [self._context_error] if self._context_error else []
        try:
            validate_headers({
                name: value for name, value in resolved.items()
                if name.lower() not in self._omitted
            })
        except ValueError as exc:
            issues.append(str(exc))
        conflicts = {name.lower() for name, value in resolved.items() if value}.intersection(self._managed)
        if conflicts:
            issues.append(
                "Conflicts with managed authentication: " + ", ".join(sorted(conflicts))
                + ". Remove the override or choose Manual headers only."
            )
        supplied = {name.lower() for name, value in resolved.items() if value}
        missing = [
            parameter.name for parameter in self.endpoint.parameters
            if parameter.source == "header" and parameter.required
            and parameter.name.lower() not in self._omitted
            and parameter.name.lower() not in self._managed
            and values.get(f"enabled:header:{parameter.name}", "true") != "false"
            and parameter.name.lower() not in supplied
        ] if self.endpoint else []
        if missing:
            issues.append("Required header values are missing: " + ", ".join(missing))
        for row in range(self.table.rowCount()):
            key = self.table.item(row, 1)
            edit = self.table.cellWidget(row, 2)
            if key is not None and isinstance(edit, QLineEdit):
                edit.setAccessibleName(f"{key.text().strip() or 'Custom'} header value")
                edit.setEchoMode(
                    QLineEdit.EchoMode.Password if key.text().strip().lower() in self._secrets
                    else QLineEdit.EchoMode.Normal
                )
        self.warning.setText("\n".join(issues))
        rows = []
        for name, value in resolved.items():
            source = sources[name.lower()]
            if name.lower() in self._omitted:
                source, value = "Omitted (no authentication)", ""
            elif name.lower() in self._managed:
                source = "Conflict" if name.lower() in conflicts else "Managed authentication"
            if value and (name.lower() in self._secrets or name.lower() in self._managed):
                value = "******"
            rows.append((name, value, source))
        existing = {name.lower() for name in resolved}
        rows.extend(
            (name, "******", "Managed authentication")
            for name in sorted(self._managed - existing)
        )
        self.effective.setRowCount(len(rows))
        for row, items in enumerate(rows):
            for column, text in enumerate(items):
                self.effective.setItem(row, column, QTableWidgetItem(text))
