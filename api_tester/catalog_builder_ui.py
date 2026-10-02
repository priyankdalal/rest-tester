"""The visual Catalog Builder.

Creates an API catalog by hand or by scanning project folders, one service at
a time, and writes the same ``schema_version 2`` document the tester loads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QEvent, QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
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
from .branding import APP_NAME, CATALOG_NAME_LIMIT
from .catalog_builder import (
    HTTP_METHODS,
    PARAMETER_SOURCES,
    SCHEMA_KINDS,
    SCHEMA_LABELS,
    CatalogDocument,
    CatalogValidationError,
    EndpointDraft,
    ServiceDraft,
    default_sample,
)
from .icons import app_icon, icon
from .scanners import FRAMEWORKS, ScanResult, detect_frameworks, scan_project
from .schema_editor import (
    EnumValueEditor,
    FilterFieldTable,
    PayloadFieldTable,
    ValueListWidget,
)
from .widgets import (
    Pane,
    ToolbarAction,
    attach_table_empty_state,
    build_page_toolbar,
    button_in_cell,
    cell_button,
    center_in_cell,
    form_caption,
    retint_text_glyphs,
    set_text_glyph,
    settle_table_rows,
    tint_toolbar,
)


NODE_KIND = Qt.ItemDataRole.UserRole
NODE_REF = Qt.ItemDataRole.UserRole + 1


class ScanWorker(QObject):
    """Runs one project scan off the UI thread.

    Scanning a large .NET solution walks thousands of files, so doing it
    inline would freeze the window for seconds at a time.
    """

    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, root: Path, service_name: str, framework_key: str) -> None:
        super().__init__()
        self.root = root
        self.service_name = service_name
        self.framework_key = framework_key

    def run(self) -> None:
        try:
            self.finished.emit(
                scan_project(self.root, self.service_name, self.framework_key)
            )
        except Exception as exc:  # surfaced in the dialog, never silently lost
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ScanServiceDialog(QDialog):
    """Asks for a service name, a folder, and the framework it contains."""

    def __init__(self, existing_names: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.existing_names = existing_names
        self.result_scan: ScanResult | None = None
        self.setWindowTitle("Add service from a project folder")
        self.resize(720, 520)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("MasterData")
        form.addRow("Service name", self.name)

        folder_row = QHBoxLayout()
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("Folder containing the API project")
        self.folder.textChanged.connect(self._folder_changed)
        folder_row.addWidget(self.folder, 1)
        browse = QPushButton("Browse...")
        set_text_glyph(browse, "folder")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(browse)
        form.addRow("Project folder", folder_row)

        framework_row = QHBoxLayout()
        self.framework = QComboBox()
        for framework in FRAMEWORKS:
            self.framework.addItem(
                f"{framework.label}", framework.key
            )
        self.framework.currentIndexChanged.connect(self._framework_changed)
        framework_row.addWidget(self.framework, 1)
        self.detect_button = QPushButton("Detect")
        self.detect_button.setToolTip("Suggest the project type from the folder")
        self.detect_button.clicked.connect(self.detect)
        framework_row.addWidget(self.detect_button)
        form.addRow("Project type", framework_row)

        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://localhost:7001")
        form.addRow("Default base URL", self.base_url)
        layout.addLayout(form)

        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setProperty("fieldCaption", True)
        layout.addWidget(self.description)

        self.scan_button = QPushButton("Scan folder")
        self.scan_button.setIcon(icon("search", "#0878F9"))
        self.scan_button.clicked.connect(self.scan)
        layout.addWidget(self.scan_button)

        self.preview = QTreeWidget()
        self.preview.setHeaderLabels(["Module / endpoint", "Method", "Path"])
        self.preview.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Interactive
        )
        self.preview.setColumnWidth(0, 240)
        self.preview.setColumnWidth(1, 70)
        attach_table_empty_state(
            self.preview,
            icon_name="search",
            title="Nothing scanned yet",
            guidance="Choose a folder and project type, then scan to preview endpoints.",
        )
        layout.addWidget(self.preview, 1)

        self.status = QLabel("Choose a folder and project type, then scan.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        self._ok_enabled(False)
        layout.addWidget(self.buttons)
        self._framework_changed()
        self._thread: QThread | None = None

    # ------------------------------------------------------------------ input

    def _ok_enabled(self, enabled: bool) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(enabled)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select the project folder")
        if folder:
            self.folder.setText(folder)

    def _folder_changed(self, value: str) -> None:
        self.result_scan = None
        self._ok_enabled(False)
        path = Path(value)
        if not self.name.text().strip() and path.name:
            self.name.setText(path.name)
        if value and path.is_dir():
            self.detect()

    def _framework_changed(self) -> None:
        key = self.framework.currentData()
        for framework in FRAMEWORKS:
            if framework.key == key:
                self.description.setText(framework.description)
                return

    def detect(self) -> None:
        root = Path(self.folder.text().strip())
        if not root.is_dir():
            self.status.setText("That folder does not exist.")
            return
        matches = detect_frameworks(root)
        if not matches:
            self.status.setText(
                "No known framework was detected. Choose the project type manually."
            )
            return
        best, score = matches[0]
        index = self.framework.findData(best.key)
        if index >= 0:
            self.framework.setCurrentIndex(index)
        others = ", ".join(item.label for item, _ in matches[1:3])
        self.status.setText(
            f"Detected {best.label} (confidence {score})."
            + (f" Other candidates: {others}." if others else "")
        )

    # ------------------------------------------------------------------- scan

    def scan(self) -> None:
        name = self.name.text().strip()
        root = Path(self.folder.text().strip())
        if not name:
            self.status.setText("Enter a service name first.")
            return
        if name in self.existing_names:
            self.status.setText(f"'{name}' is already in this catalog.")
            return
        if not root.is_dir():
            self.status.setText("Choose an existing project folder first.")
            return

        self.scan_button.setEnabled(False)
        self.status.setText(f"Scanning {root}...")
        self.preview.clear()

        thread = QThread(self)
        worker = ScanWorker(root, name, str(self.framework.currentData()))
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._scan_finished)
        worker.failed.connect(self._scan_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _scan_finished(self, result: ScanResult) -> None:
        self.scan_button.setEnabled(True)
        self.result_scan = result
        self.preview.clear()
        modules: dict[str, QTreeWidgetItem] = {}
        for endpoint in result.sorted_endpoints():
            parent = modules.get(endpoint.controller)
            if parent is None:
                parent = QTreeWidgetItem(self.preview, [endpoint.controller, "", ""])
                modules[endpoint.controller] = parent
            QTreeWidgetItem(
                parent, [endpoint.action, endpoint.method, endpoint.path]
            )
        self.preview.expandAll()
        message = (
            f"Found {result.count} endpoints in {len(modules)} modules."
            if result.count
            else "No endpoints were found."
        )
        if result.warnings:
            message += "  " + "  ".join(result.warnings)
        self.status.setText(message)
        self._ok_enabled(result.count > 0)

    def _scan_failed(self, message: str) -> None:
        self.scan_button.setEnabled(True)
        self.status.setText(f"Scan failed: {message}")
        self._ok_enabled(False)

    def _accept(self) -> None:
        if self.result_scan is None:
            self.status.setText("Scan the folder before adding the service.")
            return
        self.accept()


class ParameterValuesDialog(QDialog):
    """Chooses the values a parameter accepts: any, an enum, or a custom list."""

    ANY, ENUM, CUSTOM = "Any value", "From an enum", "Custom list"

    def __init__(
        self,
        parameter: dict[str, Any],
        enums: dict[str, list[str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Allowed values for {parameter.get('name') or 'parameter'}")
        self.resize(460, 480)
        self.enums = enums
        layout = QVBoxLayout(self)

        self.mode = QComboBox()
        self.mode.addItems([self.ANY, self.ENUM, self.CUSTOM])
        self.mode.currentTextChanged.connect(self._mode_changed)
        form = QFormLayout()
        form.addRow("Values", self.mode)
        self.enum_combo = QComboBox()
        self.enum_combo.addItems(sorted(enums))
        form.addRow("Enum", self.enum_combo)
        layout.addLayout(form)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

        self.custom = ValueListWidget(
            [str(value) for value in parameter.get("values") or []], enums
        )
        layout.addWidget(self.custom, 1)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        enum_name = parameter.get("enum")
        if enum_name:
            self.mode.setCurrentText(self.ENUM)
            if enum_name in enums:
                self.enum_combo.setCurrentText(enum_name)
            else:
                self.enum_combo.insertItem(0, str(enum_name))
                self.enum_combo.setCurrentIndex(0)
        elif parameter.get("values"):
            self.mode.setCurrentText(self.CUSTOM)
        self._mode_changed(self.mode.currentText())

    def _mode_changed(self, mode: str) -> None:
        self.enum_combo.setEnabled(mode == self.ENUM)
        self.custom.setEnabled(mode == self.CUSTOM)
        self.hint.setText(
            {
                self.ANY: "The request builder shows a free text box.",
                self.ENUM: (
                    "The request builder offers this enum's members. The reference "
                    "stays live, so editing the enum updates the parameter."
                ),
                self.CUSTOM: (
                    "The request builder offers exactly these values. Use this for "
                    "values that are not a catalog enum, such as a status code list."
                ),
            }[mode]
        )

    def result_values(self) -> dict[str, Any]:
        """Returns the keys to merge into the parameter, ``None`` meaning remove."""
        mode = self.mode.currentText()
        if mode == self.ENUM and self.enum_combo.currentText().strip():
            return {"enum": self.enum_combo.currentText().strip(), "values": None}
        if mode == self.CUSTOM and self.custom.values():
            return {"enum": None, "values": self.custom.values()}
        return {"enum": None, "values": None}


class ParameterTable(QWidget):
    """Edits the parameter list of one endpoint."""

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.enums: dict[str, list[str]] = {}
        # Value lists are keyed by a row token, not the row index, so deleting
        # a row cannot shift one parameter's values onto its neighbour.
        self._values: dict[int, dict[str, Any]] = {}
        self._next_key = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Source", "Type", "Required", "Values", "Sample"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 150)
        self.table.itemChanged.connect(lambda _: self.changed.emit())
        self.empty_state = attach_table_empty_state(
            self.table,
            icon_name="fields",
            title="No parameters",
            guidance="Add a parameter to describe what this endpoint accepts.",
        )
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        add = QPushButton("Add parameter")
        set_text_glyph(add, "add")
        add.clicked.connect(lambda: self._append({"name": "", "source": "query"}))
        row.addWidget(add)
        remove = QPushButton("Delete parameter")
        remove.setProperty("danger", True)
        remove.setIcon(icon("trash", "#E5484D"))
        remove.clicked.connect(self._remove_selected)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The first layout pass is the earliest point the row padding can be
        # measured, so the fit has to be retried here rather than only when
        # the parameters are set.
        settle_table_rows(self.table)

    def _append(self, parameter: dict[str, Any]) -> None:
        index = self.table.rowCount()
        key = self._next_key
        self._next_key += 1
        self._values[key] = {
            "enum": parameter.get("enum") or None,
            "values": list(parameter.get("values") or []) or None,
        }
        self.table.blockSignals(True)
        self.table.insertRow(index)
        name_item = QTableWidgetItem(str(parameter.get("name", "")))
        name_item.setData(Qt.ItemDataRole.UserRole, key)
        self.table.setItem(index, 0, name_item)

        source = QComboBox()
        source.addItems(PARAMETER_SOURCES)
        source.setCurrentText(str(parameter.get("source", "query")))
        source.currentTextChanged.connect(lambda _: self.changed.emit())
        self.table.setCellWidget(index, 1, source)

        self.table.setItem(
            index, 2, QTableWidgetItem(str(parameter.get("type", "string")))
        )
        required = QCheckBox()
        required.setChecked(bool(parameter.get("required", False)))
        required.toggled.connect(lambda _: self.changed.emit())
        self.table.setCellWidget(index, 3, center_in_cell(required))

        holder, button = cell_button(
            tooltip="Restrict this parameter to an enum or a list of values"
        )
        button.clicked.connect(lambda _=False, k=key: self._edit_values(k))
        self.table.setCellWidget(index, 4, holder)
        self._refresh_values_button(key)

        sample = parameter.get("sample", "")
        self.table.setItem(
            index, 5, QTableWidgetItem("" if sample == "" else json.dumps(sample))
        )
        self.table.blockSignals(False)
        self.changed.emit()

    def _row_for_key(self, key: int) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                return row
        return -1

    def _refresh_values_button(self, key: int) -> None:
        row = self._row_for_key(key)
        button = button_in_cell(self.table, row, 4) if row >= 0 else None
        if button is None:
            return
        chosen = self._values.get(key, {})
        if chosen.get("enum"):
            name = str(chosen["enum"])
            missing = name not in self.enums
            button.setText(f"{name} (missing)" if missing else name)
        elif chosen.get("values"):
            button.setText(f"{len(chosen['values'])} values...")
        else:
            button.setText("Any value...")

    def _edit_values(self, key: int) -> None:
        row = self._row_for_key(key)
        if row < 0:
            return
        name_item = self.table.item(row, 0)
        parameter = dict(self._values.get(key, {}))
        parameter["name"] = name_item.text() if name_item else ""
        dialog = ParameterValuesDialog(parameter, self.enums, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._values[key] = dialog.result_values()
        self._refresh_values_button(key)
        self.changed.emit()

    def set_enums(self, enums: dict[str, list[str]]) -> None:
        self.enums = enums
        for key in list(self._values):
            self._refresh_values_button(key)

    def _remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                self._values.pop(item.data(Qt.ItemDataRole.UserRole), None)
            self.table.removeRow(row)
        if rows:
            self.changed.emit()

    def set_parameters(self, parameters: list[dict[str, Any]]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        self._values.clear()
        for parameter in parameters:
            self._append(parameter)
        settle_table_rows(self.table)

    def parameters(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            name = name_item.text().strip() if name_item else ""
            if not name:
                continue
            source_widget = self.table.cellWidget(row, 1)
            source = source_widget.currentText() if source_widget else "query"
            type_item = self.table.item(row, 2)
            type_name = (type_item.text().strip() if type_item else "") or "string"
            holder = self.table.cellWidget(row, 3)
            checkbox = holder.findChild(QCheckBox) if holder else None
            sample_item = self.table.item(row, 5)
            raw_sample = sample_item.text().strip() if sample_item else ""
            if raw_sample:
                try:
                    sample = json.loads(raw_sample)
                except json.JSONDecodeError:
                    sample = raw_sample
            else:
                sample = default_sample(type_name, name)
            parameter = {
                "name": name,
                "source": source,
                "type": type_name,
                "required": bool(checkbox.isChecked()) if checkbox else False,
                "sample": sample,
            }
            chosen = self._values.get(name_item.data(Qt.ItemDataRole.UserRole), {})
            if chosen.get("enum"):
                parameter["enum"] = str(chosen["enum"])
            elif chosen.get("values"):
                parameter["values"] = [str(item) for item in chosen["values"]]
            result.append(parameter)
        return result


class CatalogBuilderWindow(QMainWindow):
    """The Catalog Builder shell: a hierarchy on the left, an editor on the right."""

    catalog_saved = pyqtSignal(str)

    def __init__(self, path: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.document = CatalogDocument()
        self.dirty = False
        self._loading = False
        self._active_schema_kind = "filter"
        self._active_schema_name = ""
        self._history: list[tuple[str, Any]] = [("catalog", None)]
        self._history_index = 0
        self._navigating = False
        self._palette_version = theme.PALETTE_VERSION
        self.setWindowTitle("Rest Tester Catalog Builder")
        self.setWindowIcon(app_icon())
        self.resize(1320, 840)
        self.setMinimumSize(1000, 640)
        self._build_ui()
        if path is not None and Path(path).is_file():
            self.open_catalog(Path(path))
        else:
            self.new_catalog(confirm=False)

    # ------------------------------------------------------------------- shell

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_toolbar())

        body = QWidget()
        body.setProperty("transparentPane", True)
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(10, 10, 10, 8)
        splitter = QSplitter()
        splitter.addWidget(self._build_tree_pane())
        splitter.addWidget(self._build_editor_pane())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([360, 960])
        body_layout.addWidget(splitter, 1)
        outer.addWidget(body, 1)

        outer.addWidget(self._build_activity_strip())
        self.setCentralWidget(central)

    def _build_toolbar(self) -> QWidget:
        """The top action row: navigation, then File / Build / Check groups.

        Deliberately a plain widget rather than a ``QToolBar``. The toolbar
        rendered bare captions that no stylesheet in this app targets, so it
        never matched the button styling used everywhere else.
        """
        holder = QWidget()
        holder.setObjectName("builderToolbar")
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(holder)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(6)

        # The actions stay real QActions so their shortcuts keep working even
        # though the visible control is now a button.
        self.back_action = QAction(icon("chevron-left"), "Back", self)
        self.back_action.setShortcut("Alt+Left")
        self.back_action.triggered.connect(self.go_back)
        self.forward_action = QAction(icon("chevron-right"), "Forward", self)
        self.forward_action.setToolTip("Go forward again (Alt+Right)")
        self.forward_action.setShortcut("Alt+Right")
        self.forward_action.triggered.connect(self.go_forward)
        self.addAction(self.back_action)
        self.addAction(self.forward_action)

        self._nav_buttons: dict[str, QToolButton] = {}
        for key, action, glyph in (
            ("back", self.back_action, "chevron-left"),
            ("forward", self.forward_action, "chevron-right"),
        ):
            button = QToolButton()
            button.setObjectName("headerIconButton")
            button.setFixedSize(30, 30)
            button.setAccessibleName(action.text())
            button.setDefaultAction(action)
            button.setIcon(icon(glyph, theme.TEXT_MUTED, 16))
            row.addWidget(button)
            self._nav_buttons[key] = button
        row.addSpacing(10)

        self._toolbar_actions = (
            ToolbarAction("New Catalog", "new", self.new_catalog, group=0,
                          tooltip="Start an empty catalog"),
            ToolbarAction("Open", "open", self.open_catalog, group=0,
                          tooltip="Open an existing catalog"),
            ToolbarAction("Save", "save", self.save, accent=True, group=0,
                          tooltip="Save to the current file"),
            ToolbarAction("Save As", "save-as", self.save_as, group=0,
                          tooltip="Save to a new file"),
            ToolbarAction("Scan Service", "search", self.add_scanned_service, group=1,
                          tooltip="Add a service by scanning a folder"),
            ToolbarAction("Manual Service", "add", self.add_manual_service, group=1,
                          tooltip="Add an empty service"),
            ToolbarAction("New Schema", "fields", self.new_schema_of_kind, group=1,
                          tooltip="Create a filter, payload, form schema, or enum"),
            ToolbarAction("Validate", "verify", self.validate, group=2,
                          tooltip="Check the catalog before saving"),
        )
        toolbar, self._toolbar_buttons = build_page_toolbar(self._toolbar_actions)
        tint_toolbar(self._toolbar_actions, self._toolbar_buttons)
        row.addLayout(toolbar, 1)
        return holder

    def _build_tree_pane(self) -> Pane:
        pane = Pane("Catalog")
        self.tree_pane = pane

        self.tree_search = QLineEdit()
        self.tree_search.setPlaceholderText("Search services, modules, endpoints")
        self.tree_search.setClearButtonEnabled(True)
        self.tree_search.addAction(
            icon("search", theme.TEXT_MUTED, 14),
            QLineEdit.ActionPosition.LeadingPosition,
        )
        self.tree_search.textChanged.connect(self._filter_tree)
        pane.body.addWidget(self.tree_search)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Catalog", "Method"])
        header = self.tree.header()
        # Fixed widths wider than the pane forced a permanent horizontal
        # scrollbar and clipped the second header, so size to the space that
        # actually exists instead.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        self.tree.setMinimumWidth(260)
        self.tree.currentItemChanged.connect(lambda *_: self._selection_changed())
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.ActionsContextMenu)
        attach_table_empty_state(
            self.tree,
            icon_name="api-explorer",
            title="Catalog is empty",
            guidance="Scan a project or add a service to start building the catalog.",
        )
        pane.body.addWidget(self.tree, 1)
        return pane

    def _build_editor_pane(self) -> Pane:
        pane = Pane("Catalog")
        self.editor_pane = pane

        self.back_label = QPushButton()
        self.back_label.setFlat(True)
        self.back_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_label.setToolTip("Return to the previous item (Alt+Left)")
        self.back_label.clicked.connect(self.go_back)
        self.back_label.setVisible(False)
        crumb_row = QHBoxLayout()
        crumb_row.setContentsMargins(0, 0, 0, 0)
        crumb_row.addWidget(self.back_label)
        crumb_row.addStretch()
        pane.body.addLayout(crumb_row)

        self.editor = QStackedWidget()
        self.editor.addWidget(self._build_catalog_page())
        self.editor.addWidget(self._build_service_page())
        self.editor.addWidget(self._build_module_page())
        self.editor.addWidget(self._build_endpoint_page())
        self.editor.addWidget(self._build_schema_group_page())
        self.editor.addWidget(self._build_filter_schema_page())
        self.editor.addWidget(self._build_payload_schema_page())
        self.editor.addWidget(self._build_enum_page())
        pane.body.addWidget(self.editor, 1)
        return pane

    def _build_activity_strip(self) -> QWidget:
        """A one-line status with the scan log folded away behind a toggle.

        The log used to sit open at all times showing exactly the same text as
        the status bar, so the window spent 110px saying everything twice.
        """
        holder = QWidget()
        holder.setObjectName("builderStatus")
        holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(140)
        self.log.setPlaceholderText("Scan results and validation messages appear here.")
        self.log.setVisible(False)
        column.addWidget(self.log)

        row = QHBoxLayout()
        row.setContentsMargins(12, 7, 12, 7)
        row.setSpacing(8)
        self.status_icon = QLabel()
        self.status_icon.setPixmap(icon("info-circle", theme.TEXT_MUTED, 15).pixmap(15, 15))
        row.addWidget(self.status_icon)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("collectionMeta")
        row.addWidget(self.status_label, 1)
        self.activity_button = QPushButton("Activity")
        self.activity_button.setToolTip("Show the scan and validation log")
        self.activity_button.setCheckable(True)
        self.activity_button.toggled.connect(self._set_activity_visible)
        row.addWidget(self.activity_button)
        column.addLayout(row)
        self._tint_activity_button()
        return holder

    def _tint_activity_button(self) -> None:
        expanded = self.activity_button.isChecked()
        self.activity_button.setIcon(
            icon("move-down" if expanded else "move-up", theme.TEXT, 14)
        )

    def _set_activity_visible(self, visible: bool) -> None:
        self.log.setVisible(visible)
        self.activity_button.setToolTip(
            "Hide the scan and validation log"
            if visible
            else "Show the scan and validation log"
        )
        self._tint_activity_button()

    def _filter_tree(self, text: str) -> None:
        """Hides tree rows that do not match, keeping ancestors of matches.

        Filtering never changes the selection: hiding the current item would
        silently swap the editor page out from under an edit in progress.
        """
        needle = text.strip().lower()

        def visit(item: QTreeWidgetItem) -> bool:
            matched = not needle or needle in item.text(0).lower() or needle in item.text(1).lower()
            child_matched = False
            for index in range(item.childCount()):
                if visit(item.child(index)):
                    child_matched = True
            item.setHidden(not (matched or child_matched))
            if needle and child_matched:
                item.setExpanded(True)
            return matched or child_matched

        for index in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(index))

    def refresh_theme(self) -> None:
        """Rebuilds every rasterised glyph for the active palette."""
        tint_toolbar(self._toolbar_actions, self._toolbar_buttons)
        for key, glyph in (("back", "chevron-left"), ("forward", "chevron-right")):
            button = self._nav_buttons.get(key)
            if button is not None:
                button.setIcon(icon(glyph, theme.TEXT_MUTED, 16))
        self.tree_search.setClearButtonEnabled(True)
        self.status_icon.setPixmap(icon("info-circle", theme.TEXT_MUTED, 15).pixmap(15, 15))
        self._tint_activity_button()
        retint_text_glyphs(self)

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            if self._palette_version != theme.PALETTE_VERSION:
                self._palette_version = theme.PALETTE_VERSION
                self.refresh_theme()
        super().changeEvent(event)

    def _note(self, message: str) -> None:
        self.log.appendPlainText(message)
        self.status_label.setText(message)
        self.status_label.setToolTip(message)

    # ------------------------------------------------------------------- pages

    def _build_catalog_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Catalog")
        heading.setProperty("workspaceTitle", True)
        heading.setVisible(False)
        layout.addWidget(heading)
        form = QFormLayout()
        self.catalog_name = QLineEdit()
        self.catalog_name.setMaxLength(CATALOG_NAME_LIMIT)
        self.catalog_name.setPlaceholderText(
            f"Shown beside the app icon; defaults to \"{APP_NAME}\""
        )
        self.catalog_name.setToolTip(
            "The name the tester shows in its header for this catalog. "
            f"At most {CATALOG_NAME_LIMIT} characters."
        )
        self.catalog_name.textChanged.connect(self._catalog_name_edited)
        form.addRow("Name", self.catalog_name)
        self.catalog_root = QLineEdit()
        self.catalog_root.setPlaceholderText("Workspace root recorded in the catalog")
        self.catalog_root.textChanged.connect(self._catalog_edited)
        form.addRow("Workspace root", self.catalog_root)
        self.catalog_file = QLabel("(not saved yet)")
        form.addRow("File", self.catalog_file)
        self.catalog_summary = QLabel()
        self.catalog_summary.setWordWrap(True)
        form.addRow("Contents", self.catalog_summary)
        layout.addLayout(form)
        hint = QLabel(
            "Add one service per API project. Use 'Scan Service...' to read a "
            "folder, or 'Manual Service' to build endpoints by hand. Modules "
            "are the controller groupings shown in the tester."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch()
        return page

    def _build_service_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Service")
        heading.setProperty("workspaceTitle", True)
        heading.setVisible(False)
        layout.addWidget(heading)
        form = QFormLayout()
        self.service_name = QLineEdit()
        form.addRow("Name", self.service_name)
        self.service_repository = QLineEdit()
        self.service_repository.setPlaceholderText("Source folder or repository")
        form.addRow("Repository", self.service_repository)
        self.service_base_url = QLineEdit()
        self.service_base_url.setPlaceholderText("https://localhost:7001")
        form.addRow("Default base URL", self.service_base_url)
        self.service_framework = QLabel()
        form.addRow("Scanned as", self.service_framework)
        self.service_builders = QCheckBox("Enable Filter and Sort builders")
        self.service_builders.setToolTip(
            "Only for services that use the structured filter grammar "
            "(Name__op:=value joined by ; or |, sort as Field,Other-).\n"
            "When off, the tester shows Filter and Sort as plain query values "
            "and the Data Runner offers no filter-field mappings."
        )
        form.addRow("Query builders", self.service_builders)
        self.service_summary = QLabel()
        form.addRow("Contents", self.service_summary)
        layout.addLayout(form)

        row = QHBoxLayout()
        apply_button = QPushButton("Apply changes")
        set_text_glyph(apply_button, "save")
        apply_button.clicked.connect(self._apply_service)
        row.addWidget(apply_button)
        rescan = QPushButton("Rescan folder")
        set_text_glyph(rescan, "renew")
        rescan.setToolTip("Re-read the repository folder and replace the endpoints")
        rescan.clicked.connect(self._rescan_service)
        row.addWidget(rescan)
        add_module = QPushButton("Add module")
        set_text_glyph(add_module, "add")
        add_module.clicked.connect(self._add_module)
        row.addWidget(add_module)
        remove = QPushButton("Delete service")
        remove.setProperty("danger", True)
        remove.setIcon(icon("trash", "#E5484D"))
        remove.clicked.connect(self._delete_service)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        return page

    def _build_module_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Module")
        heading.setProperty("workspaceTitle", True)
        heading.setVisible(False)
        layout.addWidget(heading)
        form = QFormLayout()
        self.module_name = QLineEdit()
        form.addRow("Name", self.module_name)
        self.module_summary = QLabel()
        form.addRow("Contents", self.module_summary)
        layout.addLayout(form)
        row = QHBoxLayout()
        rename = QPushButton("Rename module")
        set_text_glyph(rename, "edit")
        rename.clicked.connect(self._rename_module)
        row.addWidget(rename)
        add = QPushButton("Add endpoint")
        set_text_glyph(add, "add")
        add.clicked.connect(self._add_endpoint)
        row.addWidget(add)
        remove = QPushButton("Delete module")
        remove.setProperty("danger", True)
        remove.setIcon(icon("trash", "#E5484D"))
        remove.clicked.connect(self._delete_module)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()
        return page

    def _build_endpoint_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Endpoint")
        heading.setProperty("workspaceTitle", True)
        heading.setVisible(False)
        layout.addWidget(heading)

        form = QFormLayout()
        route = QHBoxLayout()
        self.endpoint_method = QComboBox()
        self.endpoint_method.addItems(HTTP_METHODS)
        route.addWidget(self.endpoint_method)
        self.endpoint_path = QLineEdit()
        self.endpoint_path.setPlaceholderText("/Brand/{id}")
        route.addWidget(self.endpoint_path, 1)
        form.addRow("Route", route)
        self.endpoint_action = QLineEdit()
        form.addRow("Action", self.endpoint_action)
        self.endpoint_module = QLineEdit()
        form.addRow("Module", self.endpoint_module)
        self.endpoint_status = QLineEdit()
        self.endpoint_status.setPlaceholderText("200-299 or 200,204")
        form.addRow("Expected status", self.endpoint_status)
        layout.addLayout(form)

        tabs = QTabWidget()
        self.parameter_table = ParameterTable()
        tabs.addTab(self.parameter_table, "Parameters")

        payload_page = QWidget()
        payload_layout = QVBoxLayout(payload_page)
        self.endpoint_payload = QPlainTextEdit()
        self.endpoint_payload.setPlaceholderText(
            'Example request body as JSON, or leave empty for no body.'
        )
        payload_layout.addWidget(self.endpoint_payload, 1)
        schema_form = QFormLayout()
        self.endpoint_payload_type = QComboBox()
        self.endpoint_payload_type.setEditable(True)
        self.endpoint_payload_type.setToolTip(
            "The declared CLR type of the body, for documentation. Pick a "
            "payload schema name or type any wrapper such as "
            "JsonPatchDocument<Brand>."
        )
        schema_form.addRow("Payload type", self.endpoint_payload_type)
        self.endpoint_payload_schema = QComboBox()
        schema_form.addRow(
            "Payload schema",
            self._schema_picker("payload", self.endpoint_payload_schema),
        )
        self.endpoint_form_schema = QComboBox()
        schema_form.addRow(
            "Form schema", self._schema_picker("form", self.endpoint_form_schema)
        )
        self.endpoint_response_schema = QComboBox()
        schema_form.addRow(
            "Response schema",
            self._schema_picker(
                "response",
                self.endpoint_response_schema,
                autodetect=self._autodetect_response_schema_for_active_endpoint,
            ),
        )
        self.endpoint_filter_entity = QComboBox()
        schema_form.addRow(
            "Filter entity",
            self._schema_picker("filter", self.endpoint_filter_entity),
        )
        payload_layout.addLayout(schema_form)
        payload_layout.addWidget(
            form_caption(
                "Schemas are shared across the catalog. Create edits one in "
                "place; Open jumps to its editor under Schemas."
            )
        )
        tabs.addTab(payload_page, "Body and schemas")

        source_page = QWidget()
        source_form = QFormLayout(source_page)
        self.endpoint_source_file = QLineEdit()
        source_form.addRow("Source file", self.endpoint_source_file)
        self.endpoint_identity = QLabel()
        source_form.addRow("Endpoint id", self.endpoint_identity)
        tabs.addTab(source_page, "Source")
        layout.addWidget(tabs, 1)

        row = QHBoxLayout()
        apply_button = QPushButton("Apply changes")
        set_text_glyph(apply_button, "save")
        apply_button.clicked.connect(self._apply_endpoint)
        row.addWidget(apply_button)
        duplicate = QPushButton("Duplicate")
        set_text_glyph(duplicate, "duplicate")
        duplicate.clicked.connect(self._duplicate_endpoint)
        row.addWidget(duplicate)
        remove = QPushButton("Delete endpoint")
        remove.setProperty("danger", True)
        remove.setIcon(icon("trash", "#E5484D"))
        remove.clicked.connect(self._delete_endpoint)
        row.addWidget(remove)
        row.addStretch()
        layout.addLayout(row)
        return page

    def _schema_picker(
        self,
        kind: str,
        combo: QComboBox,
        autodetect=None,
    ) -> QWidget:
        """A schema dropdown with Create / Open / Clear beside it.

        The combo is deliberately not editable: a typed name would become a
        dangling reference the tester silently ignores.
        """
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        combo.setEditable(False)
        layout.addWidget(combo, 1)
        create = QPushButton("Create...")
        set_text_glyph(create, "new")
        create.setToolTip(f"Create a new {SCHEMA_LABELS[kind].lower()} and use it here")
        create.clicked.connect(lambda: self._create_schema_for_endpoint(kind, combo))
        layout.addWidget(create)
        open_button = QPushButton("Open")
        set_text_glyph(open_button, "open")
        open_button.setToolTip("Edit the selected schema under Schemas")
        open_button.clicked.connect(lambda: self._open_selected_schema(kind, combo))
        layout.addWidget(open_button)
        if autodetect is not None:
            detect = QPushButton("Auto-detect from source")
            set_text_glyph(detect, "wand")
            detect.setToolTip(
                "Rescan the source project and reuse any discovered response schema."
            )
            detect.clicked.connect(autodetect)
            layout.addWidget(detect)
        clear = QPushButton("Clear")
        set_text_glyph(clear, "clear")
        clear.clicked.connect(lambda: combo.setCurrentIndex(0))
        layout.addWidget(clear)
        return holder

    def _create_schema_for_endpoint(self, kind: str, combo: QComboBox) -> None:
        """Creates a schema and points the endpoint being edited at it."""
        suggestion = ""
        endpoint = getattr(self, "_active_endpoint", None)
        if endpoint is not None:
            suffix = {
                "filter": "",
                "payload": "Payload",
                "form": "Request",
                "response": "Response",
            }[kind]
            suggestion = f"{endpoint.controller}{suffix}"
        name = self._prompt_schema_name(
            kind, f"New {SCHEMA_LABELS[kind].lower()}", suggestion
        )
        if not name:
            return
        try:
            self.document.add_schema(kind, name)
        except CatalogValidationError as exc:
            QMessageBox.warning(self, "Cannot create", str(exc))
            return
        self._fill_combo(combo, self.document.schema_store(kind), name)
        self._mark_dirty()
        self._note(
            f"Created {SCHEMA_LABELS[kind].lower()} {name}. Apply the endpoint, "
            f"then open it under Schemas to add fields."
        )

    def _open_selected_schema(self, kind: str, combo: QComboBox) -> None:
        name = combo.currentText().strip()
        if not name:
            QMessageBox.information(
                self,
                "No schema selected",
                f"Select or create a {SCHEMA_LABELS[kind].lower()} first.",
            )
            return
        self._apply_endpoint()
        self._select_schema_item(kind, name)

    def _autodetect_response_schema_for_active_endpoint(self) -> None:
        endpoint = getattr(self, "_active_endpoint", None)
        if endpoint is None:
            return
        try:
            name = self.document.autodetect_response_schema(endpoint.id)
        except CatalogValidationError as exc:
            QMessageBox.warning(self, "Cannot auto-detect response schema", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Response schema detection failed",
                f"{type(exc).__name__}: {exc}",
            )
            return
        self._fill_combo(
            self.endpoint_response_schema,
            self.document.response_schemas,
            endpoint.response_schema,
        )
        if not name:
            message = (
                "Static analysis could not determine a response type for this endpoint. "
                "Create or edit a response schema manually."
            )
            QMessageBox.information(self, "No response schema found", message)
            self._note(message)
            return
        self._mark_dirty()
        self._note(
            f"Auto-detected response schema {name} for {endpoint.label}."
        )

    # ------------------------------------------------------------ schema pages

    def _schema_action_row(self, kind_getter) -> QHBoxLayout:
        """The New / Duplicate / Rename / Delete row shared by schema pages."""
        row = QHBoxLayout()
        for caption, glyph, handler in (
            ("New", "new", lambda: self.new_schema(kind_getter())),
            ("Duplicate", "duplicate", lambda: self.duplicate_schema(kind_getter())),
            ("Rename", "edit", lambda: self.rename_schema(kind_getter())),
        ):
            button = QPushButton(caption)
            set_text_glyph(button, glyph)
            button.clicked.connect(lambda _, fn=handler: fn())
            row.addWidget(button)
        delete = QPushButton("Delete")
        delete.setProperty("danger", True)
        delete.setIcon(icon("trash", "#E5484D"))
        delete.clicked.connect(lambda: self.delete_schema(kind_getter()))
        row.addWidget(delete)
        return row

    def _build_schema_group_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.schema_group_heading = QLabel("Schemas")
        self.schema_group_heading.setProperty("workspaceTitle", True)
        self.schema_group_heading.setVisible(False)
        layout.addWidget(self.schema_group_heading)
        self.schema_group_hint = QLabel()
        self.schema_group_hint.setWordWrap(True)
        layout.addWidget(self.schema_group_hint)
        self.schema_group_list = QPlainTextEdit()
        self.schema_group_list.setReadOnly(True)
        layout.addWidget(self.schema_group_list, 1)
        row = self._schema_action_row(lambda: self._active_schema_kind)
        row.addStretch()
        layout.addLayout(row)
        return page

    def _build_filter_schema_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Filter entity")
        heading.setProperty("workspaceTitle", True)
        heading.setVisible(False)
        layout.addWidget(heading)
        form = QFormLayout()
        self.filter_schema_name = QLabel()
        form.addRow("Name", self.filter_schema_name)
        self.filter_schema_usage = QLabel()
        self.filter_schema_usage.setWordWrap(True)
        form.addRow("Used by", self.filter_schema_usage)
        layout.addLayout(form)
        layout.addWidget(
            QLabel(
                "These fields drive the query builder: every filterable and "
                "sortable property, with the operators the platform allows."
            )
        )
        self.filter_fields = FilterFieldTable()
        layout.addWidget(self.filter_fields, 1)
        row = QHBoxLayout()
        apply_button = QPushButton("Apply changes")
        set_text_glyph(apply_button, "save")
        apply_button.clicked.connect(self._apply_filter_schema)
        row.addWidget(apply_button)
        row.addLayout(self._schema_action_row(lambda: "filter"))
        row.addStretch()
        layout.addLayout(row)
        return page

    def _build_payload_schema_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.payload_schema_heading = QLabel("Payload schema")
        self.payload_schema_heading.setProperty("workspaceTitle", True)
        self.payload_schema_heading.setVisible(False)
        layout.addWidget(self.payload_schema_heading)
        form = QFormLayout()
        self.payload_schema_name = QLabel()
        form.addRow("Name", self.payload_schema_name)
        self.payload_schema_clr = QLineEdit()
        self.payload_schema_clr.setPlaceholderText("CLR type recorded for this schema")
        form.addRow("CLR type", self.payload_schema_clr)
        self.payload_schema_usage = QLabel()
        self.payload_schema_usage.setWordWrap(True)
        form.addRow("Used by", self.payload_schema_usage)
        layout.addLayout(form)
        layout.addWidget(
            QLabel(
                "Each field becomes an input in the request form. Use the "
                "Details column for array elements, nested objects, enum "
                "values, and constraints."
            )
        )
        self.payload_fields = PayloadFieldTable()
        layout.addWidget(self.payload_fields, 1)
        row = QHBoxLayout()
        apply_button = QPushButton("Apply changes")
        set_text_glyph(apply_button, "save")
        apply_button.clicked.connect(self._apply_payload_schema)
        row.addWidget(apply_button)
        row.addLayout(self._schema_action_row(lambda: self._active_schema_kind))
        row.addStretch()
        layout.addLayout(row)
        return page

    def _build_enum_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        heading = QLabel("Enum")
        heading.setProperty("workspaceTitle", True)
        heading.setVisible(False)
        layout.addWidget(heading)
        form = QFormLayout()
        self.enum_name = QLabel()
        form.addRow("Name", self.enum_name)
        self.enum_usage = QLabel()
        self.enum_usage.setWordWrap(True)
        form.addRow("Referenced by", self.enum_usage)
        layout.addLayout(form)
        layout.addWidget(
            QLabel(
                "Enum members become the selectable values of any filter or "
                "payload field typed with this enum."
            )
        )
        self.enum_values = EnumValueEditor()
        layout.addWidget(self.enum_values, 1)
        row = QHBoxLayout()
        apply_button = QPushButton("Apply changes")
        set_text_glyph(apply_button, "save")
        apply_button.clicked.connect(self._apply_enum)
        row.addWidget(apply_button)
        row.addLayout(self._schema_action_row(lambda: "enum"))
        row.addStretch()
        layout.addLayout(row)
        return page

    # -------------------------------------------------------------------- tree

    def _populate(self, select_id: str = "") -> None:
        self._loading = True
        self.tree.clear()
        root = QTreeWidgetItem(self.tree, ["Catalog", ""])
        root.setData(0, NODE_KIND, "catalog")
        for service in self.document.services:
            service_item = QTreeWidgetItem(
                root, [service.name, str(len(service.endpoints))]
            )
            service_item.setData(0, NODE_KIND, "service")
            service_item.setData(0, NODE_REF, service.name)
            for module in service.module_names():
                endpoints = service.module(module)
                module_item = QTreeWidgetItem(
                    service_item, [module, str(len(endpoints))]
                )
                module_item.setData(0, NODE_KIND, "module")
                module_item.setData(0, NODE_REF, (service.name, module))
                for endpoint in endpoints:
                    endpoint_item = QTreeWidgetItem(
                        module_item, [endpoint.action or endpoint.path, endpoint.method]
                    )
                    endpoint_item.setData(0, NODE_KIND, "endpoint")
                    endpoint_item.setData(0, NODE_REF, (service.name, endpoint.id))
                    endpoint_item.setToolTip(0, endpoint.path)
        root.setExpanded(True)
        for index in range(root.childCount()):
            root.child(index).setExpanded(True)
        self._populate_schema_nodes()
        self.tree_pane.set_count(self.document.endpoint_count)
        self._loading = False
        if select_id:
            self._select_endpoint_item(select_id)
        else:
            self.tree.setCurrentItem(root)
        self._refresh_catalog_page()

    def _populate_schema_nodes(self) -> None:
        """Adds the Schemas branch: one group per store, schemas beneath."""
        schemas_root = QTreeWidgetItem(self.tree, ["Schemas", ""])
        schemas_root.setData(0, NODE_KIND, "schemas")
        self._schema_root = schemas_root
        for kind, caption in (
            ("filter", "Filter entities"),
            ("payload", "Payload schemas"),
            ("form", "Form schemas"),
            ("response", "Response schemas"),
            ("enum", "Enums"),
        ):
            names = self.document.schema_names(kind)
            group = QTreeWidgetItem(schemas_root, [caption, str(len(names))])
            group.setData(0, NODE_KIND, "schemagroup")
            group.setData(0, NODE_REF, kind)
            for name in names:
                node = QTreeWidgetItem(group, [name, ""])
                node.setData(0, NODE_KIND, "schema")
                node.setData(0, NODE_REF, (kind, name))
                if kind != "enum":
                    used = len(self.document.schema_usage(kind, name))
                    node.setText(1, str(used))
                    node.setToolTip(0, f"{name} — used by {used} endpoint(s)")
        schemas_root.setExpanded(True)

    def _select_schema_item(self, kind: str, name: str) -> None:
        root = getattr(self, "_schema_root", None)
        if root is None:
            return
        for index in range(root.childCount()):
            group = root.child(index)
            if group.data(0, NODE_REF) != kind:
                continue
            group.setExpanded(True)
            for child in range(group.childCount()):
                node = group.child(child)
                if node.data(0, NODE_REF) == (kind, name):
                    self.tree.setCurrentItem(node)
                    return
            self.tree.setCurrentItem(group)
            return

    def _select_endpoint_item(self, endpoint_id: str) -> None:
        iterator = self.tree.findItems(
            "", Qt.MatchFlag.MatchContains | Qt.MatchFlag.MatchRecursive, 0
        )
        for item in iterator:
            reference = item.data(0, NODE_REF)
            if (
                item.data(0, NODE_KIND) == "endpoint"
                and isinstance(reference, tuple)
                and reference[1] == endpoint_id
            ):
                self.tree.setCurrentItem(item)
                return

    def _current(self) -> tuple[str, Any]:
        item = self.tree.currentItem()
        if item is None:
            return "catalog", None
        return str(item.data(0, NODE_KIND) or "catalog"), item.data(0, NODE_REF)

    def _selection_changed(self) -> None:
        if self._loading:
            return
        kind, reference = self._current()
        self._record_history(kind, reference)
        self._show(kind, reference)

    def _show(self, kind: str, reference: Any) -> None:
        self._refresh_editor_title(kind, reference)
        if kind == "service":
            self._load_service(str(reference))
            self.editor.setCurrentIndex(1)
        elif kind == "module":
            self._load_module(*reference)
            self.editor.setCurrentIndex(2)
        elif kind == "endpoint":
            self._load_endpoint(*reference)
            self.editor.setCurrentIndex(3)
        elif kind == "schemagroup":
            self._load_schema_group(str(reference))
            self.editor.setCurrentIndex(4)
        elif kind == "schema":
            self._load_schema(*reference)
        else:
            self._refresh_catalog_page()
            self.editor.setCurrentIndex(0)

    # -------------------------------------------------------------- navigation

    def _record_history(self, kind: str, reference: Any) -> None:
        """Appends the visited node unless we are replaying the history."""
        if self._navigating:
            return
        entry = (kind, reference)
        if self._history and self._history[self._history_index] == entry:
            return
        # A new move discards anything ahead of the cursor, as a browser does.
        del self._history[self._history_index + 1 :]
        self._history.append(entry)
        self._history_index = len(self._history) - 1
        self._refresh_navigation()

    def _refresh_navigation(self) -> None:
        back = getattr(self, "back_action", None)
        if back is None:
            return
        back.setEnabled(self._history_index > 0)
        self.forward_action.setEnabled(
            self._history_index < len(self._history) - 1
        )
        if self._history_index > 0:
            kind, reference = self._history[self._history_index - 1]
            back.setToolTip(f"Back to {self._describe(kind, reference)} (Alt+Left)")
        else:
            back.setToolTip("Nothing to go back to")
        self.back_label.setText(
            f"< {self._describe(*self._history[self._history_index - 1])}"
            if self._history_index > 0
            else ""
        )
        self.back_label.setVisible(self._history_index > 0)

    def _describe(self, kind: str, reference: Any) -> str:
        if kind == "schema" and isinstance(reference, tuple):
            return f"{SCHEMA_LABELS[reference[0]].rstrip('s')} {reference[1]}"
        if kind == "schemagroup":
            return SCHEMA_LABELS.get(str(reference), "Schemas")
        if kind == "endpoint" and isinstance(reference, tuple):
            endpoint = self._endpoint(*reference)
            return endpoint.label if endpoint is not None else "endpoint"
        if kind == "module" and isinstance(reference, tuple):
            return f"{reference[1]}"
        if kind == "service":
            return str(reference)
        return "Catalog"

    def _pane_title(self, kind: str, reference: Any) -> str:
        """Labels the editor pane with the item's type and name.

        The type used to be a separate heading inside every page. Folding it
        into the pane header keeps it without saying the same thing twice.
        """
        name = self._describe(kind, reference)
        prefix = {
            "service": "Service",
            "module": "Module",
            "endpoint": "Endpoint",
        }.get(kind, "")
        if kind == "schema" and isinstance(reference, tuple):
            return name
        return f"{prefix} · {name}" if prefix else name

    def _refresh_editor_title(self, kind: str, reference: Any) -> None:
        """Names the editor pane after the item it is showing.

        The pane header replaces the per-page static heading, so it has to
        carry the item's own name or the right-hand column loses its label.
        """
        self.editor_pane.set_title(self._pane_title(kind, reference))
        count: int | None = None
        if kind == "catalog":
            count = self.document.endpoint_count
        elif kind == "service":
            service = self.document.service(str(reference))
            count = len(service.endpoints) if service is not None else None
        elif kind == "module" and isinstance(reference, tuple):
            service = self.document.service(reference[0])
            count = len(service.module(reference[1])) if service is not None else None
        elif kind == "schemagroup":
            count = len(self.document.schema_names(str(reference)))
        self.editor_pane.set_count(count)

    def _goto(self, index: int) -> None:
        if not 0 <= index < len(self._history):
            return
        self._history_index = index
        kind, reference = self._history[index]
        self._navigating = True
        try:
            if not self._select_node(kind, reference):
                # The node is gone — renamed or deleted — so drop the entry
                # rather than stranding the user on a stale page.
                del self._history[index]
                self._history_index = max(0, min(index, len(self._history) - 1))
                if self._history:
                    kind, reference = self._history[self._history_index]
                    self._select_node(kind, reference)
                    self._show(kind, reference)
            else:
                self._show(kind, reference)
        finally:
            self._navigating = False
        self._refresh_navigation()

    def go_back(self) -> None:
        if self._history_index > 0:
            self._goto(self._history_index - 1)

    def go_forward(self) -> None:
        if self._history_index < len(self._history) - 1:
            self._goto(self._history_index + 1)

    def _select_node(self, kind: str, reference: Any) -> bool:
        """Moves the tree cursor without triggering a second history entry."""
        for item in self.tree.findItems(
            "", Qt.MatchFlag.MatchContains | Qt.MatchFlag.MatchRecursive, 0
        ):
            if item.data(0, NODE_KIND) == kind and item.data(0, NODE_REF) == reference:
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
                self.tree.setCurrentItem(item)
                return True
        return kind in ("catalog", "")

    # ------------------------------------------------------------- page loaders

    def _refresh_catalog_page(self) -> None:
        self.catalog_name.blockSignals(True)
        self.catalog_name.setText(self.document.name)
        self.catalog_name.blockSignals(False)
        self.catalog_root.blockSignals(True)
        self.catalog_root.setText(self.document.workspace_root)
        self.catalog_root.blockSignals(False)
        self.catalog_file.setText(
            str(self.document.path) if self.document.path else "(not saved yet)"
        )
        self.catalog_summary.setText(
            f"{len(self.document.services)} services, "
            f"{self.document.endpoint_count} endpoints, "
            f"{len(self.document.filter_schemas)} filter schemas, "
            f"{len(self.document.payload_schemas)} payload schemas, "
            f"{len(self.document.response_schemas)} response schemas, "
            f"{len(self.document.enums)} enums"
        )
        title = self.document.path.name if self.document.path else "Untitled catalog"
        self.setWindowTitle(
            f"Rest Tester Catalog Builder - {title}{' *' if self.dirty else ''}"
        )

    def _service(self, name: str) -> ServiceDraft | None:
        return self.document.service(name)

    def _load_service(self, name: str) -> None:
        service = self._service(name)
        if service is None:
            return
        self._active_service = name
        self.service_name.setText(service.name)
        self.service_repository.setText(service.repository)
        self.service_base_url.setText(service.default_base_url)
        self.service_framework.setText(service.framework or "manual")
        self.service_builders.setChecked(service.filter_sort_builders)
        self.service_summary.setText(
            f"{len(service.module_names())} modules, {len(service.endpoints)} endpoints"
        )

    def _load_module(self, service_name: str, module: str) -> None:
        service = self._service(service_name)
        if service is None:
            return
        self._active_service = service_name
        self._active_module = module
        self.module_name.setText(module)
        endpoints = service.module(module)
        methods = sorted({endpoint.method for endpoint in endpoints})
        self.module_summary.setText(
            f"{len(endpoints)} endpoints in {service_name}  |  {', '.join(methods)}"
        )

    def _endpoint(self, service_name: str, endpoint_id: str) -> EndpointDraft | None:
        service = self._service(service_name)
        if service is None:
            return None
        for endpoint in service.endpoints:
            if endpoint.id == endpoint_id:
                return endpoint
        return None

    def _load_endpoint(self, service_name: str, endpoint_id: str) -> None:
        endpoint = self._endpoint(service_name, endpoint_id)
        if endpoint is None:
            return
        self._active_service = service_name
        self._active_endpoint = endpoint
        self.endpoint_method.setCurrentText(endpoint.method)
        self.endpoint_path.setText(endpoint.path)
        self.endpoint_action.setText(endpoint.action)
        self.endpoint_module.setText(endpoint.controller)
        self.endpoint_status.setText(endpoint.expected_status)
        self.parameter_table.set_enums(self.document.enums)
        self.parameter_table.set_parameters(endpoint.parameters)
        self.endpoint_payload.setPlainText(
            json.dumps(endpoint.payload, indent=2) if endpoint.payload is not None else ""
        )
        self.endpoint_payload_type.blockSignals(True)
        self.endpoint_payload_type.clear()
        suggestions = sorted(
            set(self.document.payload_schemas)
            | {
                other.payload_type
                for other in self.document.iter_endpoints()
                if other.payload_type
            }
        )
        self.endpoint_payload_type.addItem("")
        self.endpoint_payload_type.addItems(suggestions)
        self.endpoint_payload_type.setCurrentText(endpoint.payload_type or "")
        self.endpoint_payload_type.blockSignals(False)
        self._fill_combo(
            self.endpoint_payload_schema,
            self.document.payload_schemas,
            endpoint.payload_schema,
        )
        self._fill_combo(
            self.endpoint_form_schema,
            self.document.form_schemas,
            endpoint.form_schema,
        )
        self._fill_combo(
            self.endpoint_response_schema,
            self.document.response_schemas,
            endpoint.response_schema,
        )
        self._fill_combo(
            self.endpoint_filter_entity,
            self.document.filter_schemas,
            endpoint.filter_entity,
        )
        self.endpoint_source_file.setText(endpoint.source_file)
        self.endpoint_identity.setText(endpoint.id)

    @staticmethod
    def _fill_combo(combo: QComboBox, names: dict[str, Any], current: str | None) -> None:
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("")
        combo.addItems(sorted(names))
        combo.setCurrentText(current or "")
        combo.blockSignals(False)

    # ---------------------------------------------------------- schema loaders

    def _load_schema_group(self, kind: str) -> None:
        self._active_schema_kind = kind
        label = SCHEMA_LABELS.get(kind, "Schema")
        names = self.document.schema_names(kind)
        self.schema_group_heading.setText(f"{label}s")
        hints = {
            "filter": (
                "A filter entity lists the filterable and sortable properties "
                "of one module, so the tester can offer a module-specific "
                "query builder instead of a free-text filter box."
            ),
            "payload": (
                "A payload schema describes a request body. The tester renders "
                "one input per field rather than a raw JSON editor."
            ),
            "form": (
                "A form schema describes a multipart request: file uploads and "
                "the fields sent alongside them."
            ),
            "response": (
                "A response schema describes a response body so the catalog can "
                "document or validate what an endpoint returns."
            ),
            "enum": (
                "An enum supplies the selectable values for any filter or "
                "payload field declared with that type."
            ),
        }
        self.schema_group_hint.setText(hints.get(kind, ""))
        lines = []
        for name in names:
            if kind == "enum":
                values = self.document.enums.get(name, [])
                lines.append(f"{name}  —  {len(values)} value(s)")
            else:
                used = len(self.document.schema_usage(kind, name))
                schema = self.document.schema(kind, name) or {}
                count = len(schema.get("fields", []) or [])
                lines.append(
                    f"{name}  —  {count} field(s), used by {used} endpoint(s)"
                )
        self.schema_group_list.setPlainText(
            "\n".join(lines) or f"No {label.lower()}s yet. Select New to add one."
        )

    def _load_schema(self, kind: str, name: str) -> None:
        self._active_schema_kind = kind
        self._active_schema_name = name
        schema = self.document.schema(kind, name)
        if schema is None:
            return
        if kind == "filter":
            self.filter_schema_name.setText(name)
            self.filter_schema_usage.setText(self._usage_caption(kind, name))
            self.filter_fields.set_fields(
                schema.get("fields", []) or [], self.document.enums
            )
            self.editor.setCurrentIndex(5)
        elif kind in {"payload", "form", "response"}:
            self.payload_schema_heading.setText(SCHEMA_LABELS[kind])
            self.payload_schema_name.setText(name)
            self.payload_schema_clr.setText(str(schema.get("clr_type", "") or name))
            self.payload_schema_usage.setText(self._usage_caption(kind, name))
            self.payload_fields.set_fields(
                schema.get("fields", []) or [], self.document.enums
            )
            self.editor.setCurrentIndex(6)
        else:
            self.enum_name.setText(name)
            users = self.document.enum_usage(name)
            self.enum_usage.setText(
                ", ".join(users[:8]) + (" ..." if len(users) > 8 else "")
                if users
                else "No field declares this enum yet."
            )
            self.enum_values.set_values(list(schema))
            self.editor.setCurrentIndex(7)

    def _usage_caption(self, kind: str, name: str) -> str:
        endpoints = self.document.schema_usage(kind, name)
        if not endpoints:
            return "No endpoint uses this yet."
        sample = ", ".join(
            f"{endpoint.service} {endpoint.label}" for endpoint in endpoints[:4]
        )
        suffix = f" and {len(endpoints) - 4} more" if len(endpoints) > 4 else ""
        return f"{len(endpoints)} endpoint(s): {sample}{suffix}"

    # ----------------------------------------------------------- schema edits

    def _apply_filter_schema(self) -> None:
        name = getattr(self, "_active_schema_name", "")
        if not name:
            return
        fields = self.filter_fields.fields()
        if not fields:
            QMessageBox.warning(
                self, "No fields", "A filter entity needs at least one field."
            )
            return
        self.document.set_schema("filter", name, {"name": name, "fields": fields})
        self._after_schema_edit("filter", name, f"Updated filter entity {name}.")

    def _apply_payload_schema(self) -> None:
        kind = getattr(self, "_active_schema_kind", "payload")
        name = getattr(self, "_active_schema_name", "")
        if not name:
            return
        fields = self.payload_fields.fields()
        if not fields:
            QMessageBox.warning(
                self, "No fields", f"A {SCHEMA_LABELS[kind].lower()} needs a field."
            )
            return
        existing = self.document.schema(kind, name) or {}
        schema: dict[str, Any] = {
            "name": name,
            "clr_type": self.payload_schema_clr.text().strip() or name,
            "fields": fields,
        }
        # Payload and response schemas carry an explicit object kind; form
        # schemas do not unless a catalog already contains one.
        if kind in {"payload", "response"} or "kind" in existing:
            schema = {"kind": existing.get("kind", "object"), **schema}
        self.document.set_schema(kind, name, schema)
        self._after_schema_edit(kind, name, f"Updated {SCHEMA_LABELS[kind].lower()} {name}.")

    def _apply_enum(self) -> None:
        name = getattr(self, "_active_schema_name", "")
        if not name:
            return
        values = self.enum_values.values()
        if not values:
            QMessageBox.warning(
                self, "No values", "An enum needs at least one member."
            )
            return
        self.document.set_schema("enum", name, values)
        self._after_schema_edit("enum", name, f"Updated enum {name}.")

    def _after_schema_edit(self, kind: str, name: str, message: str) -> None:
        self._mark_dirty()
        self._populate()
        self._select_schema_item(kind, name)
        self._note(message)

    def _prompt_schema_name(self, kind: str, title: str, initial: str = "") -> str:
        label = SCHEMA_LABELS.get(kind, "Schema")
        value, accepted = QInputDialog.getText(
            self, title, f"{label} name", text=initial
        )
        return value.strip() if accepted else ""

    def new_schema_of_kind(self) -> None:
        """Asks which store to add to, then creates the schema."""
        options = [SCHEMA_LABELS[kind] for kind in SCHEMA_KINDS]
        choice, accepted = QInputDialog.getItem(
            self, "New schema", "Kind", options, 0, False
        )
        if not accepted or not choice:
            return
        kind = SCHEMA_KINDS[options.index(choice)]
        self.new_schema(kind)

    def new_schema(self, kind: str) -> None:
        name = self._prompt_schema_name(kind, f"New {SCHEMA_LABELS[kind].lower()}")
        if not name:
            return
        try:
            self.document.add_schema(kind, name)
        except CatalogValidationError as exc:
            QMessageBox.warning(self, "Cannot create", str(exc))
            return
        self._after_schema_edit(kind, name, f"Created {SCHEMA_LABELS[kind].lower()} {name}.")

    def duplicate_schema(self, kind: str) -> None:
        name = getattr(self, "_active_schema_name", "")
        if not name or name not in self.document.schema_store(kind):
            QMessageBox.information(
                self, "Select a schema", "Select the schema to duplicate first."
            )
            return
        new_name = self._prompt_schema_name(
            kind, "Duplicate schema", f"{name}Copy"
        )
        if not new_name:
            return
        try:
            self.document.duplicate_schema(kind, name, new_name)
        except CatalogValidationError as exc:
            QMessageBox.warning(self, "Cannot duplicate", str(exc))
            return
        self._after_schema_edit(kind, new_name, f"Duplicated {name} as {new_name}.")

    def rename_schema(self, kind: str) -> None:
        name = getattr(self, "_active_schema_name", "")
        if not name or name not in self.document.schema_store(kind):
            QMessageBox.information(
                self, "Select a schema", "Select the schema to rename first."
            )
            return
        new_name = self._prompt_schema_name(kind, "Rename schema", name)
        if not new_name or new_name == name:
            return
        try:
            repointed = self.document.rename_schema(kind, name, new_name)
        except CatalogValidationError as exc:
            QMessageBox.warning(self, "Cannot rename", str(exc))
            return
        self._after_schema_edit(
            kind,
            new_name,
            f"Renamed {name} to {new_name}; repointed {repointed} endpoint(s).",
        )

    def delete_schema(self, kind: str) -> None:
        name = getattr(self, "_active_schema_name", "")
        if not name or name not in self.document.schema_store(kind):
            QMessageBox.information(
                self, "Select a schema", "Select the schema to delete first."
            )
            return
        if kind == "enum":
            users = self.document.enum_usage(name)
            detail = (
                f"\n\n{len(users)} field(s) and parameter(s) use this enum; their "
                "values are kept as a fixed list."
                if users
                else ""
            )
        else:
            endpoints = self.document.schema_usage(kind, name)
            detail = (
                f"\n\n{len(endpoints)} endpoint(s) reference it; the reference "
                "will be cleared."
                if endpoints
                else ""
            )
        confirmed = QMessageBox.question(
            self, "Delete schema", f"Delete '{name}'?{detail}"
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        cleared = self.document.remove_schema(kind, name)
        self._active_schema_name = ""
        self._mark_dirty()
        self._populate()
        self._note(
            f"Deleted {name}."
            + (f" Cleared {cleared} endpoint reference(s)." if cleared else "")
        )

    # ----------------------------------------------------------------- editing

    def _mark_dirty(self, *, refresh_page: bool = True) -> None:
        self.dirty = True
        if refresh_page:
            self._refresh_catalog_page()
        else:
            title = self.document.path.name if self.document.path else "Untitled catalog"
            self.setWindowTitle(f"Rest Tester Catalog Builder - {title} *")

    def _catalog_edited(self, value: str) -> None:
        if self._loading:
            return
        self.document.workspace_root = value.strip()
        self._mark_dirty(refresh_page=False)

    def _catalog_name_edited(self, value: str) -> None:
        if self._loading:
            return
        self.document.name = value.strip()
        self._mark_dirty(refresh_page=False)

    def _apply_service(self) -> None:
        service = self._service(getattr(self, "_active_service", ""))
        if service is None:
            return
        new_name = self.service_name.text().strip()
        if not new_name:
            QMessageBox.warning(self, "Service name", "A service needs a name.")
            return
        try:
            if new_name != service.name:
                self.document.rename_service(service.name, new_name)
        except CatalogValidationError as exc:
            QMessageBox.warning(self, "Rename failed", str(exc))
            return
        service.repository = self.service_repository.text().strip()
        service.default_base_url = self.service_base_url.text().strip()
        service.filter_sort_builders = self.service_builders.isChecked()
        self._mark_dirty()
        self._populate()
        self._note(f"Updated service '{new_name}'.")

    def _rescan_service(self) -> None:
        service = self._service(getattr(self, "_active_service", ""))
        if service is None:
            return
        root = Path(service.repository)
        if not root.is_dir():
            QMessageBox.warning(
                self,
                "Cannot rescan",
                f"The repository folder does not exist:\n{service.repository}",
            )
            return
        if not service.framework:
            QMessageBox.warning(
                self,
                "Cannot rescan",
                "This service was created manually, so there is no project "
                "type to rescan with. Delete it and use 'Scan Service...'.",
            )
            return
        try:
            result = scan_project(root, service.name, service.framework)
        except Exception as exc:
            QMessageBox.critical(self, "Scan failed", f"{type(exc).__name__}: {exc}")
            return
        before = len(service.endpoints)
        self.document.merge_scan(service.name, result)
        self._mark_dirty()
        self._populate()
        self._note(
            f"Rescanned '{service.name}': {before} -> {len(service.endpoints)} endpoints."
        )
        for warning in result.warnings:
            self._note(f"  {warning}")

    def _delete_service(self) -> None:
        name = getattr(self, "_active_service", "")
        if not name:
            return
        if (
            QMessageBox.question(
                self, "Delete service", f"Remove '{name}' and all of its endpoints?"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.document.remove_service(name)
        self._mark_dirty()
        self._populate()
        self._note(f"Deleted service '{name}'.")

    def _add_module(self) -> None:
        service = self._service(getattr(self, "_active_service", ""))
        if service is None:
            return
        name, accepted = QInputDialog.getText(self, "Add module", "Module name")
        name = name.strip()
        if not accepted or not name:
            return
        endpoint = EndpointDraft(
            service=service.name,
            controller=name,
            action=f"Get{name}",
            method="GET",
            path=f"/{name}",
        )
        endpoint.refresh_identity()
        service.endpoints.append(endpoint)
        self._mark_dirty()
        self._populate(endpoint.id)
        self._note(f"Added module '{name}' with a starter endpoint.")

    def _rename_module(self) -> None:
        service = self._service(getattr(self, "_active_service", ""))
        old = getattr(self, "_active_module", "")
        if service is None or not old:
            return
        new = self.module_name.text().strip()
        if not new or new == old:
            return
        changed = service.rename_module(old, new)
        self._mark_dirty()
        self._populate()
        self._note(f"Renamed module '{old}' to '{new}' ({changed} endpoints).")

    def _delete_module(self) -> None:
        service = self._service(getattr(self, "_active_service", ""))
        module = getattr(self, "_active_module", "")
        if service is None or not module:
            return
        if (
            QMessageBox.question(
                self, "Delete module", f"Remove '{module}' and its endpoints?"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        removed = service.remove_module(module)
        self._mark_dirty()
        self._populate()
        self._note(f"Deleted module '{module}' ({removed} endpoints).")

    def _add_endpoint(self) -> None:
        service = self._service(getattr(self, "_active_service", ""))
        module = getattr(self, "_active_module", "")
        if service is None or not module:
            return
        endpoint = EndpointDraft(
            service=service.name,
            controller=module,
            action="NewAction",
            method="GET",
            path=f"/{module}",
        )
        endpoint.refresh_identity()
        service.endpoints.append(endpoint)
        service.sort_endpoints()
        self._mark_dirty()
        self._populate(endpoint.id)

    def _apply_endpoint(self) -> None:
        endpoint = getattr(self, "_active_endpoint", None)
        if endpoint is None:
            return
        text = self.endpoint_payload.toPlainText().strip()
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                QMessageBox.warning(self, "Invalid body", f"The body is not JSON: {exc}")
                return
        else:
            payload = None

        endpoint.method = self.endpoint_method.currentText()
        endpoint.path = self.endpoint_path.text().strip() or "/"
        endpoint.action = self.endpoint_action.text().strip()
        endpoint.controller = self.endpoint_module.text().strip() or "Root"
        endpoint.expected_status = self.endpoint_status.text().strip() or "200-299"
        endpoint.parameters = self.parameter_table.parameters()
        endpoint.payload = payload
        endpoint.payload_type = self.endpoint_payload_type.currentText().strip() or None
        endpoint.payload_schema = self.endpoint_payload_schema.currentText().strip() or None
        endpoint.form_schema = self.endpoint_form_schema.currentText().strip() or None
        endpoint.response_schema = (
            self.endpoint_response_schema.currentText().strip() or None
        )
        endpoint.filter_entity = self.endpoint_filter_entity.currentText().strip() or None
        endpoint.source_file = self.endpoint_source_file.text().strip()
        endpoint.refresh_identity()

        problems = endpoint.validate()
        service = self._service(getattr(self, "_active_service", ""))
        if service is not None:
            service.sort_endpoints()
        self._mark_dirty()
        self._populate(endpoint.id)
        if problems:
            self._note("Saved with warnings:")
            for problem in problems:
                self._note(f"  {problem}")
        else:
            self._note(f"Updated {endpoint.label}.")

    def _duplicate_endpoint(self) -> None:
        endpoint = getattr(self, "_active_endpoint", None)
        service = self._service(getattr(self, "_active_service", ""))
        if endpoint is None or service is None:
            return
        copy = EndpointDraft.from_dict(endpoint.to_dict())
        copy.action = f"{endpoint.action}Copy"
        copy.refresh_identity()
        service.endpoints.append(copy)
        service.sort_endpoints()
        self._mark_dirty()
        self._populate(copy.id)

    def _delete_endpoint(self) -> None:
        endpoint = getattr(self, "_active_endpoint", None)
        service = self._service(getattr(self, "_active_service", ""))
        if endpoint is None or service is None:
            return
        service.endpoints.remove(endpoint)
        self._active_endpoint = None
        self._mark_dirty()
        self._populate()
        self._note(f"Deleted {endpoint.label}.")

    # -------------------------------------------------------------- file menu

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            "This catalog has unsaved changes. Discard them?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def new_catalog(self, confirm: bool = True) -> None:
        if confirm and not self._confirm_discard():
            return
        self.document = CatalogDocument()
        self.dirty = False
        self._populate()
        self._note("Started an empty catalog.")

    def open_catalog(self, path: Path | None = None) -> None:
        if path is None:
            if not self._confirm_discard():
                return
            selected, _ = QFileDialog.getOpenFileName(
                self, "Open catalog", "", "Catalog (*.json)"
            )
            if not selected:
                return
            path = Path(selected)
        try:
            self.document = CatalogDocument.load(Path(path))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Cannot open catalog", str(exc))
            return
        self.dirty = False
        self._populate()
        self._note(
            f"Opened {path} with {len(self.document.services)} services and "
            f"{self.document.endpoint_count} endpoints."
        )

    def save(self) -> bool:
        if self.document.path is None:
            return self.save_as()
        return self._write(self.document.path)

    def save_as(self) -> bool:
        selected, _ = QFileDialog.getSaveFileName(
            self, "Save catalog", "api_catalog.json", "Catalog (*.json)"
        )
        if not selected:
            return False
        return self._write(Path(selected))

    def _write(self, path: Path) -> bool:
        try:
            self.document.save(path)
        except CatalogValidationError as exc:
            QMessageBox.warning(self, "Catalog is not valid", str(exc))
            self._note("Save blocked by validation problems.")
            return False
        except OSError as exc:
            QMessageBox.critical(self, "Cannot save", str(exc))
            return False
        self.dirty = False
        self._refresh_catalog_page()
        self._note(f"Saved {self.document.endpoint_count} endpoints to {path}.")
        self.catalog_saved.emit(str(path))
        return True

    def validate(self) -> None:
        problems = self.document.validate()
        if not problems:
            self._note("Catalog is valid.")
            QMessageBox.information(
                self,
                "Catalog is valid",
                f"{len(self.document.services)} services and "
                f"{self.document.endpoint_count} endpoints are ready to save.",
            )
            return
        self._note(f"{len(problems)} problems found:")
        for problem in problems:
            self._note(f"  {problem}")
        QMessageBox.warning(
            self,
            "Catalog has problems",
            "\n".join(problems[:15])
            + ("\n..." if len(problems) > 15 else ""),
        )

    # -------------------------------------------------------------- services

    def add_scanned_service(self) -> None:
        dialog = ScanServiceDialog(self.document.service_names(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.result_scan is None:
            return
        name = dialog.name.text().strip()
        self.document.merge_scan(
            name,
            dialog.result_scan,
            repository=dialog.folder.text().strip(),
            default_base_url=dialog.base_url.text().strip(),
        )
        if not self.document.workspace_root:
            self.document.workspace_root = str(
                Path(dialog.folder.text().strip()).parent
            )
        self.dirty = True
        self._populate()
        self._note(
            f"Added '{name}' with {dialog.result_scan.count} endpoints "
            f"from {dialog.result_scan.framework}."
        )
        for warning in dialog.result_scan.warnings:
            self._note(f"  {warning}")

    def add_manual_service(self) -> None:
        name, accepted = QInputDialog.getText(self, "Add service", "Service name")
        name = name.strip()
        if not accepted or not name:
            return
        try:
            self.document.add_service(name)
        except CatalogValidationError as exc:
            QMessageBox.warning(self, "Cannot add service", str(exc))
            return
        self.dirty = True
        self._populate()
        self._note(f"Added empty service '{name}'. Add a module to start.")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if self._confirm_discard():
            event.accept()
        else:
            event.ignore()


def run(argv: list[str] | None = None) -> int:
    """Standalone entry point for the Catalog Builder."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    application = QApplication.instance() or QApplication(sys.argv)
    theme.apply_theme(application, "Light")
    window = CatalogBuilderWindow(Path(arguments[0]) if arguments else None)
    window.show()
    return application.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
