"""Run-level waterfall: where the time went across a whole suite run.

The per-response waterfall in :mod:`api_tester.viewers` breaks one request into
its phases. This one zooms out a level: each *case* becomes a bar positioned on
a shared run timeline, so a slow suite can be read as "case 7 stalled" rather
than as a column of unrelated durations.
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .runner import SuiteResult

#: Colour token per outcome, resolved against the live palette at paint time.
OUTCOME_TOKENS = {
    "PASS": "PASS",
    "FAIL": "FAIL",
    "ERROR": "WARN",
    "SKIPPED": "SKIP",
}


def _format_ms(value: float) -> str:
    """Formats a duration without pretending to precision it does not have."""
    if value >= 10_000:
        return f"{value / 1000:.1f} s"
    if value >= 100:
        return f"{value:.0f} ms"
    if value >= 10:
        return f"{value:.1f} ms"
    return f"{value:.2f} ms"


class TimelineRow:
    """One case placed on the run timeline, in milliseconds from run start."""

    __slots__ = ("case_id", "label", "start", "wall", "request_start", "request", "outcome")

    def __init__(
        self,
        case_id: str,
        label: str,
        start: float,
        wall: float,
        request_start: float,
        request: float,
        outcome: str,
    ) -> None:
        self.case_id = case_id
        self.label = label
        self.start = start
        self.wall = wall
        self.request_start = request_start
        self.request = request
        self.outcome = outcome

    @property
    def end(self) -> float:
        return self.start + self.wall

    @property
    def overhead(self) -> float:
        """Case time that was not spent waiting on the HTTP call."""
        return max(self.wall - self.request, 0.0)


class SuiteWaterfall(QWidget):
    """Cascading bar chart of every case in a run."""

    case_selected = pyqtSignal(str)

    LABEL_WIDTH = 240
    DURATION_WIDTH = 96
    ROW_HEIGHT = 26
    BAR_HEIGHT = 13
    TOP = 30
    BOTTOM_PAD = 14

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[TimelineRow] = []
        self._total = 0.0
        self._measured = False
        self._show_overhead = True
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # -- data -------------------------------------------------------------

    def set_result(self, result: SuiteResult | None) -> None:
        """Lays every case on a shared timeline.

        Prefers the offsets recorded by the runner. A result that predates them
        (or a synthetic one) has no measured offsets, so the cases are laid end
        to end from ``elapsed_ms`` instead and the caption says it is an
        approximation rather than quietly implying precision.
        """
        self._rows = []
        self._total = 0.0
        self._measured = False
        if result is None or not result.results:
            self.updateGeometry()
            self.update()
            return

        self._measured = any(item.wall_ms > 0 for item in result.results)
        cursor = 0.0
        for case in result.results:
            elapsed = float(case.elapsed_ms or 0)
            label = case.case_name or f"{case.method} {case.path}".strip()
            if case.phase == "cleanup":
                label = f"Cleanup · {label}"
            if self._measured:
                start = float(case.start_offset_ms)
                wall = float(case.wall_ms)
                request_start = start + float(case.request_offset_ms)
            else:
                start = cursor
                wall = elapsed
                request_start = start
                cursor += elapsed
            self._rows.append(
                TimelineRow(
                    case_id=case.case_id,
                    label=label,
                    start=start,
                    wall=wall,
                    request_start=request_start,
                    request=elapsed,
                    outcome=case.outcome,
                )
            )
        self._total = max((row.end for row in self._rows), default=0.0)
        self.updateGeometry()
        self.update()

    def rows(self) -> list[TimelineRow]:
        return list(self._rows)

    def total_ms(self) -> float:
        return self._total

    def is_measured(self) -> bool:
        """``False`` when the layout is an end-to-end approximation."""
        return self._measured

    def overhead_ms(self) -> float:
        """Run time not spent waiting on HTTP calls."""
        return max(self._total - sum(row.request for row in self._rows), 0.0)

    def set_show_overhead(self, show: bool) -> None:
        self._show_overhead = bool(show)
        self.update()

    # -- geometry ---------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt signature
        rows = max(len(self._rows), 1)
        return QSize(640, self.TOP + rows * self.ROW_HEIGHT + self.BOTTOM_PAD)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt signature
        return QSize(420, self.TOP + self.ROW_HEIGHT + self.BOTTOM_PAD)

    def _track(self) -> tuple[float, float]:
        left = self.LABEL_WIDTH
        width = max(self.width() - self.LABEL_WIDTH - self.DURATION_WIDTH - 16, 40)
        return left, width

    def _row_at(self, y: float) -> TimelineRow | None:
        index = int((y - self.TOP) // self.ROW_HEIGHT)
        if 0 <= index < len(self._rows) and y >= self.TOP:
            return self._rows[index]
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt signature
        row = self._row_at(event.position().y())
        if row is not None:
            self.case_selected.emit(row.case_id)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt signature
        row = self._row_at(event.position().y())
        if row is None:
            self.setToolTip("")
        else:
            lines = [
                row.label,
                f"Outcome: {row.outcome}",
                f"Starts at {_format_ms(row.start)} · case took {_format_ms(row.wall)}",
                f"HTTP call {_format_ms(row.request)} · other work {_format_ms(row.overhead)}",
            ]
            self.setToolTip("\n".join(lines))
        super().mouseMoveEvent(event)

    # -- painting ---------------------------------------------------------

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
                "Run the suite to see where its time went",
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
            QRectF(0, 4, self.LABEL_WIDTH - 10, 18),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "Case",
        )
        painter.drawText(
            QRectF(left, 4, width, 18),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"0 ms  →  {_format_ms(self._total)}",
        )
        painter.setFont(self.font())

        bottom = self.TOP + len(self._rows) * self.ROW_HEIGHT
        painter.setPen(QPen(QColor(tokens["BORDER"])))
        painter.drawLine(int(left), self.TOP - 6, int(left), bottom)

        overhead_color = QColor(tokens["BORDER"])
        metrics = painter.fontMetrics()
        for index, row in enumerate(self._rows):
            top = self.TOP + index * self.ROW_HEIGHT
            centre = top + self.ROW_HEIGHT / 2

            painter.setPen(QPen(muted if row.outcome == "SKIPPED" else text_color))
            painter.drawText(
                QRectF(0, top, self.LABEL_WIDTH - 10, self.ROW_HEIGHT),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                metrics.elidedText(
                    row.label, Qt.TextElideMode.ElideMiddle, self.LABEL_WIDTH - 14
                ),
            )

            painter.setPen(Qt.PenStyle.NoPen)
            if self._show_overhead and row.wall > row.request:
                # The full case extent, so setup and assertion time is visible
                # rather than being silently folded into the HTTP bar.
                painter.setBrush(QBrush(overhead_color))
                painter.drawRoundedRect(
                    QRectF(
                        left + row.start * scale,
                        centre - self.BAR_HEIGHT / 2,
                        max(row.wall * scale, 2.0),
                        self.BAR_HEIGHT,
                    ),
                    3,
                    3,
                )

            colour = QColor(tokens.get(OUTCOME_TOKENS.get(row.outcome, "PRIMARY"), tokens["PRIMARY"]))
            # A case that ran must never render as nothing at all.
            request_width = max(row.request * scale, 3.0)
            painter.setBrush(QBrush(colour))
            painter.drawRoundedRect(
                QRectF(
                    left + row.request_start * scale,
                    centre - self.BAR_HEIGHT / 2,
                    request_width,
                    self.BAR_HEIGHT,
                ),
                3,
                3,
            )

            painter.setPen(QPen(muted if row.outcome == "SKIPPED" else text_color))
            painter.drawText(
                QRectF(
                    self.width() - self.DURATION_WIDTH - 8,
                    top,
                    self.DURATION_WIDTH,
                    self.ROW_HEIGHT,
                ),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                "—" if row.outcome == "SKIPPED" else _format_ms(row.request),
            )

        painter.setPen(QPen(QColor(tokens["BORDER"])))
        painter.drawLine(int(left), bottom, int(left + width), bottom)
        painter.end()


class LegendStrip(QWidget):
    """Compact key for the bar colours.

    Deliberately shrinkable: a plain ``QLabel`` sentence here reported a ~780px
    minimum width, which propagated up and forced the whole window wider.
    """

    SWATCH = 10
    GAP = 6
    SPACING = 14

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries = [
            ("PASS", "Pass"),
            ("FAIL", "Fail"),
            ("WARN", "Error"),
            ("SKIP", "Skipped"),
            ("BORDER", "Other work"),
        ]
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setToolTip("Bar colour shows the case outcome. Click a bar to open that case.")

    def _entry_width(self, metrics, label: str) -> float:
        return self.SWATCH + self.GAP + metrics.horizontalAdvance(label)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt signature
        metrics = self.fontMetrics()
        width = sum(self._entry_width(metrics, label) for _, label in self._entries)
        width += self.SPACING * (len(self._entries) - 1)
        return QSize(int(width), max(metrics.height(), self.SWATCH) + 4)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt signature
        metrics = self.fontMetrics()
        return QSize(60, max(metrics.height(), self.SWATCH) + 4)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        tokens = theme.ACTIVE_TOKENS
        metrics = painter.fontMetrics()
        x = 0.0
        centre = self.height() / 2
        for token, label in self._entries:
            needed = self._entry_width(metrics, label)
            # Drop trailing entries rather than overflowing and dragging the
            # minimum width up with them.
            if x + needed > self.width():
                break
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(tokens.get(token, tokens["PRIMARY"]))))
            painter.drawRoundedRect(
                QRectF(x, centre - self.SWATCH / 2, self.SWATCH, self.SWATCH), 2, 2
            )
            painter.setPen(QPen(QColor(tokens["TEXT_MUTED"])))
            painter.drawText(
                QRectF(x + self.SWATCH + self.GAP, 0, needed, self.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            x += needed + self.SPACING
        painter.end()


class SuiteTimelineTab(QWidget):
    """The run-level waterfall plus its summary caption and legend."""

    case_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.summary = QLabel("No run yet")
        self.summary.setObjectName("timelineSummary")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        controls = QHBoxLayout()
        self.show_overhead = QCheckBox("Show non-HTTP time")
        self.show_overhead.setChecked(True)
        self.show_overhead.setToolTip(
            "Shows the whole case extent behind each bar, so time spent on "
            "variable substitution, assertions and captures is visible."
        )
        self.show_overhead.toggled.connect(self._toggle_overhead)
        controls.addWidget(self.show_overhead)
        controls.addStretch(1)
        self.legend = LegendStrip()
        controls.addWidget(self.legend)
        layout.addLayout(controls)

        self.chart = SuiteWaterfall()
        self.chart.case_selected.connect(self.case_selected)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.chart)
        layout.addWidget(self.scroll, 1)

    def _toggle_overhead(self, checked: bool) -> None:
        self.chart.set_show_overhead(checked)

    def set_result(self, result: SuiteResult | None) -> None:
        self.chart.set_result(result)
        if result is None or not result.results:
            self.summary.setText("No run yet")
            return
        total = self.chart.total_ms()
        http = sum(float(case.elapsed_ms or 0) for case in result.results)
        overhead = max(total - http, 0.0)
        share = (http / total * 100) if total > 0 else 0.0
        parts = [
            f"<b>{len(result.results)} cases</b> over {_format_ms(total)}",
            f"{_format_ms(http)} waiting on HTTP ({share:.0f}%)",
            f"{_format_ms(overhead)} on everything else",
        ]
        text = " · ".join(parts)
        if not self.chart.is_measured():
            text += (
                "<br><i>Approximate: this run has no recorded offsets, so cases "
                "are laid end to end from their response times.</i>"
            )
        self.summary.setText(text)
