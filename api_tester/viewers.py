"""Structured request and response body widgets.

Replaces the previous raw text dumps with: a grouped header table, a
syntax-highlighted JSON body, image previews, and a save panel for binary
downloads that honours ``Content-Disposition``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QRectF, QRegularExpression, QSize, Qt, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import content as content_module
from . import theme
from .comparison import ResponseComparison
from .icons import icon
from .widgets import attach_table_empty_state

_KEY_PATTERN = QRegularExpression(r'"(?:[^"\\]|\\.)*"(?=\s*:)')
_STRING_PATTERN = QRegularExpression(r'"(?:[^"\\]|\\.)*"')
_NUMBER_PATTERN = QRegularExpression(r"-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b")
_KEYWORD_PATTERN = QRegularExpression(r"\b(?:true|false|null)\b")
_PUNCTUATION_PATTERN = QRegularExpression(r"[\{\}\[\],:]")


def _format(color: str, bold: bool = False) -> QTextCharFormat:
    text_format = QTextCharFormat()
    text_format.setForeground(QColor(color))
    if bold:
        text_format.setFontWeight(QFont.Weight.Bold)
    return text_format


class JsonHighlighter(QSyntaxHighlighter):
    """Colours JSON keys, strings, numbers, literals, and punctuation."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        self._palette_version = -1
        self._rules: list[tuple[Any, QTextCharFormat]] = []
        self._rebuild_rules()

    def _rebuild_rules(self) -> None:
        self._palette_version = theme.PALETTE_VERSION
        self._rules = [
            (_PUNCTUATION_PATTERN, _format(theme.JSON_PUNCTUATION)),
            (_NUMBER_PATTERN, _format(theme.JSON_NUMBER)),
            (_KEYWORD_PATTERN, _format(theme.JSON_KEYWORD, bold=True)),
            (_STRING_PATTERN, _format(theme.JSON_STRING)),
            # Keys are matched last so they win over the generic string rule.
            (_KEY_PATTERN, _format(theme.JSON_KEY, bold=True)),
        ]

    def highlightBlock(self, text: str | None) -> None:
        if self._palette_version != theme.PALETTE_VERSION:
            self._rebuild_rules()
        if not text:
            return
        for pattern, text_format in self._rules:
            iterator = pattern.globalMatch(text)
            while iterator.hasNext():
                match = iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), text_format)


#: Phases of a request, in the order they happen. The first three are measured
#: by a diagnostic probe that opens its own connection *before* the real
#: request, so they precede it in wall-clock time but are **not** components of
#: it — the HTTP request re-establishes its own connection.
TIMELINE_PHASES = (
    ("dns_ms", "DNS lookup", "ACCENT", True),
    ("tcp_ms", "TCP connect", "PATCH", True),
    ("tls_ms", "TLS handshake", "WARN", True),
    ("http_ms", "HTTP request and response", "PRIMARY", False),
)


class WaterfallTimeline(QWidget):
    """Cascading bar chart of the phases of one request.

    Each bar starts where the previous one ended, so the chart reads as the
    request actually unfolded rather than as four unrelated durations.
    """

    LABEL_WIDTH = 190
    DURATION_WIDTH = 92
    ROW_HEIGHT = 30
    BAR_HEIGHT = 15
    TOP = 34

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[str, float, float, str, bool]] = []
        self._total = 0.0
        self._probe_error = ""
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_timings(self, timings: dict[str, float | str]) -> None:
        """Lays the phases end to end, keeping each one's start offset."""
        self._rows = []
        self._probe_error = str(timings.get("probe_error", "") or "")
        offset = 0.0
        for key, label, token, probe in TIMELINE_PHASES:
            if key not in timings:
                continue
            try:
                duration = float(timings[key])
            except (TypeError, ValueError):
                continue
            self._rows.append((label, offset, duration, token, probe))
            offset += duration
        self._total = offset
        self.updateGeometry()
        self.update()

    def rows(self) -> list[tuple[str, float, float, str, bool]]:
        """``(label, start_ms, duration_ms, colour token, from probe)``."""
        return list(self._rows)

    def total_ms(self) -> float:
        return self._total

    def sizeHint(self):  # noqa: N802 - Qt signature
        rows = max(len(self._rows), 1)
        extra = 58 if self._probe_error else 40
        return QSize(560, self.TOP + rows * self.ROW_HEIGHT + extra)

    def minimumSizeHint(self):  # noqa: N802 - Qt signature
        return self.sizeHint()

    def _track(self) -> tuple[float, float]:
        left = self.LABEL_WIDTH
        width = max(self.width() - self.LABEL_WIDTH - self.DURATION_WIDTH - 16, 40)
        return left, width

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tokens = theme.ACTIVE_TOKENS
        text_color = QColor(tokens["TEXT"])
        muted = QColor(tokens["TEXT_MUTED"])
        painter.fillRect(self.rect(), QColor(tokens["SURFACE"]))

        if not self._rows:
            painter.setPen(QPen(muted))
            painter.drawText(
                self.rect(),
                Qt.AlignmentFlag.AlignCenter,
                "Send a request to see its timing breakdown",
            )
            painter.end()
            return

        left, width = self._track()
        scale = width / self._total if self._total > 0 else 0.0

        header = self.font()
        header.setBold(True)
        painter.setFont(header)
        painter.setPen(QPen(muted))
        painter.drawText(
            QRectF(0, 6, self.LABEL_WIDTH - 10, 18),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "Phase",
        )
        painter.drawText(
            QRectF(left, 6, width, 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"0 ms  →  {_format_ms(self._total)}",
        )
        painter.setFont(self.font())

        bottom = self.TOP + len(self._rows) * self.ROW_HEIGHT
        painter.setPen(QPen(QColor(tokens["BORDER"])))
        painter.drawLine(int(left), self.TOP - 8, int(left), bottom)

        for index, (label, start, duration, token, probe) in enumerate(self._rows):
            top = self.TOP + index * self.ROW_HEIGHT
            centre = top + self.ROW_HEIGHT / 2

            painter.setPen(QPen(muted if probe else text_color))
            painter.drawText(
                QRectF(0, top, self.LABEL_WIDTH - 10, self.ROW_HEIGHT),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

            bar_x = left + start * scale
            bar_width = duration * scale
            # A sub-millisecond phase still gets a visible sliver: a phase that
            # ran must never look like it did not happen.
            drawn = max(bar_width, 3.0)
            bar = QRectF(bar_x, centre - self.BAR_HEIGHT / 2, drawn, self.BAR_HEIGHT)
            colour = QColor(tokens.get(token, tokens["PRIMARY"]))
            if probe:
                colour.setAlpha(170)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(colour))
            painter.drawRoundedRect(bar, 3, 3)

            painter.setPen(QPen(text_color))
            painter.drawText(
                QRectF(
                    self.width() - self.DURATION_WIDTH - 8,
                    top,
                    self.DURATION_WIDTH,
                    self.ROW_HEIGHT,
                ),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                _format_ms(duration),
            )

        painter.setPen(QPen(QColor(tokens["BORDER"])))
        painter.drawLine(int(left), bottom, int(left + width), bottom)

        caption = (
            f"Total {_format_ms(self._total)} · DNS, TCP and TLS come from a "
            "diagnostic probe that connects before the request, which then "
            "makes its own connection."
        )
        painter.setPen(QPen(muted))
        painter.drawText(
            QRectF(8, bottom + 4, self.width() - 16, 34),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            | int(Qt.TextFlag.TextWordWrap),
            caption,
        )
        if self._probe_error:
            painter.setPen(QPen(QColor(tokens["FAIL"])))
            painter.drawText(
                QRectF(8, bottom + 38, self.width() - 16, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                f"Probe failed: {self._probe_error}",
            )
        painter.end()


def _format_ms(value: float) -> str:
    """Formats a duration without pretending to precision it does not have."""
    if value >= 100:
        return f"{value:.0f} ms"
    if value >= 10:
        return f"{value:.1f} ms"
    return f"{value:.2f} ms"


def _mono(widget: QPlainTextEdit) -> QPlainTextEdit:
    font = QFont()
    font.setFamilies(["Cascadia Mono", "Consolas", "Menlo", "monospace"])
    font.setStyleHint(QFont.StyleHint.Monospace)
    widget.setFont(font)
    widget.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    widget.setTabStopDistance(28)
    return widget


class JsonTextEdit(QPlainTextEdit):
    """Monospaced editor with JSON syntax highlighting."""

    def __init__(self, read_only: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        _mono(self)
        self.setReadOnly(read_only)
        self.highlighter = JsonHighlighter(self.document())

    def set_highlighting(self, enabled: bool) -> None:
        self.highlighter.setDocument(self.document() if enabled else None)


class HeaderTable(QTableWidget):
    """Headers rendered as a grouped, read-only two-column table.

    While empty it shows an overlay: an idle prompt before anything has been
    sent, and a "none returned" note once a result arrived without headers.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        idle_title: str = "No headers yet",
        idle_guidance: str = "Send a request to see the headers.",
        empty_title: str = "No headers",
        empty_guidance: str = "The last exchange carried no headers.",
    ) -> None:
        super().__init__(0, 2, parent)
        self.setHorizontalHeaderLabels(["Header", "Value"])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setWordWrap(False)
        self.horizontalHeader().setStretchLastSection(True)
        self.setColumnWidth(0, 260)
        self._idle = (idle_title, idle_guidance)
        self._empty = (empty_title, empty_guidance)
        self.empty_state = attach_table_empty_state(
            self, icon_name="send", title=idle_title, guidance=idle_guidance
        )

    def reset(self) -> None:
        """Clears the rows and shows the 'send a request' prompt again."""
        self.setRowCount(0)
        self._set_empty_message(*self._idle)

    def refresh_theme(self) -> None:
        self.empty_state.refresh_theme()

    def _set_empty_message(self, title: str, guidance: str) -> None:
        self.empty_state.set_content(icon_name="send", title=title, guidance=guidance)

    def show_headers(self, headers: dict[str, str]) -> None:
        self.setRowCount(0)
        for group, rows in content_module.group_headers(headers):
            self._append_group(group)
            for name, value in rows:
                self._append_row(name, value)
        if self.rowCount() == 0:
            self._set_empty_message(*self._empty)

    def _append_group(self, name: str) -> None:
        row = self.rowCount()
        self.insertRow(row)
        cell = QTableWidgetItem(name)
        font = cell.font()
        font.setBold(True)
        cell.setFont(font)
        cell.setForeground(QColor(theme.TEXT_MUTED))
        cell.setBackground(QColor(theme.SURFACE_ALT))
        self.setItem(row, 0, cell)
        filler = QTableWidgetItem("")
        filler.setBackground(QColor(theme.SURFACE_ALT))
        self.setItem(row, 1, filler)

    def _append_row(self, name: str, value: str) -> None:
        row = self.rowCount()
        self.insertRow(row)
        key_cell = QTableWidgetItem(name)
        key_cell.setForeground(QColor(theme.JSON_KEY))
        self.setItem(row, 0, key_cell)
        value_cell = QTableWidgetItem(value)
        value_cell.setToolTip(value)
        self.setItem(row, 1, value_cell)


class BinaryPanel(QWidget):
    """Save panel shown for binary and undisplayable bodies."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("responseViewer")
        self._payload = b""
        self._filename = "download.bin"
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary = QLabel("")
        self.summary.setProperty("heading", True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.summary)
        self.detail = QLabel("")
        self.detail.setProperty("muted", True)
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.preview = QLabel("")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setVisible(False)
        self.preview.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.preview, 1)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save as...")
        self.save_button.setProperty("accent", True)
        self.save_button.clicked.connect(self.save)
        buttons.addWidget(self.save_button)
        buttons.addStretch()
        layout.addLayout(buttons)

    def show_body(self, info: content_module.BodyInfo, payload: bytes) -> None:
        self._payload = payload
        self._filename = info.filename
        kind = "Image" if info.kind == content_module.IMAGE else "Binary"
        self.summary.setText(f"{kind} response · {content_module.human_size(info.size)}")
        self.detail.setText(
            f"Content type: {info.content_type or 'unknown'}\nSuggested file name: {info.filename}"
        )
        self.save_button.setEnabled(bool(payload))
        if info.kind == content_module.IMAGE:
            pixmap = QPixmap()
            if pixmap.loadFromData(payload):
                self.preview.setPixmap(
                    pixmap.scaled(
                        560,
                        420,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                self.preview.setVisible(True)
                return
        self.preview.clear()
        self.preview.setVisible(False)

    def save(self) -> str:
        if not self._payload:
            return ""
        target, _ = QFileDialog.getSaveFileName(self, "Save response body", self._filename)
        if not target:
            return ""
        Path(target).write_bytes(self._payload)
        return target


class ResponseViewer(QWidget):
    """Status line plus Body / Headers / Request tabs for one API result."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._payload = b""
        self._info: content_module.BodyInfo | None = None
        self._raw_text = ""
        self._previous_text: str | None = None
        self._diagnostic: dict[str, Any] = {}
        self._inspectors: list[QDialog] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        status_frame = QWidget()
        status_frame.setObjectName("responseStatusBar")
        status_row = QVBoxLayout(status_frame)
        status_row.setContentsMargins(10, 7, 10, 7)
        status_row.setSpacing(4)
        self.status_label = QLabel("No response yet")
        self.status_label.setObjectName("responseStatus")
        self.status_label.setTextFormat(Qt.TextFormat.RichText)
        status_row.addWidget(self.status_label)
        self.url_label = QLabel("")
        self.url_label.setProperty("muted", True)
        self.url_label.setTextFormat(Qt.TextFormat.PlainText)
        self.url_label.setWordWrap(True)
        self.url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        status_row.addWidget(self.url_label)
        layout.addWidget(status_frame)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("responseTabs")
        layout.addWidget(self.tabs, 1)

        body_page = QWidget()
        body_layout = QVBoxLayout(body_page)
        body_tools = QHBoxLayout()
        self.body_summary = QLabel("")
        self.body_summary.setProperty("muted", True)
        body_tools.addWidget(self.body_summary)
        body_tools.addStretch()
        self.view_mode = QComboBox()
        self.view_mode.addItems(["Formatted", "Raw"])
        self.view_mode.currentIndexChanged.connect(self._refresh_body_text)
        body_tools.addWidget(QLabel("View"))
        body_tools.addWidget(self.view_mode)
        self.wrap_button = QPushButton()
        self.wrap_button.setCheckable(True)
        self.wrap_button.toggled.connect(self._set_wrap)
        body_tools.addWidget(self.wrap_button)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Find in response")
        self.search.setMaximumWidth(180)
        self.search.returnPressed.connect(self._find_next)
        body_tools.addWidget(self.search)
        self.find_button = QPushButton()
        self.find_button.clicked.connect(self._find_next)
        body_tools.addWidget(self.find_button)
        self.copy_button = QPushButton()
        self.copy_button.clicked.connect(self._copy_body)
        body_tools.addWidget(self.copy_button)
        self.fullscreen_button = QPushButton()
        self.fullscreen_button.clicked.connect(self._show_fullscreen)
        body_tools.addWidget(self.fullscreen_button)
        self.compare_button = QPushButton()
        self.compare_button.setEnabled(False)
        self.compare_button.clicked.connect(self._compare_previous)
        body_tools.addWidget(self.compare_button)
        self.export_diagnostic_button = QPushButton()
        self.export_diagnostic_button.setEnabled(False)
        self.export_diagnostic_button.clicked.connect(self._export_diagnostic)
        body_tools.addWidget(self.export_diagnostic_button)
        self.save_body_button = QPushButton()
        self.save_body_button.clicked.connect(self._save_body)
        body_tools.addWidget(self.save_body_button)
        self._response_actions = (
            (self.wrap_button, "wrap", "Toggle response text wrapping"),
            (self.find_button, "search", "Find next match in response"),
            (self.copy_button, "copy", "Copy response body"),
            (self.fullscreen_button, "fullscreen", "Open the response in a new window"),
            (self.compare_button, "compare", "Compare with the previous response"),
            (
                self.export_diagnostic_button,
                "export",
                "Export a diagnostic bundle",
            ),
            (self.save_body_button, "save", "Save the response body to a file"),
        )
        for button, _icon_name, tooltip in self._response_actions:
            button.setObjectName("responseActionButton")
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            button.setFixedSize(34, 32)
            button.setIconSize(QSize(18, 18))
        self.refresh_theme()
        body_layout.addLayout(body_tools)

        self.body_stack = QStackedWidget()
        self.text_view = JsonTextEdit(read_only=True)
        self.text_view.setObjectName("responseCodeEditor")
        self.binary_view = BinaryPanel()
        self.body_stack.addWidget(self.text_view)
        self.body_stack.addWidget(self.binary_view)
        body_layout.addWidget(self.body_stack, 1)
        self.body_error = QLabel("")
        self.body_error.setStyleSheet(f"color: {theme.JSON_ERROR};")
        self.body_error.setVisible(False)
        body_layout.addWidget(self.body_error)
        self.tabs.addTab(body_page, "Body")

        self.headers_table = HeaderTable(
            idle_title="No response headers yet",
            idle_guidance="Send a request to see the headers the server returns.",
            empty_title="No response headers",
            empty_guidance="The server returned this response without any headers.",
        )
        self.tabs.addTab(self.headers_table, "Headers")

        timeline_page = QWidget()
        timeline_layout = QVBoxLayout(timeline_page)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        self.timeline = WaterfallTimeline()
        timeline_layout.addWidget(self.timeline, 1)
        self.tabs.addTab(timeline_page, "Timeline")

        request_page = QWidget()
        request_layout = QVBoxLayout(request_page)
        request_layout.addWidget(QLabel("Request headers"))
        self.request_headers = HeaderTable(
            idle_title="No request headers yet",
            idle_guidance="Send a request to see the headers that were sent with it.",
            empty_title="No request headers",
            empty_guidance="The request was sent without any headers.",
        )
        request_layout.addWidget(self.request_headers, 1)
        request_layout.addWidget(QLabel("Request body"))
        self.request_body = JsonTextEdit(read_only=True)
        self.request_body.setObjectName("responseCodeEditor")
        self.request_body.setPlaceholderText("Send a request to see the body that was sent.")
        request_layout.addWidget(self.request_body, 1)
        self.tabs.addTab(request_page, "Request")

        self.validation = QLabel("No validation result yet")
        self.validation.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.validation.setWordWrap(True)
        self.tabs.addTab(self.validation, "Validation")

    def refresh_theme(self) -> None:
        for button, icon_name, _tooltip in self._response_actions:
            button.setIcon(icon(icon_name, theme.TEXT, 18))
        # Called once before the tables exist, from the toolbar setup.
        for table in (getattr(self, "headers_table", None), getattr(self, "request_headers", None)):
            if table is not None:
                table.refresh_theme()
        for inspector in getattr(self, "_inspectors", []):
            inspector.refresh_theme()

    def clear(self) -> None:
        self._payload = b""
        self._info = None
        self._raw_text = ""
        self._previous_text = None
        self._diagnostic = {}
        self.compare_button.setEnabled(False)
        self.status_label.setText("No response yet")
        self.url_label.clear()
        self.body_summary.clear()
        self.text_view.clear()
        self.body_error.setVisible(False)
        self.headers_table.reset()
        self.timeline.set_timings({})
        self.request_headers.reset()
        self.request_body.clear()
        self.validation.setText("No validation result yet")
        self.export_diagnostic_button.setEnabled(False)
        self.body_stack.setCurrentWidget(self.text_view)

    def show_error(self, message: str) -> None:
        self.clear()
        self.status_label.setText(
            f"<span style='color:{theme.FAIL}; font-weight:bold'>ERROR</span>"
        )
        self.text_view.set_highlighting(False)
        self.text_view.setPlainText(message)
        self.validation.setText(
            "Request could not be validated because no HTTP response was received."
        )

    def show_result(self, result: Any) -> None:
        """Renders an ``ApiResult`` or a runner ``CaseResult``."""
        self._previous_text = (
            self._raw_text if self._info is not None and not self._info.is_binary else None
        )
        headers = dict(getattr(result, "response_headers", {}) or {})
        payload = getattr(result, "content", b"") or b""
        content_type = getattr(result, "content_type", "") or headers.get("Content-Type", "")
        disposition = getattr(result, "content_disposition", "") or headers.get(
            "Content-Disposition", ""
        )
        url = getattr(result, "url", "") or ""

        if not payload and getattr(result, "response_body", ""):
            # Persisted results keep only the decoded text.
            payload = result.response_body.encode("utf-8", errors="replace")
            content_type = content_type or "text/plain"

        info = content_module.inspect_body(payload, content_type, disposition, url)
        self._payload = payload
        self._info = info
        self._raw_text = (
            "" if info.is_binary else content_module.decode_text(payload, content_type)
        )

        self._show_status(result)
        self.url_label.setText(url)
        self.headers_table.show_headers(headers)
        self.request_headers.show_headers(dict(getattr(result, "request_headers", {}) or {}))
        self._show_request_body(getattr(result, "request_body", "") or "")
        self._show_body(info)
        self._show_timeline(dict(getattr(result, "timings", {}) or {}))
        passed = bool(getattr(result, "passed", False))
        error = getattr(result, "error", "") or ""
        self.validation.setText(
            error
            if error
            else (
                "Expected status and all enabled assertions passed."
                if passed
                else "The response did not satisfy the configured verification."
            )
        )
        if info.is_binary:
            self._previous_text = None
        self.compare_button.setEnabled(self._previous_text is not None)
        self._diagnostic = {
            "url": url,
            "status_code": int(getattr(result, "status_code", 0) or 0),
            "elapsed_ms": int(getattr(result, "elapsed_ms", 0) or 0),
            "response_headers": headers,
            "response_body": (
                f"<binary body: {len(payload)} bytes>" if info.is_binary else self._raw_text
            ),
            "request_headers": dict(getattr(result, "request_headers", {}) or {}),
            "request_body": getattr(result, "request_body", "") or "",
            "timings": dict(getattr(result, "timings", {}) or {}),
        }
        self.export_diagnostic_button.setEnabled(True)

    def _show_timeline(self, timings: dict[str, float | str]) -> None:
        self.timeline.set_timings(timings)

    def _find_next(self) -> None:
        text = self.search.text()
        if not text:
            return
        if not self.text_view.find(text):
            cursor = self.text_view.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.text_view.setTextCursor(cursor)
            self.text_view.find(text)

    def _show_fullscreen(self) -> QDialog:
        from .response_inspector import ResponseInspector

        info = self._info
        kind = info.kind if info is not None else content_module.TEXT
        raw = self.view_mode.currentText() == "Raw"
        text = (
            self._raw_text
            if raw or kind in (content_module.JSON, content_module.XML)
            else self.text_view.toPlainText()
        )
        if info is None:
            text = self.text_view.toPlainText()
        diagnostic = self._diagnostic
        status_parts = (
            [f"HTTP {diagnostic.get('status_code', 0)}", f"{diagnostic.get('elapsed_ms', 0)} ms"]
            if diagnostic.get("status_code")
            else []
        )
        inspector = ResponseInspector(
            text,
            kind,
            title="Response body",
            summary=" · ".join(status_parts + [self.body_summary.text()]).strip(" ·"),
            wrapped=self.wrap_button.isChecked(),
            parent=self,
        )
        self._inspectors.append(inspector)
        inspector.finished.connect(
            lambda _code=0, window=inspector: self._forget_inspector(window)
        )
        inspector.show()
        inspector.raise_()
        inspector.activateWindow()
        return inspector

    def _forget_inspector(self, window: QDialog) -> None:
        if window in self._inspectors:
            self._inspectors.remove(window)

    def _compare_previous(self) -> None:
        if self._previous_text is None:
            return
        ResponseComparison(self._previous_text, self._raw_text, self).exec()

    def _export_diagnostic(self) -> str:
        if not self._diagnostic:
            return ""
        target, _ = QFileDialog.getSaveFileName(
            self, "Export diagnostic bundle", "api-diagnostic.json", "JSON (*.json)"
        )
        if not target:
            return ""
        Path(target).write_text(
            json.dumps(self._diagnostic, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return target

    def _show_status(self, result: Any) -> None:
        status_code = int(getattr(result, "status_code", 0) or 0)
        elapsed = int(getattr(result, "elapsed_ms", 0) or 0)
        error = getattr(result, "error", "") or ""
        outcome = getattr(result, "outcome", None)
        if outcome is None:
            outcome = "PASS" if getattr(result, "passed", False) else "FAIL"
        outcome_color = {
            "PASS": theme.PASS,
            "FAIL": theme.FAIL,
            "ERROR": theme.WARN,
            "SKIPPED": theme.SKIP,
        }.get(outcome, theme.TEXT_MUTED)
        reason = getattr(result, "reason", "") or ""
        status_text = f"{status_code} {reason}".strip() if status_code else "no status"
        size = content_module.human_size(len(self._payload))
        parts = [
            f"<span style='color:{outcome_color}; font-weight:bold'>{outcome}</span>",
            f"<span style='color:{theme.status_color(status_code)}; font-weight:bold'>"
            f"HTTP {status_text}</span>",
            f"<span style='color:{theme.TEXT_MUTED}'>{elapsed} ms · {size}</span>",
        ]
        if error:
            parts.append(f"<span style='color:{theme.FAIL}'>{error}</span>")
        timings = getattr(result, "timings", {}) or {}
        if "auth_ms" in timings:
            parts.append(
                f"Auth {float(timings['auth_ms']):.1f} ms"
                f" &nbsp;({int(timings.get('auth_retry_count', 0))} retries)"
            )
        self.status_label.setText(" &nbsp;·&nbsp; ".join(parts))

    def _show_request_body(self, body: str) -> None:
        formatted, error = content_module.pretty_json(body)
        self.request_body.set_highlighting(not error and bool(formatted))
        self.request_body.setPlainText(formatted if not error else body)

    def _show_body(self, info: content_module.BodyInfo) -> None:
        if info.kind == content_module.EMPTY:
            self.body_summary.setText("Empty body")
            self.view_mode.setEnabled(False)
            self.save_body_button.setEnabled(False)
            self.text_view.set_highlighting(False)
            self.text_view.clear()
            self.body_stack.setCurrentWidget(self.text_view)
            self.body_error.setVisible(False)
            return

        self.save_body_button.setEnabled(True)
        if info.is_binary:
            self.view_mode.setEnabled(False)
            self.body_summary.setText(
                f"{info.content_type or 'binary'} · {content_module.human_size(info.size)}"
            )
            self.binary_view.show_body(info, self._payload)
            self.body_stack.setCurrentWidget(self.binary_view)
            self.body_error.setVisible(False)
            return

        self.view_mode.setEnabled(True)
        summary = f"{info.content_type or info.kind} · {content_module.human_size(info.size)}"
        if info.kind == content_module.JSON and not info.error:
            try:
                summary += f" · {content_module.describe_json(json.loads(info.text))}"
            except json.JSONDecodeError:
                pass
        self.body_summary.setText(summary)
        self.body_error.setText(f"Invalid JSON — {info.error}" if info.error else "")
        self.body_error.setVisible(bool(info.error))
        self.body_stack.setCurrentWidget(self.text_view)
        self._refresh_body_text()

    def _refresh_body_text(self) -> None:
        if self._info is None or self._info.is_binary:
            return
        raw = self.view_mode.currentText() == "Raw"
        text = self._raw_text if raw else self._info.text
        self.text_view.set_highlighting(self._info.kind == content_module.JSON and not raw)
        self.text_view.setPlainText(text)

    def _set_wrap(self, wrapped: bool) -> None:
        mode = (
            QPlainTextEdit.LineWrapMode.WidgetWidth
            if wrapped
            else QPlainTextEdit.LineWrapMode.NoWrap
        )
        self.text_view.setLineWrapMode(mode)

    def _copy_body(self) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.text_view.toPlainText())

    def _save_body(self) -> str:
        if self._info is None or not self._payload:
            return ""
        target, _ = QFileDialog.getSaveFileName(self, "Save response body", self._info.filename)
        if not target:
            return ""
        Path(target).write_bytes(self._payload)
        return target


class FilePicker(QWidget):
    """Line edit plus a browse button, used for ``IFormFile`` parameters."""

    changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(4)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Choose a file to upload")
        self.path_edit.textChanged.connect(self.changed.emit)
        layout.addWidget(self.path_edit, 1)
        self.browse_button = QPushButton()
        self.browse_button.setToolTip("Browse for a file to upload")
        self.browse_button.setAccessibleName("Browse for a file to upload")
        self.browse_button.clicked.connect(self.browse)
        layout.addWidget(self.browse_button)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        self.browse_button.setIcon(icon("open", theme.TEXT, 18))

    def text(self) -> str:
        return self.path_edit.text()

    def setText(self, value: str) -> None:
        self.path_edit.setText(value or "")

    def browse(self) -> str:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file to upload")
        if path:
            self.path_edit.setText(path)
        return path


class ValuePicker(QWidget):
    """Dropdown of the values a parameter accepts.

    Editable on purpose: the catalog lists the values the API documents, but a
    tester must still be able to send an undocumented one to check the
    validation.
    """

    changed = pyqtSignal(str)

    def __init__(self, values: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(0)
        self.combo = QComboBox()
        self.combo.setEditable(True)
        self.combo.addItem("")
        self.combo.addItems([str(value) for value in values])
        self.combo.setToolTip(
            "Allowed values:\n" + "\n".join(str(value) for value in values)
        )
        self.combo.currentTextChanged.connect(self.changed.emit)
        layout.addWidget(self.combo, 1)

    def text(self) -> str:
        return self.combo.currentText()

    def setText(self, value: str) -> None:
        self.combo.setCurrentText(value or "")


#: Request body modes offered by RequestBodyEditor.
JSON_MODE = "JSON"
TEXT_MODE = "Text"

_BODY_MODES = (JSON_MODE, TEXT_MODE)


class RequestBodyEditor(QWidget):
    """JSON/text request body editor with beautify, minify, and validation.

    Exposes ``toPlainText`` / ``setPlainText`` so it can stand in for the
    ``QTextEdit`` it replaces.
    """

    changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        tools = QHBoxLayout()
        self.mode = QComboBox()
        self.mode.addItems(_BODY_MODES)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        tools.addWidget(QLabel("Body"))
        tools.addWidget(self.mode)
        self.beautify_button = QPushButton()
        self.beautify_button.clicked.connect(self.beautify)
        tools.addWidget(self.beautify_button)
        self.minify_button = QPushButton()
        self.minify_button.clicked.connect(self.minify)
        tools.addWidget(self.minify_button)
        self.load_button = QPushButton()
        self.load_button.clicked.connect(self.load_file)
        tools.addWidget(self.load_button)
        self.clear_button = QPushButton()
        self.clear_button.clicked.connect(lambda: self.setPlainText(""))
        tools.addWidget(self.clear_button)
        self.action_buttons = {
            "beautify": (
                self.beautify_button,
                "Beautify JSON body (Ctrl+B)",
            ),
            "minify": (self.minify_button, "Minify JSON body"),
            "open": (
                self.load_button,
                "Load body from a text or JSON file",
            ),
            "clear": (self.clear_button, "Clear request body"),
        }
        for button, tooltip in self.action_buttons.values():
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
        tools.addStretch()
        self.status = QLabel("")
        self.status.setProperty("muted", True)
        tools.addWidget(self.status)
        layout.addLayout(tools)

        self.editor = JsonTextEdit()
        self.editor.setPlaceholderText("No request payload")
        self.editor.textChanged.connect(self._text_changed)
        layout.addWidget(self.editor, 1)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        for icon_name, (button, _tooltip) in self.action_buttons.items():
            button.setIcon(icon(icon_name, theme.TEXT, 18))

    # -- QTextEdit-compatible surface -------------------------------------
    def toPlainText(self) -> str:
        return self.editor.toPlainText()

    def setPlainText(self, text: str) -> None:
        self.editor.setPlainText(text or "")

    def setPlaceholderText(self, text: str) -> None:
        self.editor.setPlaceholderText(text)

    def clear(self) -> None:
        self.editor.clear()

    # -- Behaviour ---------------------------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode in _BODY_MODES:
            self.mode.setCurrentText(mode)

    def _mode_changed(self) -> None:
        is_json = self.mode.currentText() == JSON_MODE
        self.beautify_button.setEnabled(is_json)
        self.minify_button.setEnabled(is_json)
        self.editor.set_highlighting(is_json)
        self._validate()

    def _text_changed(self) -> None:
        self._validate()
        self.changed.emit()

    def _validate(self) -> None:
        text = self.editor.toPlainText().strip()
        if self.mode.currentText() != JSON_MODE or not text:
            self.status.setText("")
            self.status.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            return
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            self.status.setText(f"Invalid JSON — line {exc.lineno}, col {exc.colno}")
            self.status.setStyleSheet(f"color: {theme.JSON_ERROR}; font-weight: 600;")
            return
        self.status.setText(f"Valid JSON · {content_module.describe_json(document)}")
        self.status.setStyleSheet(f"color: {theme.PASS};")

    def beautify(self) -> bool:
        formatted, error = content_module.pretty_json(self.editor.toPlainText())
        if error:
            return False
        self.editor.setPlainText(formatted)
        return True

    def minify(self) -> bool:
        compact, error = content_module.minify_json(self.editor.toPlainText())
        if error:
            return False
        self.editor.setPlainText(compact)
        return True

    def load_file(self) -> str:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load request body", "", "Text files (*.json *.txt *.xml *.csv);;All files (*)"
        )
        if not path:
            return ""
        payload = Path(path).read_bytes()
        if content_module.looks_binary(payload):
            QMessageBox.warning(
                self,
                "Binary file",
                "That file is binary. Attach binary content with a form 'file' parameter instead.",
            )
            return ""
        self.setPlainText(content_module.decode_text(payload))
        if not Path(path).suffix.lower() == ".json":
            self.set_mode(TEXT_MODE)
        return path
