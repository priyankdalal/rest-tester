"""Editors for the catalog's filter, payload, form, and enum schemas.

The scanners derive these from source, but a hand-built catalog needs them
authored directly. Each widget here writes the exact JSON shape
:mod:`api_tester.builders` consumes, so a schema typed in by hand drives the
same query builder and payload form as a scanned one.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
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

from .catalog_builder import (
    FILTER_DATA_TYPES,
    PAYLOAD_KINDS,
    default_clr_type,
    display_name_for,
    new_filter_field,
    new_payload_field,
    operators_for,
)
from .icons import icon
from .widgets import (
    attach_table_empty_state,
    button_in_cell,
    cell_button,
    center_in_cell,
    fit_combo_column,
    fit_last_column,
    set_text_glyph,
    settle_table_rows,
)


def _checkbox_cell(checked: bool, on_toggle: Callable[[], None]) -> QWidget:
    """A centred checkbox usable as a table cell widget."""
    box = QCheckBox()
    box.setChecked(checked)
    box.toggled.connect(lambda _: on_toggle())
    return center_in_cell(box)


def _cell_checked(table: QTableWidget, row: int, column: int) -> bool:
    holder = table.cellWidget(row, column)
    box = holder.findChild(QCheckBox) if holder else None
    return bool(box.isChecked()) if box else False


def _text(table: QTableWidget, row: int, column: int) -> str:
    item = table.item(row, column)
    return item.text().strip() if item else ""


class ValueListWidget(QWidget):
    """Edits a list of values, optionally copying them from a catalog enum."""

    def __init__(
        self,
        values: list[str],
        enums: dict[str, list[str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if enums:
            row = QHBoxLayout()
            row.addWidget(QLabel("Copy from enum"))
            self.enum_combo = QComboBox()
            self.enum_combo.addItem("")
            self.enum_combo.addItems(sorted(enums))
            row.addWidget(self.enum_combo, 1)
            copy = QPushButton("Copy")
            set_text_glyph(copy, "duplicate")
            copy.setToolTip("Replace the list with that enum's members")
            copy.clicked.connect(lambda: self._copy_enum(enums))
            row.addWidget(copy)
            layout.addLayout(row)

        self.list = QListWidget()
        for value in values:
            self._append(str(value))
        layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        add = QPushButton("Add value")
        set_text_glyph(add, "add")
        add.clicked.connect(lambda: self._append("new-value", edit=True))
        buttons.addWidget(add)
        remove = QPushButton("Delete value")
        remove.setProperty("danger", True)
        remove.setIcon(icon("trash", "#E5484D"))
        remove.clicked.connect(self._remove_selected)
        buttons.addWidget(remove)
        buttons.addStretch()
        layout.addLayout(buttons)

    def _append(self, value: str, edit: bool = False) -> None:
        item = QListWidgetItem(value)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.list.addItem(item)
        if edit:
            self.list.setCurrentItem(item)
            self.list.editItem(item)

    def _copy_enum(self, enums: dict[str, list[str]]) -> None:
        name = self.enum_combo.currentText().strip()
        if not name:
            return
        self.list.clear()
        for value in enums.get(name, []):
            self._append(str(value))

    def _remove_selected(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def values(self) -> list[str]:
        return [
            self.list.item(index).text().strip()
            for index in range(self.list.count())
            if self.list.item(index).text().strip()
        ]


class ValueListDialog(QDialog):
    """Modal wrapper around :class:`AllowedValuesWidget`."""

    def __init__(
        self,
        field: dict[str, Any],
        enums: dict[str, list[str]],
        title: str = "Allowed values",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(440, 480)
        layout = QVBoxLayout(self)
        self.editor = AllowedValuesWidget(dict(field), enums)
        layout.addWidget(self.editor, 1)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def result_values(self) -> dict[str, Any]:
        """Returns ``{"lov": [...], "enum": name | None}``."""
        result = self.editor.apply_to({})
        result.setdefault("enum", None)
        return result

    def values(self) -> list[str]:
        return self.result_values()["lov"]


class AllowedValuesWidget(QWidget):
    """Chooses the values a field accepts: any, an enum, or a custom list.

    The three modes match the parameter editor. "From an enum" stores a live
    reference, so editing the enum updates every field that uses it.
    """

    ANY, ENUM, CUSTOM = "Any value", "From an enum", "Custom list"

    changed = pyqtSignal()

    def __init__(
        self,
        field: dict[str, Any],
        enums: dict[str, list[str]],
        label: str = "Allowed values",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.enums = enums
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItems([self.ANY, self.ENUM, self.CUSTOM])
        self.mode.currentTextChanged.connect(self._mode_changed)
        form.addRow(label, self.mode)
        self.enum_combo = QComboBox()
        self.enum_combo.addItems(sorted(enums))
        self.enum_combo.currentTextChanged.connect(lambda _: self.changed.emit())
        form.addRow("Enum", self.enum_combo)
        layout.addLayout(form)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setProperty("fieldCaption", True)
        layout.addWidget(self.hint)

        self.custom = ValueListWidget(
            [str(value) for value in field.get("lov") or []], enums
        )
        layout.addWidget(self.custom, 1)

        enum_name = field.get("enum")
        if enum_name:
            self.mode.setCurrentText(self.ENUM)
            if enum_name in enums:
                self.enum_combo.setCurrentText(str(enum_name))
            else:
                self.enum_combo.insertItem(0, str(enum_name))
                self.enum_combo.setCurrentIndex(0)
        elif field.get("lov"):
            self.mode.setCurrentText(self.CUSTOM)
        self._mode_changed(self.mode.currentText())

    def _mode_changed(self, mode: str) -> None:
        self.enum_combo.setEnabled(mode == self.ENUM)
        self.custom.setEnabled(mode == self.CUSTOM)
        self.hint.setText(
            {
                self.ANY: "The request builder accepts any value.",
                self.ENUM: (
                    "The request builder offers this enum's members. The "
                    "reference stays live, so editing the enum updates the field."
                ),
                self.CUSTOM: "The request builder offers exactly these values.",
            }[mode]
        )
        self.changed.emit()

    def apply_to(self, field: dict[str, Any], keep_lov: bool = True) -> dict[str, Any]:
        """Writes ``enum`` and ``lov`` onto ``field`` and returns it.

        An enum reference also caches its members in ``lov`` so the catalog
        stays readable on its own; the reference wins when it is next loaded.
        """
        mode = self.mode.currentText()
        if mode == self.ENUM and self.enum_combo.currentText().strip():
            name = self.enum_combo.currentText().strip()
            field["enum"] = name
            field["lov"] = [str(value) for value in self.enums.get(name, [])]
        elif mode == self.CUSTOM and self.custom.values():
            field.pop("enum", None)
            field["lov"] = self.custom.values()
        else:
            field.pop("enum", None)
            if keep_lov:
                field["lov"] = []
            else:
                field.pop("lov", None)
        return field


def values_caption(field: dict[str, Any], enums: dict[str, list[str]]) -> str:
    """One-line summary of a field's allowed values, for a button label."""
    enum_name = field.get("enum")
    if enum_name:
        return f"{enum_name} (missing)" if enum_name not in enums else str(enum_name)
    count = len(field.get("lov") or [])
    return f"{count} value(s)" if count else "Any value"


class OperatorDialog(QDialog):
    """Restricts a filter field to a subset of its type's operators."""

    def __init__(
        self,
        data_type: str,
        selected: list[dict[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Operators for {data_type}")
        self.resize(360, 380)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"The platform supports these operators for {data_type} fields. "
                "Untick any this endpoint must not offer."
            )
        )
        chosen = {item.get("value") for item in selected}
        self.boxes: list[tuple[QCheckBox, dict[str, str]]] = []
        for operator in operators_for(data_type):
            box = QCheckBox(f"{operator['label']}  ({operator['value']})")
            box.setChecked(not selected or operator["value"] in chosen)
            layout.addWidget(box)
            self.boxes.append((box, operator))
        layout.addStretch()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def operators(self) -> list[dict[str, str]]:
        return [dict(operator) for box, operator in self.boxes if box.isChecked()]


class FilterFieldTable(QWidget):
    """Edits the fields of one filter entity."""

    changed = pyqtSignal()

    COLUMNS = (
        "Name",
        "Display name",
        "Data type",
        "CLR type",
        "Nullable",
        "Sortable",
        "Values",
        "Operators",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.enums: dict[str, list[str]] = {}
        self._operators: dict[int, list[dict[str, str]]] = {}
        self._next_key = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(64)
        for index, width in enumerate((150, 140, 115, 120, 70, 70, 120)):
            self.table.setColumnWidth(index, width)
        # The checkbox columns only need to clear their own heading, and the
        # last column absorbs the slack so the row never needs a sideways
        # scroll to reach its buttons. It gets no explicit width: a width set
        # before the stretch mode is applied survives as the section size and
        # pushes the total past the viewport.
        for index in (4, 5):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table.itemChanged.connect(self._name_edited)
        self.empty_state = attach_table_empty_state(
            self.table,
            icon_name="filter",
            title="No filterable fields",
            guidance="Add a field to describe what this endpoint can filter and sort on.",
        )
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        add = QPushButton("Add field")
        set_text_glyph(add, "add")
        add.clicked.connect(lambda: self.append(new_filter_field("NewField", "Text")))
        row.addWidget(add)
        remove = QPushButton("Delete field")
        remove.setProperty("danger", True)
        remove.setIcon(icon("trash", "#E5484D"))
        remove.clicked.connect(self._remove_selected)
        row.addWidget(remove)
        row.addStretch()
        self.hint = QLabel(
            "Operators follow the platform's supported operations for each data type."
        )
        self.hint.setProperty("fieldCaption", True)
        row.addWidget(self.hint)
        layout.addLayout(row)

    # ------------------------------------------------------------------ rows

    def append(self, field: dict[str, Any]) -> None:
        index = self.table.rowCount()
        self.table.blockSignals(True)
        self.table.insertRow(index)
        key = self._next_key
        self._next_key += 1

        name_item = QTableWidgetItem(str(field.get("name", "")))
        name_item.setData(Qt.ItemDataRole.UserRole, key)
        self.table.setItem(index, 0, name_item)
        self.table.setItem(
            index, 1, QTableWidgetItem(str(field.get("display_name", "")))
        )

        data_type = str(field.get("data_type", "Text"))
        combo = QComboBox()
        combo.addItems(FILTER_DATA_TYPES)
        combo.setCurrentText(data_type if data_type in FILTER_DATA_TYPES else "Text")
        combo.currentTextChanged.connect(
            lambda value, k=key: self._data_type_changed(k, value)
        )
        self.table.setCellWidget(index, 2, combo)
        fit_combo_column(self.table, 2, combo)

        self.table.setItem(
            index,
            3,
            QTableWidgetItem(str(field.get("clr_type", "") or default_clr_type(data_type))),
        )
        self.table.setCellWidget(
            index, 4, _checkbox_cell(bool(field.get("nullable")), self.changed.emit)
        )
        self.table.setCellWidget(
            index,
            5,
            _checkbox_cell(bool(field.get("sortable", True)), self.changed.emit),
        )

        values_holder, values_button = cell_button(
            tooltip="Restrict this field to an enum or a list of values"
        )
        values_button.clicked.connect(lambda _, k=key: self._edit_values(k))
        self.table.setCellWidget(index, 6, values_holder)

        operators_holder, operators_button = cell_button(
            tooltip="Choose the operators this field offers"
        )
        operators_button.clicked.connect(lambda _, k=key: self._edit_operators(k))
        self.table.setCellWidget(index, 7, operators_holder)

        self._values: dict[int, dict[str, Any]] = getattr(self, "_values", {})
        self._values[key] = {
            "lov": [str(item) for item in field.get("lov", []) or []],
            "enum": field.get("enum") or None,
        }
        self._operators[key] = [
            dict(item) for item in field.get("operators", []) or []
        ] or operators_for(combo.currentText())
        self.table.blockSignals(False)
        self._refresh_buttons(key)
        self.changed.emit()

    def _row_for_key(self, key: int) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                return row
        return -1

    def _refresh_buttons(self, key: int) -> None:
        row = self._row_for_key(key)
        if row < 0:
            return
        values = self._values.get(key, {})
        values_button = button_in_cell(self.table, row, 6)
        if values_button is not None:
            values_button.setText(values_caption(values, self.enums))
        operators = self._operators.get(key, [])
        operator_button = button_in_cell(self.table, row, 7)
        if operator_button is not None:
            operator_button.setText(f"{len(operators)} operator(s)")

    def _name_edited(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            display = self.table.item(item.row(), 1)
            if display is not None and not display.text().strip():
                self.table.blockSignals(True)
                display.setText(display_name_for(item.text().strip()))
                self.table.blockSignals(False)
        self.changed.emit()

    def _data_type_changed(self, key: int, data_type: str) -> None:
        row = self._row_for_key(key)
        if row < 0:
            return
        self._operators[key] = operators_for(data_type)
        clr_item = self.table.item(row, 3)
        if clr_item is not None and clr_item.text().strip() in set(
            default_clr_type(candidate) for candidate in FILTER_DATA_TYPES
        ) | {""}:
            self.table.blockSignals(True)
            clr_item.setText(default_clr_type(data_type))
            self.table.blockSignals(False)
        self._refresh_buttons(key)
        self.changed.emit()

    def _edit_values(self, key: int) -> None:
        row = self._row_for_key(key)
        name = _text(self.table, row, 0) if row >= 0 else ""
        dialog = ValueListDialog(
            self._values.get(key, {}),
            self.enums,
            title=f"Allowed values for {name or 'field'}",
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._values[key] = dialog.result_values()
            self._refresh_buttons(key)
            self.changed.emit()

    def _edit_operators(self, key: int) -> None:
        row = self._row_for_key(key)
        if row < 0:
            return
        combo = self.table.cellWidget(row, 2)
        data_type = combo.currentText() if isinstance(combo, QComboBox) else "Text"
        dialog = OperatorDialog(data_type, self._operators.get(key, []), parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            chosen = dialog.operators()
            if not chosen:
                QMessageBox.warning(
                    self,
                    "No operators",
                    "A filter field needs at least one operator. Keeping the "
                    "platform defaults.",
                )
                chosen = operators_for(data_type)
            self._operators[key] = chosen
            self._refresh_buttons(key)
            self.changed.emit()

    def _remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                key = item.data(Qt.ItemDataRole.UserRole)
                self._operators.pop(key, None)
                self._values.pop(key, None)
            self.table.removeRow(row)
        if rows:
            self.changed.emit()

    # ---------------------------------------------------------------- access

    def set_fields(self, fields: list[dict[str, Any]], enums: dict[str, list[str]]) -> None:
        self.enums = enums
        self._operators.clear()
        self._values = {}
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        for field in fields:
            self.append(field)
        settle_table_rows(self.table)

    def showEvent(self, event) -> None:
        """Re-fits rows once the stylesheet has been applied.

        Row padding is only known after the table is polished, so a fit done
        while hidden measures the wrong deficit and leaves the cell buttons
        clipped.
        """
        super().showEvent(event)
        settle_table_rows(self.table)
        fit_last_column(self.table)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        fit_last_column(self.table)

    def fields(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            name = _text(self.table, row, 0)
            if not name:
                continue
            item = self.table.item(row, 0)
            key = item.data(Qt.ItemDataRole.UserRole) if item else -1
            combo = self.table.cellWidget(row, 2)
            data_type = combo.currentText() if isinstance(combo, QComboBox) else "Text"
            values = self._values.get(key, {})
            field = {
                "name": name,
                "display_name": _text(self.table, row, 1) or display_name_for(name),
                "data_type": data_type,
                "clr_type": _text(self.table, row, 3) or default_clr_type(data_type),
                "nullable": _cell_checked(self.table, row, 4),
                "sortable": _cell_checked(self.table, row, 5),
                "lov": list(values.get("lov", [])),
                "operators": [
                    dict(operator)
                    for operator in self._operators.get(key, [])
                    or operators_for(data_type)
                ],
            }
            if values.get("enum"):
                field["enum"] = str(values["enum"])
            result.append(field)
        return result


class FieldDetailsDialog(QDialog):
    """Edits the parts of a payload field a table cell cannot hold.

    Arrays carry an ``item`` descriptor and objects carry nested ``fields``;
    every other kind may restrict its allowed values — to an enum reference or
    a custom list — and carry numeric or length constraints alongside.
    """

    def __init__(
        self,
        field: dict[str, Any],
        enums: dict[str, list[str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.field = json.loads(json.dumps(field))
        self.enums = enums
        kind = str(self.field.get("kind", "text"))
        self.setWindowTitle(f"{self.field.get('name', 'Field')} — {kind}")
        self.resize(640, 620)
        layout = QVBoxLayout(self)
        self.values_widget: AllowedValuesWidget | None = None
        self.minimum: QLineEdit | None = None

        if kind == "array":
            layout.addWidget(
                QLabel("Each element of the array is described below.")
            )
            form = QFormLayout()
            item = self.field.get("item") or {}
            self.item_kind = QComboBox()
            self.item_kind.addItems([k for k in PAYLOAD_KINDS if k != "array"])
            self.item_kind.setCurrentText(str(item.get("kind", "text")))
            form.addRow("Element kind", self.item_kind)
            self.item_clr = QLineEdit(str(item.get("clr_type", "string")))
            form.addRow("Element CLR type", self.item_clr)
            layout.addLayout(form)
            self.item_values = AllowedValuesWidget(
                item, enums, label="Element values"
            )
            layout.addWidget(self.item_values)
            self.nested = PayloadFieldTable()
            self.nested.set_fields(item.get("fields", []) or [], enums)
            layout.addWidget(
                QLabel("If elements are objects, define their fields here:")
            )
            layout.addWidget(self.nested, 1)
        elif kind == "object":
            layout.addWidget(QLabel("Fields of the nested object."))
            self.nested = PayloadFieldTable()
            self.nested.set_fields(self.field.get("fields", []) or [], enums)
            layout.addWidget(self.nested, 1)
        else:
            self.values_widget = AllowedValuesWidget(self.field, enums)
            layout.addWidget(self.values_widget, 1)

            constraints_box = QGroupBox("Validation constraints")
            constraints = self.field.get("constraints") or {}
            form = QFormLayout(constraints_box)
            self.minimum = QLineEdit(str(constraints.get("minimum", "")))
            self.minimum.setPlaceholderText("leave empty for none")
            form.addRow("Minimum", self.minimum)
            self.maximum = QLineEdit(str(constraints.get("maximum", "")))
            self.maximum.setPlaceholderText("leave empty for none")
            form.addRow("Maximum", self.maximum)
            self.max_length = QSpinBox()
            self.max_length.setRange(0, 1_000_000)
            self.max_length.setValue(int(constraints.get("max_length", 0) or 0))
            self.max_length.setSpecialValueText("none")
            form.addRow("Max length", self.max_length)
            layout.addWidget(constraints_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_field(self) -> dict[str, Any]:
        kind = str(self.field.get("kind", "text"))
        if kind == "array":
            item: dict[str, Any] = {
                "kind": self.item_kind.currentText(),
                "clr_type": self.item_clr.text().strip() or "string",
            }
            self.item_values.apply_to(item, keep_lov=item["kind"] == "enum")
            nested = self.nested.fields()
            if nested:
                item["fields"] = nested
                item["kind"] = "object"
            self.field["item"] = item
        elif kind == "object":
            self.field["fields"] = self.nested.fields()
        else:
            # An enum kind always declares a list; other kinds only when set.
            self.values_widget.apply_to(self.field, keep_lov=kind == "enum")
            constraints: dict[str, Any] = {}
            for key, widget in (("minimum", self.minimum), ("maximum", self.maximum)):
                raw = widget.text().strip()
                if raw:
                    try:
                        constraints[key] = json.loads(raw)
                    except json.JSONDecodeError:
                        constraints[key] = raw
            if self.max_length.value():
                constraints["max_length"] = self.max_length.value()
            if constraints:
                self.field["constraints"] = constraints
            else:
                self.field.pop("constraints", None)
        return self.field


class PayloadFieldTable(QWidget):
    """Edits the fields of a payload or form schema."""

    changed = pyqtSignal()

    COLUMNS = (
        "Name",
        "Display name",
        "Kind",
        "CLR type",
        "Required",
        "Nullable",
        "Details",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.enums: dict[str, list[str]] = {}
        self._details: dict[int, dict[str, Any]] = {}
        self._next_key = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(64)
        for index, width in enumerate((150, 140, 115, 120, 70, 70)):
            self.table.setColumnWidth(index, width)
        # The checkbox columns only need to clear their own heading, and the
        # Details column absorbs the slack so its button never needs a sideways
        # scroll to reach. It gets no explicit width: a width set before the
        # stretch mode is applied survives as the section size and pushes the
        # total past the viewport.
        for index in (4, 5):
            header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        self.table.itemChanged.connect(self._name_edited)
        self.empty_state = attach_table_empty_state(
            self.table,
            icon_name="fields",
            title="No payload fields",
            guidance="Add a field to describe the request body this endpoint accepts.",
        )
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        add = QPushButton("Add field")
        set_text_glyph(add, "add")
        add.clicked.connect(lambda: self.append(new_payload_field("NewField", "text")))
        row.addWidget(add)
        remove = QPushButton("Delete field")
        remove.setProperty("danger", True)
        remove.setIcon(icon("trash", "#E5484D"))
        remove.clicked.connect(self._remove_selected)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)

    def append(self, field: dict[str, Any]) -> None:
        index = self.table.rowCount()
        self.table.blockSignals(True)
        self.table.insertRow(index)
        key = self._next_key
        self._next_key += 1

        name_item = QTableWidgetItem(str(field.get("name", "")))
        name_item.setData(Qt.ItemDataRole.UserRole, key)
        self.table.setItem(index, 0, name_item)
        self.table.setItem(
            index, 1, QTableWidgetItem(str(field.get("display_name", "")))
        )

        kind = str(field.get("kind", "text"))
        combo = QComboBox()
        combo.addItems(PAYLOAD_KINDS)
        combo.setCurrentText(kind if kind in PAYLOAD_KINDS else "text")
        combo.currentTextChanged.connect(
            lambda value, k=key: self._kind_changed(k, value)
        )
        self.table.setCellWidget(index, 2, combo)
        fit_combo_column(self.table, 2, combo)

        self.table.setItem(
            index,
            3,
            QTableWidgetItem(str(field.get("clr_type", "") or default_clr_type(kind))),
        )
        self.table.setCellWidget(
            index, 4, _checkbox_cell(bool(field.get("required")), self.changed.emit)
        )
        self.table.setCellWidget(
            index, 5, _checkbox_cell(bool(field.get("nullable")), self.changed.emit)
        )

        holder, button = cell_button(
            tooltip="Edit this field's values and constraints"
        )
        button.clicked.connect(lambda _, k=key: self._edit_details(k))
        self.table.setCellWidget(index, 6, holder)

        self._details[key] = {
            piece: json.loads(json.dumps(field[piece]))
            for piece in ("item", "fields", "lov", "enum", "constraints")
            if piece in field
        }
        self.table.blockSignals(False)
        self._refresh_button(key)
        self.changed.emit()

    def _row_for_key(self, key: int) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                return row
        return -1

    def _refresh_button(self, key: int) -> None:
        row = self._row_for_key(key)
        if row < 0:
            return
        combo = self.table.cellWidget(row, 2)
        kind = combo.currentText() if isinstance(combo, QComboBox) else "text"
        detail = self._details.get(key, {})
        if kind == "array":
            item = detail.get("item") or {}
            caption = f"element: {item.get('kind', 'text')}"
        elif kind == "object":
            caption = f"{len(detail.get('fields') or [])} field(s)"
        else:
            parts = []
            if detail.get("enum") or detail.get("lov"):
                parts.append(values_caption(detail, self.enums))
            if detail.get("constraints"):
                parts.append("constrained")
            caption = ", ".join(parts) if parts else "Values, constraints..."
        button = button_in_cell(self.table, row, 6)
        if button is not None:
            button.setText(caption)

    def _name_edited(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            display = self.table.item(item.row(), 1)
            if display is not None and not display.text().strip():
                self.table.blockSignals(True)
                display.setText(display_name_for(item.text().strip()))
                self.table.blockSignals(False)
        self.changed.emit()

    def _kind_changed(self, key: int, kind: str) -> None:
        row = self._row_for_key(key)
        if row < 0:
            return
        detail = self._details.setdefault(key, {})
        if kind == "array" and "item" not in detail:
            detail["item"] = {"kind": "text", "clr_type": "string", "lov": []}
        if kind == "object" and "fields" not in detail:
            detail["fields"] = []
        if kind == "enum" and "lov" not in detail:
            detail["lov"] = []
        clr_item = self.table.item(row, 3)
        known = {default_clr_type(candidate) for candidate in PAYLOAD_KINDS} | {""}
        if clr_item is not None and clr_item.text().strip() in known:
            self.table.blockSignals(True)
            clr_item.setText(default_clr_type(kind))
            self.table.blockSignals(False)
        self._refresh_button(key)
        self.changed.emit()

    def _edit_details(self, key: int) -> None:
        row = self._row_for_key(key)
        if row < 0:
            return
        dialog = FieldDetailsDialog(self._field_at(row), self.enums, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            updated = dialog.result_field()
            self._details[key] = {
                piece: updated[piece]
                for piece in ("item", "fields", "lov", "enum", "constraints")
                if piece in updated
            }
            self._refresh_button(key)
            self.changed.emit()

    def _remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                self._details.pop(item.data(Qt.ItemDataRole.UserRole), None)
            self.table.removeRow(row)
        if rows:
            self.changed.emit()

    def _field_at(self, row: int) -> dict[str, Any]:
        name = _text(self.table, row, 0)
        item = self.table.item(row, 0)
        key = item.data(Qt.ItemDataRole.UserRole) if item else -1
        combo = self.table.cellWidget(row, 2)
        kind = combo.currentText() if isinstance(combo, QComboBox) else "text"
        field: dict[str, Any] = {
            "name": name,
            "clr_type": _text(self.table, row, 3) or default_clr_type(kind),
            "nullable": _cell_checked(self.table, row, 5),
            "required": _cell_checked(self.table, row, 4),
            "display_name": _text(self.table, row, 1) or display_name_for(name),
            "kind": kind,
        }
        detail = self._details.get(key, {})
        if kind == "array":
            field["item"] = detail.get("item") or {
                "kind": "text",
                "clr_type": "string",
                "lov": [],
            }
        elif kind == "object":
            field["fields"] = detail.get("fields") or []
        elif kind == "enum":
            field["lov"] = detail.get("lov") or []
        elif detail.get("lov"):
            field["lov"] = list(detail["lov"])
        if detail.get("enum") and kind not in ("array", "object"):
            field["enum"] = str(detail["enum"])
        if detail.get("constraints"):
            field["constraints"] = detail["constraints"]
        return field

    def set_fields(self, fields: list[dict[str, Any]], enums: dict[str, list[str]]) -> None:
        self.enums = enums
        self._details.clear()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        for field in fields:
            self.append(field)
        settle_table_rows(self.table)

    def showEvent(self, event) -> None:
        """Re-fits rows once the stylesheet has been applied.

        Row padding is only known after the table is polished, so a fit done
        while hidden measures the wrong deficit and leaves the cell buttons
        clipped.
        """
        super().showEvent(event)
        settle_table_rows(self.table)
        fit_last_column(self.table)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        fit_last_column(self.table)

    def fields(self) -> list[dict[str, Any]]:
        return [
            self._field_at(row)
            for row in range(self.table.rowCount())
            if _text(self.table, row, 0)
        ]


class EnumValueEditor(QWidget):
    """Edits the members of one catalog enum."""

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("One enum member per line, for example:\nDraft\nActive")
        self.editor.textChanged.connect(self.changed.emit)
        layout.addWidget(self.editor, 1)

    def set_values(self, values: list[str]) -> None:
        self.editor.blockSignals(True)
        self.editor.setPlainText("\n".join(str(value) for value in values))
        self.editor.blockSignals(False)

    def values(self) -> list[str]:
        return [
            line.strip()
            for line in self.editor.toPlainText().splitlines()
            if line.strip()
        ]
