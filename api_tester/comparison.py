"""Aligned, read-only response comparison."""

from __future__ import annotations

import difflib
import json
from itertools import zip_longest

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFontDatabase, QPainter, QTextCursor, QTextFormat
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSplitter, QTextEdit, QVBoxLayout, QWidget,
)

from . import theme


class DiffPane(QPlainTextEdit):
    def __init__(self) -> None:
        super().__init__()
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setObjectName("responseCodeEditor")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.numbers: list[int | None] = []
        self.gutter = QWidget(self)
        self.gutter.paintEvent = self._paint_numbers
        self.updateRequest.connect(lambda *_: self.gutter.update())

    def set_lines(self, lines: list[str], numbers: list[int | None]) -> None:
        self.numbers = numbers
        self.setPlainText("\n".join(lines))
        last_number = max((number for number in numbers if number is not None), default=1)
        width = self.fontMetrics().horizontalAdvance(str(last_number)) + 18
        self.setViewportMargins(width, 0, 0, 0)
        self._place_gutter(width)

    def _place_gutter(self, width: int) -> None:
        rect = self.viewport().geometry()
        self.gutter.setGeometry(self.contentsRect().left(), rect.top(), width, rect.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_gutter(self.viewportMargins().left())

    def _paint_numbers(self, event) -> None:
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), QColor(theme.ACTIVE_TOKENS["SURFACE"]))
        painter.setPen(QColor(theme.ACTIVE_TOKENS["TEXT_MUTED"]))
        painter.setFont(self.font())
        block = self.firstVisibleBlock()
        while block.isValid():
            top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
            if top > event.rect().bottom():
                break
            index = block.blockNumber()
            if index < len(self.numbers) and self.numbers[index] is not None:
                painter.drawText(
                    0, top, self.gutter.width() - 8, self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight, str(self.numbers[index]),
                )
            block = block.next()
        painter.end()


def _selection(pane: DiffPane, row: int, token: str, span=None):
    selection = QTextEdit.ExtraSelection()
    block = pane.document().findBlockByNumber(row)
    cursor = QTextCursor(block)
    if span is None:
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
    else:
        # Qt cursor offsets count UTF-16 code units, not Python code points.
        text = block.text()
        start, end = (len(text[:offset].encode("utf-16-le")) // 2 for offset in span)
        cursor.setPosition(block.position() + start)
        cursor.setPosition(block.position() + end, QTextCursor.MoveMode.KeepAnchor)
    selection.cursor = cursor
    selection.format.setBackground(QColor(theme.ACTIVE_TOKENS[token]))
    return selection


class ResponseComparison(QDialog):
    def __init__(self, previous: str, current: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Compare with previous response")
        self.resize(1200, 760)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.previous = previous
        self.current = current
        self.changes: list[int] = []
        self.change_index = -1
        layout = QVBoxLayout(self)
        tools = QHBoxLayout()
        self.summary = QLabel()
        tools.addWidget(self.summary, 1)
        self.pretty = QCheckBox("Format JSON")
        self.pretty.setToolTip("Indent JSON on both sides; uncheck to compare exact response text.")
        tools.addWidget(self.pretty)
        self.back = QPushButton("Previous change")
        self.forward = QPushButton("Next change")
        tools.addWidget(self.back)
        tools.addWidget(self.forward)
        layout.addLayout(tools)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.left, self.right = DiffPane(), DiffPane()
        for title, pane in (("Previous response (- removed)", self.left),
                            ("Current response (+ added)", self.right)):
            page = QWidget()
            column = QVBoxLayout(page)
            column.setContentsMargins(0, 0, 0, 0)
            column.addWidget(QLabel(title))
            column.addWidget(pane)
            self.splitter.addWidget(page)
        self.splitter.setSizes([600, 600])
        layout.addWidget(self.splitter, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.left.verticalScrollBar().valueChanged.connect(self.right.verticalScrollBar().setValue)
        self.right.verticalScrollBar().valueChanged.connect(self.left.verticalScrollBar().setValue)
        self.back.clicked.connect(lambda: self._navigate(-1))
        self.forward.clicked.connect(lambda: self._navigate(1))
        try:
            self.formatted = tuple(
                json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                for text in (previous, current)
            )
        except (ValueError, RecursionError):
            # Non-JSON responses are compared verbatim.
            self.formatted = None
        self.pretty.setEnabled(self.formatted is not None)
        self.pretty.setChecked(self.formatted is not None)
        self.pretty.toggled.connect(self._render)
        self._render()

    def _render(self) -> None:
        texts = self.formatted if self.pretty.isChecked() else (self.previous, self.current)
        assert texts is not None
        # Splitting on '\n' retains final-newline differences in raw mode.
        before, after = (text.split("\n") if text else [] for text in texts)
        rows: list[tuple[int | None, int | None, str]] = []
        self.changes = []
        for tag, a, b, c, d in difflib.SequenceMatcher(
            None, before, after, autojunk=False
        ).get_opcodes():
            if tag != "equal":
                self.changes.append(len(rows))
            rows.extend((i, j, tag) for i, j in zip_longest(range(a, b), range(c, d)))
        self.left.set_lines(
            [before[i] if i is not None else "" for i, _, _ in rows],
            [i + 1 if i is not None else None for i, _, _ in rows],
        )
        self.right.set_lines(
            [after[j] if j is not None else "" for _, j, _ in rows],
            [j + 1 if j is not None else None for _, j, _ in rows],
        )
        left_selections, right_selections = [], []
        for row, (i, j, tag) in enumerate(rows):
            if tag == "equal":
                continue
            for pane, index, selections, token in (
                (self.left, i, left_selections, "DANGER_SOFT"),
                (self.right, j, right_selections, "SUCCESS_SOFT"),
            ):
                selections.append(_selection(pane, row, token if index is not None else "BORDER"))
            if tag == "replace" and i is not None and j is not None:
                for kind, a, b, c, d in difflib.SequenceMatcher(
                    None, before[i], after[j], autojunk=False
                ).get_opcodes():
                    if kind != "equal":
                        if a != b:
                            left_selections.append(_selection(self.left, row, "BORDER_STRONG", (a, b)))
                        if c != d:
                            right_selections.append(_selection(self.right, row, "BORDER_STRONG", (c, d)))
        self.left.setExtraSelections(left_selections)
        self.right.setExtraSelections(right_selections)
        self.change_index = -1
        self.summary.setText(
            f"{len(self.changes)} change region(s)"
            if self.changes else
            ("No differences in formatted JSON." if self.pretty.isChecked() else "No textual differences.")
        )
        self.back.setEnabled(bool(self.changes))
        self.forward.setEnabled(bool(self.changes))
        self.left.verticalScrollBar().setValue(0)
        self.right.verticalScrollBar().setValue(0)

    def _navigate(self, step: int) -> None:
        if not self.changes:
            return
        self.change_index = (
            (0 if step > 0 else len(self.changes) - 1)
            if self.change_index < 0 else
            (self.change_index + step) % len(self.changes)
        )
        row = self.changes[self.change_index]
        for pane in (self.left, self.right):
            pane.setTextCursor(QTextCursor(pane.document().findBlockByNumber(row)))
        self.left.verticalScrollBar().setValue(row)
        self.right.verticalScrollBar().setValue(row)
        self.summary.setText(f"Change {self.change_index + 1} of {len(self.changes)}")
