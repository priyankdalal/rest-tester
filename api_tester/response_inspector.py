"""Full-window response inspector with JSON path / XPath filtering."""

from __future__ import annotations

import json
from typing import Any
from xml.etree.ElementTree import Element, ParseError

from PyQt6.QtCore import QStringListModel, Qt, QTimer
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QCompleter,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import content as content_module
from . import theme
from .icons import icon
from .path_eval import (
    PathResult,
    evaluate_json_path,
    evaluate_xml_path,
    json_result_text,
    parse_xml,
    xml_result_text,
)
from .viewers import JsonTextEdit

JSON_PATH_HELP = (
    "JSON path — same grammar as assertions, captures and baseline ignore paths:\n"
    "  $.values            property (a leading $ is optional)\n"
    "  values[0].name      array index\n"
    "  values.name         project a property across an array\n"
    "  values[*].tags[0]   fan out over every element\n"
    "  values[code=B1]     keep elements whose property equals the value\n"
    "Property names fall back to a case-insensitive match."
)
XML_PATH_HELP = (
    "XPath (ElementTree subset):\n"
    "  /root/item          absolute path\n"
    "  //name              any descendant\n"
    "  item[@id='2']       attribute predicate\n"
    "  item[2]             position (1-based)\n"
    "  item/@id            attribute values\n"
    "  //name/text()       element text"
)
_MAX_SUGGESTIONS = 3000


def json_path_suggestions(document: Any, limit: int = _MAX_SUGGESTIONS) -> list[str]:
    """Generalised paths (arrays as ``[*]``) found in ``document``, in order."""
    seen: dict[str, None] = {}

    def walk(value: Any, path: str) -> None:
        if len(seen) >= limit:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                child = f"{path}.{key}"
                seen.setdefault(child, None)
                walk(item, child)
        elif isinstance(value, list):
            child = f"{path}[*]"
            if value:
                seen.setdefault(child, None)
            for item in value[:50]:
                walk(item, child)

    walk(document, "$")
    return list(seen)


def xml_path_suggestions(root: Element, limit: int = _MAX_SUGGESTIONS) -> list[str]:
    seen: dict[str, None] = {}

    def walk(element: Element, path: str) -> None:
        if len(seen) >= limit:
            return
        seen.setdefault(path, None)
        for name in element.attrib:
            seen.setdefault(f"{path}/@{name}", None)
        for child in element:
            walk(child, f"{path}/{child.tag}")

    walk(root, f"/{root.tag}")
    return list(seen)


class ResponseInspector(QDialog):
    """Non-modal window showing a response body, filterable by path.

    JSON and XML bodies get a path box: the body is re-rendered as whatever the
    path selects, and an indicator above the box reports the match count or
    the error (with its column) while the path is being typed.
    """

    def __init__(
        self,
        text: str,
        kind: str,
        *,
        title: str = "Response body",
        summary: str = "",
        wrapped: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("responseInspector")
        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.WindowMinMaxButtonsHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(1200, 820)
        self._text = text
        self._document: Any = None
        self._root: Element | None = None
        self.mode = "text"
        if kind == content_module.JSON:
            try:
                self._document = json.loads(text) if text.strip() else None
                self.mode = "json" if text.strip() else "text"
            except json.JSONDecodeError:
                self.mode = "text"
        elif kind == content_module.XML:
            try:
                self._root = parse_xml(text)
                self.mode = "xml"
            except ParseError:
                self.mode = "text"
        self._result: PathResult | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        heading = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setProperty("sectionTitle", True)
        heading.addWidget(self.title_label)
        heading.addStretch()
        self.summary_label = QLabel(summary)
        self.summary_label.setProperty("muted", True)
        heading.addWidget(self.summary_label)
        layout.addLayout(heading)

        self.path_panel = QWidget()
        path_layout = QVBoxLayout(self.path_panel)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(4)
        caption_row = QHBoxLayout()
        caption_row.setContentsMargins(0, 0, 0, 0)
        self.path_caption = QLabel("JSON path" if self.mode == "json" else "XPath")
        self.path_caption.setProperty("fieldCaption", True)
        caption_row.addWidget(self.path_caption)
        caption_row.addStretch()
        self.path_indicator = QLabel()
        self.path_indicator.setObjectName("pathIndicator")
        self.path_indicator.setTextFormat(Qt.TextFormat.PlainText)
        caption_row.addWidget(self.path_indicator)
        path_layout.addLayout(caption_row)
        input_row = QHBoxLayout()
        input_row.setSpacing(6)
        self.path_input = QLineEdit()
        self.path_input.setObjectName("pathInput")
        self.path_input.setClearButtonEnabled(True)
        mono = QFont()
        mono.setFamilies(["Cascadia Mono", "Consolas", "Menlo", "monospace"])
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.path_input.setFont(mono)
        self.path_input.setPlaceholderText(
            "$.values[*].name   ·   values[code=B1]   ·   values[0]"
            if self.mode == "json"
            else "//item[@id='2']/name   ·   /root/item/@id   ·   //name/text()"
        )
        self.path_input.setAccessibleName(self.path_caption.text())
        input_row.addWidget(self.path_input, 1)
        self.help_button = QToolButton()
        self.help_button.setObjectName("responseActionButton")
        self.help_button.setToolTip(JSON_PATH_HELP if self.mode == "json" else XML_PATH_HELP)
        self.help_button.setAccessibleName("Path syntax help")
        self.help_button.setFixedSize(34, 32)
        input_row.addWidget(self.help_button)
        path_layout.addLayout(input_row)
        layout.addWidget(self.path_panel)
        self.path_panel.setVisible(self.mode in ("json", "xml"))

        self.editor = JsonTextEdit(read_only=True)
        self.editor.setObjectName("responseCodeEditor")
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
            if wrapped
            else QPlainTextEdit.LineWrapMode.NoWrap
        )
        layout.addWidget(self.editor, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.show_paths = QCheckBox("Show matched paths")
        self.show_paths.setToolTip("Key every JSON match by its concrete path")
        self.show_paths.setVisible(self.mode == "json")
        self.show_paths.toggled.connect(self._render)
        footer.addWidget(self.show_paths)
        footer.addStretch()
        self.find_input = QLineEdit()
        self.find_input.setPlaceholderText("Find in result")
        self.find_input.setMaximumWidth(220)
        self.find_input.returnPressed.connect(self._find_next)
        footer.addWidget(self.find_input)
        self.wrap_button = QPushButton()
        self.wrap_button.setCheckable(True)
        self.wrap_button.setChecked(wrapped)
        self.wrap_button.toggled.connect(self._set_wrap)
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self._copy)
        self._actions = (
            (self.wrap_button, "wrap", "Toggle wrapping"),
            (self.copy_button, "copy", "Copy the result"),
        )
        for button, _name, tooltip in self._actions:
            button.setObjectName("responseActionButton")
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            button.setFixedSize(34, 32)
            footer.addWidget(button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        layout.addLayout(footer)

        suggestions = (
            json_path_suggestions(self._document)
            if self.mode == "json"
            else xml_path_suggestions(self._root)
            if self.mode == "xml"
            else []
        )
        completer = QCompleter(QStringListModel(suggestions, self), self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(14)
        self.path_input.setCompleter(completer)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self.evaluate)
        self.path_input.textChanged.connect(lambda _text: self._timer.start())

        self.refresh_theme()
        self.evaluate()
        if self.mode in ("json", "xml"):
            self.path_input.setFocus()

    # ------------------------------------------------------------ evaluation

    def set_path(self, path: str) -> None:
        self.path_input.setText(path)
        self.evaluate()

    def evaluate(self) -> None:
        self._timer.stop()
        path = self.path_input.text().strip()
        if self.mode == "json":
            self._result = evaluate_json_path(self._document, path)
        elif self.mode == "xml":
            assert self._root is not None
            self._result = evaluate_xml_path(self._root, path)
        else:
            self._result = None
        self._render()

    def path_result(self) -> PathResult | None:
        return self._result

    def indicator_state(self) -> str:
        return str(self.path_indicator.property("state") or "")

    def _render(self) -> None:
        result = self._result
        if self.mode == "text":
            self.editor.set_highlighting(False)
            self.editor.setPlainText(self._text)
            return
        path = self.path_input.text().strip()
        if result is None or not result.ok:
            message = result.error if result else ""
            if result is not None and result.column:
                message = f"{message} · column {result.column}"
            self._set_indicator("error", f"✕  {message}")
            self._set_input_error(True)
            self.editor.set_highlighting(False)
            self.editor.setPlainText("")
            self.editor.setPlaceholderText("No result — fix the path above.")
            return
        self._set_input_error(False)
        if not path:
            self._set_indicator("idle", "Whole document · type a path to filter")
        else:
            count = result.count
            noun = "match" if count == 1 else "matches"
            self._set_indicator("ok", f"✓  {count:,} {noun}")
        if self.mode == "json":
            if self.show_paths.isChecked() and result.paths:
                text = json.dumps(
                    dict(zip(result.paths, result.values)), indent=2, ensure_ascii=False
                )
            else:
                text = json_result_text(result)
            self.editor.set_highlighting(True)
        else:
            text = xml_result_text(result)
            self.editor.set_highlighting(False)
        self.editor.setPlainText(text)

    def _set_indicator(self, state: str, text: str) -> None:
        self.path_indicator.setProperty("state", state)
        self.path_indicator.setText(text)
        self.path_indicator.setToolTip(text)
        color = {"ok": theme.PASS, "error": theme.FAIL}.get(state, theme.TEXT_MUTED)
        self.path_indicator.setStyleSheet(
            f"background: transparent; color: {color}; font-weight: 600;"
        )

    def _set_input_error(self, error: bool) -> None:
        self.path_input.setProperty("validationState", "error" if error else "")
        style = self.path_input.style()
        if style is not None:
            style.unpolish(self.path_input)
            style.polish(self.path_input)

    # --------------------------------------------------------------- actions

    def refresh_theme(self) -> None:
        self.help_button.setIcon(icon("info-circle", theme.TEXT, 18))
        for button, name, _tooltip in self._actions:
            button.setIcon(icon(name, theme.TEXT, 18))
        if self._result is not None or self.mode != "text":
            state = self.indicator_state()
            if state:
                self._set_indicator(state, self.path_indicator.text())

    def _set_wrap(self, wrapped: bool) -> None:
        self.editor.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth
            if wrapped
            else QPlainTextEdit.LineWrapMode.NoWrap
        )

    def _copy(self) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.editor.toPlainText())

    def _find_next(self) -> None:
        needle = self.find_input.text()
        if not needle:
            return
        if not self.editor.find(needle):
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            self.editor.find(needle)
