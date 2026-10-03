"""Module-aware query, sort, and payload widgets driven by the Rest Tester schemas."""

from __future__ import annotations

import json
from typing import Any

from PyQt6.QtCore import QSize, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QDoubleValidator, QIntValidator
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .icons import icon
from .schema import (
    MULTI_VALUE_OPERATORS,
    NULLABLE_OPERATORS,
    RANGE_OPERATORS,
    FilterField,
    FilterSchema,
    decode_filter,
    decode_sort,
    encode_filter,
    encode_sort,
    seed_filter_value,
    seed_payload_field,
)
from .widgets import attach_table_empty_state, settle_table_rows


ROW_HEIGHT = 38

#: Kinds a value list cannot sensibly be offered for: these hold structured or
#: binary content, not one of a set of scalars.
_UNLISTABLE_KINDS = frozenset({"array", "json", "object", "boolean", "file", "file_list"})


def _fit_action_cells(table: QTableWidget, column: int) -> None:
    for row in range(table.rowCount()):
        actions = table.cellWidget(row, column)
        actions.ensurePolished()
        for button in actions.findChildren(QPushButton):
            button.ensurePolished()
        actions.layout().invalidate()
        item = table.item(row, column)
        if item is None:
            item = QTableWidgetItem()
            table.setItem(row, column, item)
        # Match the styled table cell's 7px side padding and 1px grid line.
        item.setSizeHint(QSize(actions.sizeHint().width() + 15, ROW_HEIGHT))
    QTimer.singleShot(0, lambda: settle_table_rows(table))


def _coerce_scalar(text: str, kind: str) -> Any:
    """Converts dropdown text back to the type the field declares.

    Falls back to the raw string when the text does not parse, so an
    undocumented value typed into the editable combo still reaches the API and
    can exercise its validation.
    """
    if kind == "integer":
        try:
            return int(text)
        except ValueError:
            return text
    if kind == "number":
        try:
            return float(text)
        except ValueError:
            return text
    if kind == "boolean":
        lowered = text.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return text
    return text


class FilterValueEdit(QWidget):
    """Value cell of the query builder: a text box, or a dropdown when the
    field documents its values.

    The dropdown is editable and only appears for single-value operators —
    a range or `in` condition needs a comma-separated list, which a combo
    cannot express.
    """

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        # Tracked explicitly rather than read from isVisible(): a widget on a
        # hidden tab reports False, which would read the wrong control.
        self._listed = False
        self.line = QLineEdit()
        self.line.textChanged.connect(self._text_changed)
        layout.addWidget(self.line, 1)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.currentTextChanged.connect(self._text_changed)
        self.combo.hide()
        layout.addWidget(self.combo, 1)

    def _text_changed(self, _: str) -> None:
        self.changed.emit()

    def set_choices(self, values: list[str]) -> None:
        """Offers ``values``, or falls back to a plain text box when empty."""
        text = self.text()
        self._listed = bool(values)
        if values:
            self.combo.blockSignals(True)
            self.combo.clear()
            self.combo.addItem("")
            self.combo.addItems([str(value) for value in values])
            self.combo.setToolTip(
                "Allowed values:\n" + "\n".join(str(value) for value in values)
            )
            self.combo.setCurrentText(text)
            self.combo.blockSignals(False)
        else:
            self.line.blockSignals(True)
            self.line.setText(text)
            self.line.blockSignals(False)
        self.combo.setVisible(self._listed)
        self.line.setVisible(not self._listed)

    def text(self) -> str:
        return self.combo.currentText() if self._listed else self.line.text()

    def setText(self, value: str) -> None:
        text = "" if value is None else str(value)
        self.combo.setCurrentText(text)
        self.line.setText(text)

    def setPlaceholderText(self, hint: str) -> None:
        self.line.setPlaceholderText(hint)
        edit = self.combo.lineEdit()
        if edit is not None:
            edit.setPlaceholderText(hint)


class QueryBuilder(QWidget):
    """Builds a `Name__op:=value` filter string for one module's entity."""

    changed = pyqtSignal(str)

    COLUMNS = ("Logic", "Field", "Operator", "Value", "")

    def __init__(self, schema: FilterSchema | None = None) -> None:
        super().__init__()
        self.schema: FilterSchema | None = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.heading = QLabel()
        self.heading.setProperty("muted", True)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.empty_state = attach_table_empty_state(
            self.table,
            icon_name="filter",
            title="No conditions yet",
            guidance="Add a condition to filter this endpoint's results.",
        )
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.action_buttons = {
            "add": QPushButton("Add condition"),
            "seed": QPushButton("Seed all values"),
            "clear": QPushButton("Clear"),
        }
        self.action_buttons["add"].clicked.connect(lambda: self.add_condition())
        self.action_buttons["seed"].clicked.connect(self.seed_all)
        self.action_buttons["clear"].clicked.connect(self.clear)
        for button in self.action_buttons.values():
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.refresh_theme()

        layout.addWidget(QLabel("Encoded filter"))
        self.preview = QLineEdit()
        self.preview.setPlaceholderText("Name__eq:=value;Status__in:=Active,Locked")
        self.preview.editingFinished.connect(self._preview_edited)
        layout.addWidget(self.preview)

        self.set_schema(schema)

    def set_schema(self, schema: FilterSchema | None) -> None:
        self.schema = schema
        self.table.setRowCount(0)
        if schema is None:
            self.heading.setText(
                "No filter schema resolved for this module — edit the encoded filter directly."
            )
        else:
            self.heading.setText(
                f"Filtering <b>{schema.name}</b> — {len(schema.fields)} filterable fields. "
                "Conditions join with ; (AND) or | (OR)."
            )
        self.table.setEnabled(schema is not None)
        self._emit()

    def add_condition(self, condition: dict[str, Any] | None = None) -> None:
        if self.schema is None or not self.schema.fields:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)

        logic = QComboBox()
        logic.addItems(["AND", "OR"])
        logic.setEnabled(row > 0)
        if condition and not condition.get("and", True):
            logic.setCurrentText("OR")
        logic.currentIndexChanged.connect(self._emit)
        self.table.setCellWidget(row, 0, logic)

        field_box = QComboBox()
        for item in self.schema.fields:
            field_box.addItem(item.label, item.name)
        if condition:
            index = field_box.findData(condition["name"], Qt.ItemDataRole.UserRole)
            if index < 0:
                field_box.addItem(f"{condition['name']} (unknown)", condition["name"])
                index = field_box.count() - 1
            field_box.setCurrentIndex(index)
        field_box.currentIndexChanged.connect(lambda _, r=row: self._field_changed(r))
        self.table.setCellWidget(row, 1, field_box)

        operator_box = QComboBox()
        self.table.setCellWidget(row, 2, operator_box)
        operator_box.currentIndexChanged.connect(lambda _, r=row: self._operator_changed(r))

        value_edit = FilterValueEdit()
        self.table.setCellWidget(row, 3, value_edit)
        value_edit.changed.connect(self._emit)

        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(2, 0, 2, 0)
        actions_layout.setSpacing(4)
        seed = QPushButton("Seed")
        seed.setObjectName("rowButton")
        seed.setProperty("queryRowAction", "seed")
        seed.setIcon(icon("seed", theme.TEXT, 16))
        seed.setToolTip("Generate a value for this condition")
        seed.clicked.connect(lambda _, r=row: self.seed_row(r))
        remove = QPushButton()
        remove.setObjectName("rowRemoveButton")
        remove.setProperty("queryRowAction", "remove")
        remove.setIcon(icon("trash", theme.FAIL, 16))
        remove.setToolTip("Remove this condition")
        remove.setFixedWidth(30)
        remove.clicked.connect(lambda _, r=row: self.remove_row(r))
        actions_layout.addWidget(seed)
        actions_layout.addWidget(remove)
        self.table.setCellWidget(row, 4, actions)
        self.table.setRowHeight(row, ROW_HEIGHT)
        _fit_action_cells(self.table, 4)

        self._populate_operators(row, condition.get("operator") if condition else None)
        if condition:
            value_edit.setText(condition.get("value", ""))
        else:
            self.seed_row(row)
        self._emit()

    def refresh_theme(self) -> None:
        for icon_name, button in self.action_buttons.items():
            button.setIcon(icon(icon_name, theme.TEXT, 18))
        for button in self.table.findChildren(QPushButton):
            action = button.property("queryRowAction")
            if action == "seed":
                button.setIcon(icon("seed", theme.TEXT, 16))
            elif action == "remove":
                button.setIcon(icon("trash", theme.FAIL, 16))
        self.empty_state.refresh_theme()
        _fit_action_cells(self.table, 4)

    def _field_changed(self, row: int) -> None:
        self._populate_operators(row)
        if not self._loading:
            self.seed_row(row)

    def _populate_operators(self, row: int, selected: str | None = None) -> None:
        field = self.field_at(row)
        operator_box: QComboBox = self.table.cellWidget(row, 2)
        operator_box.blockSignals(True)
        operator_box.clear()
        if field is not None:
            for operator in field.operators:
                operator_box.addItem(f"{operator.label} ({operator.value})", operator.value)
        if selected:
            index = operator_box.findData(selected, Qt.ItemDataRole.UserRole)
            if index < 0:
                operator_box.addItem(f"{selected} (custom)", selected)
                index = operator_box.count() - 1
            operator_box.setCurrentIndex(index)
        operator_box.blockSignals(False)
        self._update_value_hint(row)

    def _operator_changed(self, row: int) -> None:
        self._update_value_hint(row)
        if not self._loading:
            self.seed_row(row)

    def _update_value_hint(self, row: int) -> None:
        field = self.field_at(row)
        operator = self.operator_at(row)
        value_edit: FilterValueEdit = self.table.cellWidget(row, 3)
        if field is None or not operator:
            return
        listed = False
        if operator in RANGE_OPERATORS:
            hint = "start,end"
        elif operator in MULTI_VALUE_OPERATORS:
            hint = "comma,separated,values"
        elif operator in NULLABLE_OPERATORS and field.nullable:
            hint = "leave empty for a null/empty check"
            listed = bool(field.lov)
        elif field.lov:
            hint = " | ".join(field.lov)
            listed = True
        else:
            hint = field.data_type
        value_edit.set_choices(list(field.lov) if listed else [])
        value_edit.setPlaceholderText(hint)

    def field_at(self, row: int) -> FilterField | None:
        if self.schema is None:
            return None
        box: QComboBox = self.table.cellWidget(row, 1)
        return self.schema.field(box.currentData(Qt.ItemDataRole.UserRole) or "")

    def operator_at(self, row: int) -> str:
        box: QComboBox = self.table.cellWidget(row, 2)
        return box.currentData(Qt.ItemDataRole.UserRole) or ""

    def seed_row(self, row: int) -> None:
        field = self.field_at(row)
        operator = self.operator_at(row)
        if field is None or not operator:
            return
        value_edit: FilterValueEdit = self.table.cellWidget(row, 3)
        value_edit.setText(seed_filter_value(field, operator))

    def seed_all(self) -> None:
        for row in range(self.table.rowCount()):
            self.seed_row(row)

    def remove_row(self, row: int) -> None:
        self.table.removeRow(row)
        self._rebind()
        self._emit()

    def clear(self) -> None:
        self.table.setRowCount(0)
        self._emit()

    def _rebind(self) -> None:
        """Re-attaches row-index-dependent callbacks after a removal."""
        conditions = self.conditions()
        self._loading = True
        self.table.setRowCount(0)
        for condition in conditions:
            self.add_condition(condition)
        self._loading = False

    def conditions(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            field_box: QComboBox = self.table.cellWidget(row, 1)
            logic: QComboBox = self.table.cellWidget(row, 0)
            value_edit: FilterValueEdit = self.table.cellWidget(row, 3)
            name = field_box.currentData(Qt.ItemDataRole.UserRole)
            operator = self.operator_at(row)
            if not name or not operator:
                continue
            result.append(
                {
                    "name": name,
                    "operator": operator,
                    "value": value_edit.text(),
                    "and": logic.currentText() == "AND",
                }
            )
        return result

    def filter_string(self) -> str:
        if self.table.rowCount() == 0:
            return self.preview.text().strip()
        return encode_filter(self.conditions())

    def set_filter_string(self, value: str) -> None:
        self._loading = True
        self.table.setRowCount(0)
        for condition in decode_filter(value):
            self.add_condition(condition)
        self._loading = False
        self.preview.setText(value)
        self._emit()

    def _preview_edited(self) -> None:
        if self._loading:
            return
        text = self.preview.text().strip()
        if text != encode_filter(self.conditions()):
            self.set_filter_string(text)

    def _emit(self) -> None:
        if self._loading:
            return
        if self.table.rowCount():
            self.preview.setText(encode_filter(self.conditions()))
        self.changed.emit(self.filter_string())


class SortBuilder(QWidget):
    """Builds a `Field,Field-` sort string from the module's sortable fields."""

    changed = pyqtSignal(str)

    def __init__(self, schema: FilterSchema | None = None) -> None:
        super().__init__()
        self.schema: FilterSchema | None = None
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel()
        self.heading.setProperty("muted", True)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("Order", "Field", "Direction", ""))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.empty_state = attach_table_empty_state(
            self.table,
            icon_name="sort",
            title="No sort fields yet",
            guidance="Add a sort field to order this endpoint's results.",
        )
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        self.action_buttons = {
            "add": QPushButton("Add sort field"),
            "clear": QPushButton("Clear"),
        }
        self.action_buttons["add"].clicked.connect(lambda: self.add_entry())
        self.action_buttons["clear"].clicked.connect(self.clear)
        buttons.addWidget(self.action_buttons["add"])
        buttons.addWidget(self.action_buttons["clear"])
        buttons.addStretch()
        layout.addLayout(buttons)
        self.refresh_theme()

        layout.addWidget(QLabel("Encoded sort"))
        self.preview = QLineEdit()
        self.preview.setPlaceholderText("Name,CreatedDate-")
        self.preview.editingFinished.connect(self._preview_edited)
        layout.addWidget(self.preview)

        self.set_schema(schema)

    def set_schema(self, schema: FilterSchema | None) -> None:
        self.schema = schema
        self.table.setRowCount(0)
        if schema is None:
            self.heading.setText(
                "No sort schema resolved for this module — edit the encoded sort directly."
            )
        else:
            self.heading.setText(
                f"Sorting <b>{schema.name}</b> — {len(schema.sortable_fields)} sortable fields. "
                "A trailing '-' means descending."
            )
        self.table.setEnabled(schema is not None)
        self._emit()

    def add_entry(self, entry: dict[str, Any] | None = None) -> None:
        if self.schema is None:
            return
        fields = self.schema.sortable_fields
        if not fields:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(row + 1)))

        field_box = QComboBox()
        for item in fields:
            field_box.addItem(item.label, item.name)
        if entry:
            index = field_box.findData(entry["name"], Qt.ItemDataRole.UserRole)
            if index < 0:
                field_box.addItem(f"{entry['name']} (unknown)", entry["name"])
                index = field_box.count() - 1
            field_box.setCurrentIndex(index)
        field_box.currentIndexChanged.connect(self._emit)
        self.table.setCellWidget(row, 1, field_box)

        direction = QComboBox()
        direction.addItems(["Ascending", "Descending"])
        if entry and entry.get("descending"):
            direction.setCurrentText("Descending")
        direction.currentIndexChanged.connect(self._emit)
        self.table.setCellWidget(row, 2, direction)

        remove = QPushButton()
        remove.setObjectName("rowRemoveButton")
        remove.setProperty("sortRowAction", "remove")
        remove.setIcon(icon("trash", theme.FAIL, 16))
        remove.setToolTip("Remove this sort field")
        remove.setFixedWidth(30)
        remove.clicked.connect(lambda _, r=row: self.remove_row(r))
        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(2, 0, 2, 0)
        holder_layout.addWidget(remove)
        self.table.setCellWidget(row, 3, holder)
        self.table.setRowHeight(row, ROW_HEIGHT)
        _fit_action_cells(self.table, 3)
        self._emit()

    def refresh_theme(self) -> None:
        for icon_name, button in self.action_buttons.items():
            button.setIcon(icon(icon_name, theme.TEXT, 18))
        for button in self.table.findChildren(QPushButton):
            if button.property("sortRowAction") == "remove":
                button.setIcon(icon("trash", theme.FAIL, 16))
        self.empty_state.refresh_theme()
        _fit_action_cells(self.table, 3)

    def remove_row(self, row: int) -> None:
        entries = self.entries()
        del entries[row]
        self.set_sort_string(encode_sort(entries))

    def clear(self) -> None:
        self.table.setRowCount(0)
        self._emit()

    def entries(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            field_box: QComboBox = self.table.cellWidget(row, 1)
            direction: QComboBox = self.table.cellWidget(row, 2)
            name = field_box.currentData(Qt.ItemDataRole.UserRole)
            if name:
                result.append(
                    {"name": name, "descending": direction.currentText() == "Descending"}
                )
        return result

    def sort_string(self) -> str:
        if self.table.rowCount() == 0:
            return self.preview.text().strip()
        return encode_sort(self.entries())

    def set_sort_string(self, value: str) -> None:
        self._loading = True
        self.table.setRowCount(0)
        for entry in decode_sort(value):
            self.add_entry(entry)
        self._loading = False
        self.preview.setText(value)
        self._emit()

    def _preview_edited(self) -> None:
        if self._loading:
            return
        text = self.preview.text().strip()
        if text != encode_sort(self.entries()):
            self.set_sort_string(text)

    def _emit(self) -> None:
        if self._loading:
            return
        if self.table.rowCount():
            self.preview.setText(encode_sort(self.entries()))
        self.changed.emit(self.sort_string())


class FormBuilder(QWidget):
    """Renders a ``multipart/form-data`` body as typed fields.

    Form values are always strings on the wire, so every editor is converted to
    its multipart representation; ``file`` fields hold a local path that
    :func:`api_tester.client.execute_endpoint` opens and uploads.
    """

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.schema: dict[str, Any] | None = None
        self._editors: list[tuple[str, QWidget, dict[str, Any]]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel()
        self.heading.setProperty("muted", True)
        layout.addWidget(self.heading)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.form = QFormLayout(self.container)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll, 1)

        buttons = QHBoxLayout()
        self.seed_button = QPushButton()
        self.seed_button.setToolTip("Seed all form fields")
        self.seed_button.setAccessibleName("Seed all form fields")
        self.seed_button.clicked.connect(self.seed)
        buttons.addWidget(self.seed_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.refresh_theme()

    @property
    def field_names(self) -> list[str]:
        return [name for name, _, _ in self._editors]

    def set_schema(self, schema: dict[str, Any] | None, values: dict[str, str] | None = None) -> None:
        self.schema = schema
        self._editors = []
        self.container.deleteLater()
        self.container = QWidget()
        self.form = QFormLayout(self.container)
        self.scroll.setWidget(self.container)
        self.seed_button.setEnabled(schema is not None)

        if schema is None:
            self.heading.setText("This endpoint does not send a form body.")
            return
        title = schema.get("clr_type") or schema.get("name") or "form"
        self.heading.setText(
            f"multipart/form-data fields of <b>{title}</b> (required fields are marked *)"
        )
        current = values or {}
        for field in schema.get("fields", []):
            name = field["name"]
            editor = self._create_editor(field)
            self._set_editor_value(editor, field, current.get(f"form:{name}", ""))
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(editor, 1)
            if field.get("kind") not in {"file", "file_list"}:
                seed = QPushButton()
                seed.setProperty("formFieldAction", "seed")
                seed.setToolTip(f"Seed {name}")
                seed.setAccessibleName(f"Seed {name}")
                seed.setFixedWidth(56)
                seed.setIcon(icon("seed", theme.TEXT, 16))
                seed.clicked.connect(
                    lambda _, f=field, e=editor: self._set_editor_value(
                        e, f, _as_form_text(seed_payload_field(f))
                    )
                )
                row_layout.addWidget(seed)
            label = QLabel(f"{name}{' *' if field.get('required') else ''}")
            label.setToolTip(field.get("clr_type", ""))
            self.form.addRow(label, row)
            self._editors.append((name, editor, field))

    def refresh_theme(self) -> None:
        self.seed_button.setIcon(icon("seed", theme.TEXT, 18))
        from .viewers import FilePicker

        for _name, editor, _field in self._editors:
            if isinstance(editor, FilePicker):
                editor.refresh_theme()
        for button in self.container.findChildren(QPushButton):
            if button.property("formFieldAction") == "seed":
                button.setIcon(icon("seed", theme.TEXT, 16))

    def _create_editor(self, field: dict[str, Any]) -> QWidget:
        from .viewers import FilePicker

        kind = field.get("kind", "text")
        if kind in {"file", "file_list"}:
            picker = FilePicker()
            picker.changed.connect(lambda _: self.changed.emit())
            if kind == "file_list":
                picker.path_edit.setPlaceholderText("Choose a file (one per request)")
            return picker
        if kind == "boolean":
            editor = QCheckBox()
            editor.toggled.connect(lambda _: self.changed.emit())
            return editor
        if kind == "enum":
            editor = QComboBox()
            editor.setEditable(True)
            editor.addItem("")
            for item in field.get("lov", []):
                editor.addItem(item)
            editor.currentTextChanged.connect(lambda _: self.changed.emit())
            return editor
        if kind == "json":
            editor = QPlainTextEdit()
            editor.setMaximumHeight(90)
            editor.textChanged.connect(self.changed.emit)
            return editor
        allowed = [str(item) for item in field.get("lov") or []]
        if allowed and kind not in _UNLISTABLE_KINDS:
            editor = QComboBox()
            editor.setEditable(True)
            editor.addItem("")
            for item in allowed:
                editor.addItem(item)
            editor.setToolTip("Allowed values:\n" + "\n".join(allowed))
            editor.currentTextChanged.connect(lambda _: self.changed.emit())
            return editor
        editor = QLineEdit()
        editor.setPlaceholderText(field.get("clr_type", ""))
        # Multipart values are strings, so numbers stay text editors: a spin box
        # cannot express "leave this nullable field unset".
        if kind == "integer":
            editor.setValidator(QIntValidator())
        elif kind == "number":
            validator = QDoubleValidator()
            validator.setNotation(QDoubleValidator.Notation.StandardNotation)
            editor.setValidator(validator)
        editor.textChanged.connect(lambda _: self.changed.emit())
        return editor

    def _set_editor_value(self, editor: QWidget, field: dict[str, Any], value: str) -> None:
        from .viewers import FilePicker

        text = "" if value is None else str(value)
        if isinstance(editor, FilePicker):
            editor.setText(text)
        elif isinstance(editor, QCheckBox):
            editor.setChecked(text.strip().lower() in {"true", "1", "yes", "on"})
        elif isinstance(editor, QComboBox):
            editor.setCurrentText(text)
        elif isinstance(editor, QPlainTextEdit):
            editor.setPlainText(text)
        elif isinstance(editor, QLineEdit):
            editor.setText(text)

    def seed(self) -> None:
        for _, editor, field in self._editors:
            if field.get("kind") in {"file", "file_list"}:
                continue  # A file path cannot be fabricated.
            self._set_editor_value(editor, field, _as_form_text(seed_payload_field(field)))
        self.changed.emit()

    def values(self) -> dict[str, str]:
        """Returns ``{"form:Name": "value"}`` for non-empty fields."""
        result: dict[str, str] = {}
        for name, editor, field in self._editors:
            text = self._editor_text(editor)
            if text:
                result[f"form:{name}"] = text
        return result

    def set_values(self, values: dict[str, str]) -> None:
        for name, editor, field in self._editors:
            self._set_editor_value(editor, field, values.get(f"form:{name}", ""))

    def _editor_text(self, editor: QWidget) -> str:
        from .viewers import FilePicker

        if isinstance(editor, FilePicker):
            return editor.text().strip()
        if isinstance(editor, QCheckBox):
            return "true" if editor.isChecked() else "false"
        if isinstance(editor, QComboBox):
            return editor.currentText().strip()
        if isinstance(editor, QPlainTextEdit):
            return editor.toPlainText().strip()
        if isinstance(editor, QLineEdit):
            return editor.text().strip()
        return ""


def _as_form_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


class PayloadForm(QWidget):
    """Renders a request payload as typed fields derived from its C# DTO."""

    def __init__(self) -> None:
        super().__init__()
        self.schema: dict[str, Any] | None = None
        self._editors: list[tuple[list[str], QWidget, dict[str, Any]]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.heading = QLabel()
        self.heading.setProperty("muted", True)
        self.heading.setWordWrap(True)
        layout.addWidget(self.heading)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.form = QFormLayout(self.container)
        self.scroll.setWidget(self.container)
        layout.addWidget(self.scroll)

    def set_schema(self, schema: dict[str, Any] | None, payload: Any = None) -> None:
        self.schema = schema
        self._editors.clear()
        self.container.deleteLater()
        self.container = QWidget()
        self.form = QFormLayout(self.container)
        self.scroll.setWidget(self.container)

        if schema is None:
            self.heading.setText("No DTO schema resolved — use the JSON tab.")
            return
        if schema.get("kind") == "array":
            self.heading.setText(
                f"<b>{schema.get('clr_type')}</b> is a collection — edit it on the JSON tab."
            )
            return
        self.heading.setText(
            f"Fields of <b>{schema.get('name')}</b> (required fields are marked *)"
        )
        values = payload if isinstance(payload, dict) else {}
        self._build_fields(schema.get("fields", []), self.form, [], values)

    def _build_fields(
        self,
        fields: list[dict[str, Any]],
        form: QFormLayout,
        prefix: list[str],
        values: dict[str, Any],
    ) -> None:
        for field in fields:
            name = field["name"]
            path = prefix + [name]
            value = values.get(name)
            label = f"{name}{' *' if field.get('required') else ''}"
            if field.get("kind") == "object" and field.get("fields"):
                group = QGroupBox(f"{name} ({field.get('clr_type')})")
                nested = QFormLayout(group)
                self._build_fields(
                    field["fields"], nested, path, value if isinstance(value, dict) else {}
                )
                form.addRow(group)
                continue
            editor = self._create_editor(field, value)
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(editor, 1)
            seed = QPushButton()
            seed.setProperty("payloadFieldAction", "seed")
            seed.setToolTip(f"Seed {'.'.join(path)}")
            seed.setAccessibleName(f"Seed {'.'.join(path)}")
            seed.setFixedWidth(56)
            seed.setIcon(icon("seed", theme.TEXT, 16))
            seed.clicked.connect(
                lambda _, f=field, e=editor: self._set_editor_value(e, f, seed_payload_field(f))
            )
            row_layout.addWidget(seed)
            label_widget = QLabel(label)
            label_widget.setToolTip(field.get("clr_type", ""))
            form.addRow(label_widget, row)
            self._editors.append((path, editor, field))

    def refresh_theme(self) -> None:
        for button in self.container.findChildren(QPushButton):
            if button.property("payloadFieldAction") == "seed":
                button.setIcon(icon("seed", theme.TEXT, 16))

    def _create_editor(self, field: dict[str, Any], value: Any) -> QWidget:
        kind = field.get("kind", "text")
        constraints = field.get("constraints", {})
        allowed = [str(item) for item in field.get("lov") or []]
        # A list of allowed values beats the kind-specific editor: a spin box
        # cannot offer a choice, and the catalog knows what the API accepts.
        if kind == "enum" or (allowed and kind not in _UNLISTABLE_KINDS):
            editor = QComboBox()
            editor.setEditable(True)
            if field.get("nullable") or not field.get("required"):
                editor.addItem("")
            for item in allowed:
                editor.addItem(item)
            if allowed:
                editor.setToolTip("Allowed values:\n" + "\n".join(allowed))
            if value is not None:
                editor.setCurrentText(str(value))
            return editor
        if kind == "boolean":
            editor = QCheckBox()
            editor.setChecked(bool(value))
            return editor
        if kind == "integer":
            editor = QSpinBox()
            editor.setRange(
                int(constraints.get("minimum", -2_147_483_648)),
                int(constraints.get("maximum", 2_147_483_647)),
            )
            if isinstance(value, (int, float)):
                editor.setValue(int(value))
            return editor
        if kind == "number":
            editor = QDoubleSpinBox()
            editor.setDecimals(4)
            editor.setRange(
                float(constraints.get("minimum", -1e12)),
                float(constraints.get("maximum", 1e12)),
            )
            if isinstance(value, (int, float)):
                editor.setValue(float(value))
            return editor
        if kind in {"array", "json"}:
            editor = QPlainTextEdit()
            editor.setMaximumHeight(90)
            editor.setPlainText(
                json.dumps(value, indent=2) if value is not None else ("[]" if kind == "array" else "{}")
            )
            return editor
        editor = QLineEdit()
        if constraints.get("max_length"):
            editor.setMaxLength(int(constraints["max_length"]))
        if value is not None:
            editor.setText(str(value))
        editor.setPlaceholderText(field.get("clr_type", ""))
        return editor

    def _set_editor_value(self, editor: QWidget, field: dict[str, Any], value: Any) -> None:
        if isinstance(editor, QComboBox):
            editor.setCurrentText("" if value is None else str(value))
        elif isinstance(editor, QCheckBox):
            editor.setChecked(bool(value))
        elif isinstance(editor, QSpinBox):
            editor.setValue(int(value or 0))
        elif isinstance(editor, QDoubleSpinBox):
            editor.setValue(float(value or 0))
        elif isinstance(editor, QPlainTextEdit):
            editor.setPlainText(json.dumps(value, indent=2))
        elif isinstance(editor, QLineEdit):
            editor.setText("" if value is None else str(value))

    def seed(self) -> None:
        for _, editor, field in self._editors:
            self._set_editor_value(editor, field, seed_payload_field(field))

    def payload(self) -> Any:
        document: dict[str, Any] = {}
        for path, editor, field in self._editors:
            value = self._editor_value(editor, field)
            target = document
            for part in path[:-1]:
                target = target.setdefault(part, {})
            target[path[-1]] = value
        return document

    def _editor_value(self, editor: QWidget, field: dict[str, Any]) -> Any:
        if isinstance(editor, QComboBox):
            text = editor.currentText().strip()
            if not text:
                return None
            # A dropdown holds text, but the field may be numeric or boolean:
            # sending "10" where the API expects 10 is a needless 400.
            return _coerce_scalar(text, field.get("kind", "text"))
        if isinstance(editor, QCheckBox):
            return editor.isChecked()
        if isinstance(editor, QSpinBox):
            return editor.value()
        if isinstance(editor, QDoubleSpinBox):
            return editor.value()
        if isinstance(editor, QPlainTextEdit):
            text = editor.toPlainText().strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
        if isinstance(editor, QLineEdit):
            text = editor.text()
            if not text and field.get("nullable"):
                return None
            return text
        return None
