"""The tabbed request editor shared by the API explorer and the suite editor.

Path, Query, Headers, Form and Payload JSON tabs, with the Filter, Sort and Payload
fields editors opened as modal dialogs. Keeping one implementation means a
test case is authored with exactly the same controls used to explore it.
"""

from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .builders import FormBuilder, PayloadForm, QueryBuilder, SortBuilder
from .catalog import Catalog, Endpoint
from .client import parameter_enabled_key, parameter_is_enabled
from .icons import icon
from .headers_ui import HeadersEditor
from .seeding import refresh_payload, seed_parameter
from .viewers import FilePicker, RequestBodyEditor, ValuePicker
from .widgets import EditorDialog, attach_table_empty_state


def merge_payload(base: dict, fields: dict) -> dict:
    """Overlay schema-built field values onto the JSON they were opened from.

    Keys the schema does not model are kept, so opening the fields editor and
    applying never silently drops hand-written JSON.
    """
    merged = dict(base)
    for key, value in fields.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_payload(merged[key], value)
        else:
            merged[key] = value
    return merged


class RequestEditor(QWidget):
    """Parameter, header and payload tabs for one endpoint request."""

    #: Emitted after any user edit to a parameter, form field or the payload.
    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self.endpoint: Endpoint | None = None
        self._payload_schema = None
        self._builder_parameters: set[str] = set()
        self._builder_value_edits: dict[str, QLineEdit] = {}
        self._builder_buttons: dict[str, QPushButton] = {}
        self._path_parameter_list: list = []
        self._query_parameter_list: list = []
        self._file_pickers: dict = {}
        self._draft_file_values: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.request_tabs = QTabWidget()
        self.request_tabs.setObjectName("requestBuilderTabs")

        path_page = QWidget()
        path_layout = QVBoxLayout(path_page)
        path_layout.setContentsMargins(0, 8, 0, 0)
        path_layout.setSpacing(8)
        self.path_parameters = self._new_parameter_table(["Name", "Type", "Value"])
        self.path_parameters.itemChanged.connect(self._emit_changed)
        self.path_parameters_empty_state = attach_table_empty_state(
            self.path_parameters,
            icon_name="fields",
            title="No path parameters",
            guidance="This endpoint's path has no placeholders to fill in.",
        )
        path_layout.addWidget(self.path_parameters, 1)
        path_actions = QHBoxLayout()
        path_actions.addStretch()
        self.path_seed_button = QPushButton("Seed path values")
        self.path_seed_button.clicked.connect(self.seed_path_parameters)
        path_actions.addWidget(self.path_seed_button)
        path_layout.addLayout(path_actions)
        self.path_tab_index = self.request_tabs.addTab(path_page, "Path")

        query_page = QWidget()
        query_layout = QVBoxLayout(query_page)
        query_layout.setContentsMargins(0, 8, 0, 0)
        query_layout.setSpacing(8)
        # Column 0 is the include toggle and column 4 hosts the Filter/Sort
        # builder buttons, so both carry no header text.
        self.query_parameters = self._new_parameter_table(
            ["", "Name", "Type", "Value", ""]
        )
        header = self.query_parameters.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.query_parameters.setColumnWidth(0, 40)
        self.query_parameters.setColumnWidth(1, 180)
        self.query_parameters.setColumnWidth(2, 100)
        self.query_parameters.setColumnWidth(4, 150)
        self.query_parameters.itemChanged.connect(self._emit_changed)
        self.query_parameters_empty_state = attach_table_empty_state(
            self.query_parameters,
            icon_name="fields",
            title="No query parameters",
            guidance="This endpoint accepts no query string parameters.",
        )
        query_layout.addWidget(self.query_parameters, 1)
        query_actions = QHBoxLayout()
        query_actions.addStretch()
        self.query_seed_button = QPushButton("Seed query values")
        self.query_seed_button.clicked.connect(self.seed_query_parameters)
        query_actions.addWidget(self.query_seed_button)
        query_layout.addLayout(query_actions)
        self.query_tab_index = self.request_tabs.addTab(query_page, "Query")

        # The Filter and Sort editors live in modal dialogs opened from their
        # query rows. The row's value cell stays the source of truth: each
        # open re-seeds the builder from it, so Cancel needs no undo.
        self.query_builder = QueryBuilder()
        self.filter_dialog = EditorDialog(
            self, "Filter builder", self.query_builder, "Apply filter"
        )
        self.sort_builder = SortBuilder()
        self.sort_dialog = EditorDialog(
            self, "Sort builder", self.sort_builder, "Apply sort", size=(760, 420)
        )

        self.form_builder = FormBuilder()
        self.form_builder.changed.connect(self._emit_changed)
        self.form_tab_index = self.request_tabs.addTab(self.form_builder, "Form")

        json_page = QWidget()
        json_layout = QVBoxLayout(json_page)
        json_layout.setContentsMargins(0, 8, 0, 0)
        json_row = QHBoxLayout()
        json_row.addWidget(QLabel("JSON payload sent with the request"))
        json_row.addStretch()
        self.payload_fields_button = QPushButton("Payload fields")
        self.payload_fields_button.setToolTip(
            "Edit this JSON as typed fields; Apply writes them back here"
        )
        self.payload_fields_button.setAccessibleName("Edit payload as fields")
        self.payload_fields_button.clicked.connect(self.open_payload_fields)
        json_row.addWidget(self.payload_fields_button)
        self.payload_seed_button = QPushButton()
        self.payload_seed_button.setToolTip("Generate or seed request payload data")
        self.payload_seed_button.setAccessibleName(
            "Generate or seed request payload data"
        )
        self.payload_seed_button.clicked.connect(self.seed_data)
        json_row.addWidget(self.payload_seed_button)
        self.payload_seed_layout = json_row
        json_layout.addLayout(json_row)
        self.payload = RequestBodyEditor()
        self.payload.changed.connect(self._emit_changed)
        json_layout.addWidget(self.payload)
        self.json_tab_index = self.request_tabs.addTab(json_page, "Payload JSON")
        self.headers_editor = HeadersEditor(self)
        self.headers_editor.changed.connect(self._emit_changed)
        self.headers_tab_index = self.request_tabs.addTab(self.headers_editor, "Headers")
        self.headers_editor.setEnabled(False)
        self.request_tabs.setTabEnabled(self.headers_tab_index, False)

        self.payload_form = PayloadForm()
        payload_buttons = QHBoxLayout()
        self.payload_fields_buttons = {"seed": QPushButton("Seed fields")}
        self.payload_fields_buttons["seed"].setToolTip("Seed all payload fields")
        self.payload_fields_buttons["seed"].setAccessibleName(
            "Seed all payload fields"
        )
        self.payload_fields_buttons["seed"].clicked.connect(self.payload_form.seed)
        payload_buttons.addWidget(self.payload_fields_buttons["seed"])
        self.payload_fields_layout = payload_buttons
        self.payload_fields_dialog = EditorDialog(
            self,
            "Payload fields",
            self.payload_form,
            "Apply to JSON",
            leading=payload_buttons,
            size=(780, 600),
        )

        layout.addWidget(self.request_tabs, 1)

    # -- loading --------------------------------------------------------------
    def load(
        self,
        catalog: Catalog,
        endpoint: Endpoint | None,
        values: dict[str, str] | None = None,
        payload_text: str | None = None,
    ) -> None:
        """Shows ``endpoint``'s request.

        ``values``/``payload_text`` are a saved draft. With no ``values`` the
        parameters are seeded; with no ``payload_text`` the endpoint's sample
        payload is used.
        """
        self._loading = True
        try:
            self._load(catalog, endpoint, values, payload_text)
        finally:
            self._loading = False

    def _load(
        self,
        catalog: Catalog,
        endpoint: Endpoint | None,
        values: dict[str, str] | None,
        payload_text: str | None,
    ) -> None:
        self.endpoint = endpoint
        has_draft = values is not None
        draft_values = values or {}
        self._draft_file_values = draft_values
        self.headers_editor.load(endpoint, draft_values)
        self.request_tabs.setTabEnabled(self.headers_tab_index, endpoint is not None)
        filter_schema = catalog.endpoint_filter_schema(endpoint) if endpoint else None
        self.query_builder.set_schema(filter_schema)
        self.sort_builder.set_schema(filter_schema)
        entity = (endpoint.filter_entity or endpoint.controller) if endpoint else ""
        self.filter_dialog.set_title(
            f"Filter builder \u2014 {entity}" if entity else "Filter builder"
        )
        self.sort_dialog.set_title(
            f"Sort builder \u2014 {entity}" if entity else "Sort builder"
        )
        sample_payload = endpoint.payload if endpoint else None
        draft_payload = sample_payload
        if payload_text and payload_text.strip():
            try:
                draft_payload = json.loads(payload_text)
            except json.JSONDecodeError:
                draft_payload = sample_payload
        payload_schema_name = endpoint.payload_schema if endpoint else None
        self._payload_schema = (
            catalog.payload_schema(payload_schema_name) if payload_schema_name else None
        )
        self.payload_form.set_schema(self._payload_schema, draft_payload)
        self.payload_fields_dialog.set_title(
            f"Payload fields \u2014 {payload_schema_name}"
            if payload_schema_name
            else "Payload fields"
        )
        form_schema = (
            catalog.form_schema(endpoint.form_schema)
            if endpoint and endpoint.form_schema
            else None
        )
        self.form_builder.set_schema(form_schema, draft_values)

        parameters = endpoint.parameters if endpoint else ()
        # Filter and Sort only get a builder when their entity schema resolved;
        # otherwise they stay plain editable rows.
        self._builder_parameters = {
            parameter.name
            for parameter in parameters
            if filter_schema is not None
            and parameter.source == "query"
            and parameter.name in ("Filter", "Sort")
        }
        self._path_parameter_list = [
            parameter for parameter in parameters if parameter.source == "path"
        ]
        # Form fields are owned by the Form tab. A form field without a form
        # schema has no other home, so it falls back to the query grid, whose
        # values are keyed by source and therefore still sent as form data.
        self._query_parameter_list = [
            parameter
            for parameter in parameters
            if parameter.source == "query"
            or (parameter.source == "form" and form_schema is None)
        ]

        def initial_value(parameter) -> str:
            key = f"{parameter.source}:{parameter.name}"
            if has_draft:
                return draft_values.get(key, "")
            if parameter.name in self._builder_parameters:
                return ""
            if parameter.source == "form" and "file" in parameter.type.lower():
                return ""
            return seed_parameter(parameter)

        self._file_pickers = {}
        self._builder_value_edits = {}
        self._builder_buttons = {}

        self.path_parameters.setRowCount(0)
        self.path_parameters.setRowCount(len(self._path_parameter_list))
        for row, parameter in enumerate(self._path_parameter_list):
            self._set_readonly_cell(self.path_parameters, row, 0, parameter.name)
            self._set_readonly_cell(self.path_parameters, row, 1, parameter.type)
            self._install_value_editor(
                self.path_parameters, row, 2, parameter, initial_value(parameter)
            )

        self.query_parameters.setRowCount(0)
        self.query_parameters.setRowCount(len(self._query_parameter_list))
        for row, parameter in enumerate(self._query_parameter_list):
            include = QTableWidgetItem()
            include.setFlags(
                (include.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsEditable
            )
            if parameter.source == "query":
                include.setCheckState(
                    Qt.CheckState.Checked
                    if parameter_is_enabled(draft_values, "query", parameter.name)
                    else Qt.CheckState.Unchecked
                )
            else:
                include.setFlags(include.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            self.query_parameters.setItem(row, 0, include)
            self._set_readonly_cell(self.query_parameters, row, 1, parameter.name)
            self._set_readonly_cell(
                self.query_parameters,
                row,
                2,
                parameter.type
                if parameter.source == "query"
                else f"form \u00b7 {parameter.type}",
            )
            value = initial_value(parameter)
            if parameter.name in self._builder_parameters:
                self._install_builder_row(row, parameter.name, value)
            else:
                self._install_value_editor(
                    self.query_parameters, row, 3, parameter, value
                )

        path_count = len(self._path_parameter_list)
        query_count = len(self._query_parameter_list)
        self.request_tabs.setTabText(
            self.path_tab_index, f"Path  {path_count}" if path_count else "Path"
        )
        self.request_tabs.setTabText(
            self.query_tab_index, f"Query  {query_count}" if query_count else "Query"
        )
        self.request_tabs.setTabEnabled(self.path_tab_index, path_count > 0)
        self.request_tabs.setTabEnabled(self.query_tab_index, query_count > 0)
        self.request_tabs.setTabEnabled(self.form_tab_index, form_schema is not None)
        has_payload = endpoint is not None and (
            endpoint.payload_schema is not None or endpoint.payload is not None
        )
        self.request_tabs.setTabEnabled(
            self.json_tab_index, has_payload or bool(payload_text and payload_text.strip())
        )
        self.payload_fields_button.setEnabled(self._payload_schema is not None)
        self.payload_fields_button.setToolTip(
            "Edit this JSON as typed fields; Apply writes them back here"
            if self._payload_schema is not None
            else "No payload schema resolved for this endpoint \u2014 edit the JSON directly"
        )
        self.payload_seed_button.setEnabled(endpoint is not None)
        self.select_first_enabled_tab()
        self.refresh_theme()
        if payload_text is not None:
            self.payload.setPlainText(payload_text)
        else:
            self.payload.setPlainText(
                "" if sample_payload is None else json.dumps(sample_payload, indent=2)
            )

    # -- reading --------------------------------------------------------------
    def values(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for row, parameter in enumerate(self._path_parameter_list):
            values[f"path:{parameter.name}"] = self._parameter_value(
                self.path_parameters, row, 2
            )
        for row, parameter in enumerate(self._query_parameter_list):
            source, name = parameter.source, parameter.name
            values[f"{source}:{name}"] = self._parameter_value(
                self.query_parameters, row, 3
            )
            if source == "query":
                include = self.query_parameters.item(row, 0)
                values[parameter_enabled_key(source, name)] = (
                    "true"
                    if include is not None
                    and include.checkState() == Qt.CheckState.Checked
                    else "false"
                )
        values.update(self.form_builder.values())
        for row, parameter in enumerate(self._query_parameter_list):
            picker = self.query_parameters.cellWidget(row, 3)
            if isinstance(picker, FilePicker):
                values.update(picker.option_values(parameter.name))
        values.update(self.headers_editor.values())
        return values

    def payload_text(self) -> str:
        return self.payload.toPlainText()

    def set_file_secret_names(self, names: frozenset[str]) -> None:
        for picker in self.findChildren(FilePicker):
            picker.secret_names = names

    @staticmethod
    def _parameter_value(table: QTableWidget, row: int, column: int) -> str:
        # Builder rows hold their encoded string in a line edit; every other
        # editor (file and value pickers) mirrors itself into the cell item.
        widget = table.cellWidget(row, column)
        if isinstance(widget, QLineEdit):
            return widget.text()
        item = table.item(row, column)
        return item.text() if item is not None else ""

    # -- actions --------------------------------------------------------------
    def form_to_json(self, base: object = None) -> None:
        payload = self.payload_form.payload()
        if payload is None:
            return
        if isinstance(base, dict) and isinstance(payload, dict):
            payload = merge_payload(base, payload)
        self.payload.setPlainText(json.dumps(payload, indent=2))
        self.request_tabs.setCurrentIndex(self.json_tab_index)

    def open_payload_fields(self) -> None:
        if self._payload_schema is None:
            return
        text = self.payload.toPlainText().strip()
        parsed: object = None
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as error:
                QMessageBox.warning(
                    self,
                    "Payload fields",
                    "The payload JSON is not valid, so it cannot be opened as "
                    f"fields.\n\n{error.msg} (line {error.lineno}, column {error.colno})",
                )
                return
        seed = parsed if parsed is not None else (
            self.endpoint.payload if self.endpoint else None
        )
        self.payload_form.set_schema(self._payload_schema, seed)
        if self.payload_fields_dialog.exec() == QDialog.DialogCode.Accepted:
            # Merge so JSON keys the schema does not model survive the round trip.
            self.form_to_json(base=parsed)

    def open_filter_builder(self) -> None:
        self._open_builder("Filter", self.filter_dialog)

    def open_sort_builder(self) -> None:
        self._open_builder("Sort", self.sort_dialog)

    def _open_builder(self, name: str, dialog: EditorDialog) -> None:
        edit = self._builder_value_edits.get(name)
        if edit is None:
            return
        if name == "Filter":
            self.query_builder.set_filter_string(edit.text())
        else:
            self.sort_builder.set_sort_string(edit.text())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        edit.setText(
            self.query_builder.filter_string()
            if name == "Filter"
            else self.sort_builder.sort_string()
        )
        self._include_query_parameter(name)

    def _include_query_parameter(self, name: str) -> None:
        for row, parameter in enumerate(self._query_parameter_list):
            if parameter.source == "query" and parameter.name == name:
                include = self.query_parameters.item(row, 0)
                if include is not None:
                    include.setCheckState(Qt.CheckState.Checked)
                return

    def seed_path_parameters(self) -> None:
        for row, parameter in enumerate(self._path_parameter_list):
            self._seed_value_cell(self.path_parameters, row, 2, parameter)

    def seed_query_parameters(self) -> None:
        for row, parameter in enumerate(self._query_parameter_list):
            if parameter.name in self._builder_parameters:
                if parameter.name == "Filter":
                    edit = self._builder_value_edits["Filter"]
                    self.query_builder.set_filter_string(edit.text())
                    self.query_builder.seed_all()
                    edit.setText(self.query_builder.filter_string())
                continue
            self._seed_value_cell(self.query_parameters, row, 3, parameter)

    def _seed_value_cell(
        self, table: QTableWidget, row: int, column: int, parameter
    ) -> None:
        widget = table.cellWidget(row, column)
        if isinstance(widget, FilePicker):
            return  # A file path cannot be seeded.
        seeded = seed_parameter(parameter)
        if isinstance(widget, ValuePicker):
            widget.setText(seeded)  # The widget mirrors itself into the cell.
        else:
            table.item(row, column).setText(seeded)

    def seed_parameters(self) -> None:
        self.seed_path_parameters()
        self.seed_query_parameters()

    def seed_data(self) -> None:
        if self.endpoint is None:
            return
        self.seed_parameters()
        if self.form_builder.schema is not None:
            self.form_builder.seed()
        if self._payload_schema is not None:
            self.payload_form.set_schema(self._payload_schema, self.endpoint.payload)
            self.payload_form.seed()
            self.form_to_json()
        elif self.endpoint.payload is not None:
            self.payload.setPlainText(
                json.dumps(refresh_payload(self.endpoint.payload), indent=2)
            )

    # -- presentation ---------------------------------------------------------
    def select_first_enabled_tab(self) -> None:
        if self.request_tabs.isTabEnabled(self.request_tabs.currentIndex()):
            return
        for index in range(self.request_tabs.count()):
            if self.request_tabs.isTabEnabled(index):
                self.request_tabs.setCurrentIndex(index)
                return

    def refresh_icons(self) -> None:
        for button in (self.path_seed_button, self.query_seed_button, self.payload_seed_button):
            button.setIcon(icon("seed", theme.TEXT, 18))
        self.payload_fields_button.setIcon(icon("fields", theme.TEXT, 18))
        for button in self.payload_fields_buttons.values():
            button.setIcon(icon("seed", theme.TEXT, 18))
        for name, button in self._builder_buttons.items():
            button.setIcon(icon("filter" if name == "Filter" else "sort", theme.TEXT, 16))

    def refresh_theme(self) -> None:
        self.refresh_icons()
        self.payload.refresh_theme()
        self.form_builder.refresh_theme()
        self.payload_form.refresh_theme()
        self.query_builder.refresh_theme()
        self.sort_builder.refresh_theme()
        self.path_parameters_empty_state.refresh_theme()
        self.query_parameters_empty_state.refresh_theme()
        self.headers_editor.empty_state.refresh_theme()

    # -- construction helpers -------------------------------------------------
    def _emit_changed(self, *_args: Any) -> None:
        if not self._loading:
            self.changed.emit()

    @staticmethod
    def _new_parameter_table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setObjectName("parametersTable")
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(40)
        table.setShowGrid(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(
            headers.index("Value"), QHeaderView.ResizeMode.Stretch
        )
        return table

    @staticmethod
    def _set_readonly_cell(
        table: QTableWidget, row: int, column: int, text: str
    ) -> None:
        cell = QTableWidgetItem(text)
        cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, column, cell)

    def _install_value_editor(
        self, table: QTableWidget, row: int, column: int, parameter, value: str
    ) -> None:
        cell = QTableWidgetItem(value)
        table.setItem(row, column, cell)
        is_file = parameter.source == "form" and "file" in parameter.type.lower()
        if is_file:
            cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
            picker = FilePicker()
            picker.setText(value)
            picker.set_options(parameter.name, self._draft_file_values)
            # Mirror the chosen path into the cell so values() still reads it.
            picker.changed.connect(lambda text, item=cell: item.setText(text))
            picker.changed.connect(self._emit_changed)
            table.setCellWidget(row, column, picker)
            self._file_pickers[(id(table), row)] = picker
        elif parameter.values:
            chooser = ValuePicker(list(parameter.values))
            chooser.setText(value)
            chooser.changed.connect(lambda text, item=cell: item.setText(text))
            table.setCellWidget(row, column, chooser)

    def _install_builder_row(self, row: int, name: str, value: str) -> None:
        edit = QLineEdit(value)
        edit.setPlaceholderText(
            "Name__op:=value;\u2026  \u2014 or open the builder"
            if name == "Filter"
            else "Field,Other-  \u2014 or open the builder"
        )
        edit.setAccessibleName(f"{name} query value")
        edit.textChanged.connect(self._emit_changed)
        self.query_parameters.setCellWidget(row, 3, edit)
        self._builder_value_edits[name] = edit

        button = QPushButton(f"{name} builder")
        button.setToolTip(f"Open the {name.lower()} builder")
        button.setAccessibleName(f"Open {name.lower()} builder")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(
            self.open_filter_builder if name == "Filter" else self.open_sort_builder
        )
        self.query_parameters.setCellWidget(row, 4, button)
        self._builder_buttons[name] = button
