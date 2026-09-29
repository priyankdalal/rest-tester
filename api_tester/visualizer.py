"""Visual analysis of a suite run: summary tiles, timing chart, and breakdowns."""

from __future__ import annotations

from PyQt6.QtCore import QPoint, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .runner import CaseResult, SuiteResult
from .widgets import (
    AccordionScrollArea,
    NumericTableItem,
    attach_table_empty_state,
    enable_result_sorting,
)

PASS_COLOR = QColor(theme.PASS)
FAIL_COLOR = QColor(theme.FAIL)
ERROR_COLOR = QColor(theme.WARN)
SKIP_COLOR = QColor(theme.SKIP)

OUTCOME_COLORS = {
    "PASS": PASS_COLOR,
    "FAIL": FAIL_COLOR,
    "ERROR": ERROR_COLOR,
    "SKIPPED": SKIP_COLOR,
}


def _outcome_color(outcome: str) -> QColor:
    """Resolve outcome colours from the active palette rather than import time."""
    tokens = {
        "PASS": theme.PASS,
        "FAIL": theme.FAIL,
        "ERROR": theme.WARN,
        "SKIPPED": theme.SKIP,
    }
    return QColor(tokens.get(outcome, theme.SKIP))


def _format_duration(milliseconds: float) -> str:
    if milliseconds >= 1000:
        return f"{milliseconds / 1000:.1f} s"
    if milliseconds >= 100:
        return f"{milliseconds:.0f} ms"
    if milliseconds >= 10:
        return f"{milliseconds:.1f} ms"
    return f"{milliseconds:.2f} ms"


class SummaryTile(QGroupBox):
    def __init__(self, caption: str, outcome: str = "") -> None:
        super().__init__(caption)
        self.setObjectName("summaryTile")
        layout = QVBoxLayout(self)
        self.value = QLabel("0")
        self.value.setObjectName("summaryValue")
        if outcome:
            self.value.setProperty("outcome", outcome)
        self.value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.value)

    def set_value(self, text: str) -> None:
        self.value.setText(text)


class TimingChart(QWidget):
    """Per-case response-time bars, coloured by outcome."""

    bar_clicked = pyqtSignal(int)

    def __init__(self) -> None:
        super().__init__()
        self.results: list[CaseResult] = []
        self._hovered_index: int | None = None
        self._selected_index: int | None = None
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)

    def set_results(self, results: list[CaseResult]) -> None:
        self.results = results
        self._hovered_index = None
        self._selected_index = None
        self.update()

    def _bar_geometry(self) -> tuple[float, float, float, int]:
        width = self.width() - 70
        height = self.height() - 30
        count = max(len(self.results), 1)
        slot = width / count
        peak = max((item.elapsed_ms for item in self.results), default=0) or 1
        return slot, height, peak, count

    def _index_at(self, position: QPoint) -> int | None:
        slot, _, _, _ = self._bar_geometry()
        index = int((position.x() - 60) / slot) if slot else -1
        return index if 0 <= index < len(self.results) else None

    def _bar_rect(self, index: int) -> QRectF:
        slot, height, peak, _ = self._bar_geometry()
        item = self.results[index]
        bar_width = max(slot * 0.7, 1.0)
        bar_height = max(height * item.elapsed_ms / peak, 2.0) if not item.skipped else 2.0
        x = 60 + index * slot + (slot - bar_width) / 2
        return QRectF(x, 10 + height - bar_height, bar_width, bar_height)

    def _show_tooltip(self, index: int, position: QPoint) -> None:
        item = self.results[index]
        service = item.service or "Unknown service"
        outcome = item.outcome.title()
        text = (
            f"<b>{item.case_name}</b><br>"
            f"Service: {service}<br>"
            f"Outcome: {outcome}<br>"
            f"Duration: {_format_duration(item.elapsed_ms)}"
        )
        QToolTip.showText(self.mapToGlobal(position), text, self)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt signature
        index = self._index_at(event.position().toPoint())
        if index != self._hovered_index:
            self._hovered_index = index
            self.update()
        if index is not None:
            self._show_tooltip(index, event.position().toPoint())
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt signature
        self._hovered_index = None
        QToolTip.hideText()
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt signature
        if not self.results:
            return
        index = self._index_at(event.position().toPoint())
        if index is not None:
            self._selected_index = index
            self.update()
            self.bar_clicked.emit(index)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.SURFACE))

        if not self.results:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "Run the suite to see timings"
            )
            return

        slot, height, peak, _ = self._bar_geometry()
        top = 10

        painter.setPen(QPen(QColor(theme.BORDER)))
        painter.setFont(self.font())
        for fraction in (0, 0.25, 0.5, 0.75, 1):
            y = top + height * (1 - fraction)
            painter.setPen(QPen(QColor(theme.BORDER)))
            painter.drawLine(60, int(y), self.width() - 10, int(y))
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(
                QRectF(0, y - 9, 52, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                f"{int(peak * fraction)} ms",
            )

        bar_width = max(slot * 0.7, 1.0)
        for index, item in enumerate(self.results):
            ratio = item.elapsed_ms / peak
            bar_height = max(height * ratio, 2.0) if not item.skipped else 2.0
            x = 60 + index * slot + (slot - bar_width) / 2
            y = top + height - bar_height
            rect = QRectF(x, y, bar_width, bar_height)
            color = _outcome_color(item.outcome)
            if index == self._hovered_index:
                color = color.lighter(115)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            painter.drawRoundedRect(rect, min(6.0, bar_width / 2), min(6.0, bar_width / 2))
            if index == self._selected_index:
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(QColor(theme.TEXT), 2))
                painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 5, 5)

        painter.setPen(QPen(QColor(theme.TEXT)))
        painter.drawLine(60, top + height, self.width() - 10, top + height)
        painter.drawText(
            QRectF(60, top + height + 4, self.width() - 70, 20),
            Qt.AlignmentFlag.AlignLeft,
            f"{len(self.results)} cases · slowest {_format_duration(peak)}",
        )
        painter.end()


class OutcomeBar(QWidget):
    """A single stacked bar showing the pass/fail/error/skip split."""

    def __init__(self) -> None:
        super().__init__()
        self.counts: list[tuple[str, int]] = []
        self.setFixedHeight(26)

    def set_counts(self, counts: list[tuple[str, int]]) -> None:
        self.counts = counts
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        total = sum(count for _, count in self.counts)
        if not total:
            painter.fillRect(self.rect(), QColor(theme.SURFACE_ALT))
            painter.end()
            return
        x = 0.0
        for name, count in self.counts:
            if not count:
                continue
            width = self.width() * count / total
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(_outcome_color(name)))
            painter.drawRect(QRectF(x, 0, width, self.height()))
            if width > 34:
                painter.setPen(QPen(QColor("#ffffff")))
                painter.drawText(
                    QRectF(x, 0, width, self.height()),
                    Qt.AlignmentFlag.AlignCenter,
                    f"{count}",
                )
            x += width
        painter.end()

    def refresh_theme(self) -> None:
        self.update()


class OutcomeLegend(QWidget):
    """Compact, theme-aware key for the outcome colours."""

    def __init__(self) -> None:
        super().__init__()
        self.counts: list[tuple[str, int]] = []
        self.setFixedHeight(24)

    def set_counts(self, counts: list[tuple[str, int]]) -> None:
        self.counts = counts
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(self.font())
        x = 0.0
        for name, count in self.counts:
            label = f"{name.title()} {count}"
            text_width = painter.fontMetrics().horizontalAdvance(label)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(_outcome_color(name)))
            painter.drawRoundedRect(QRectF(x, 7, 8, 8), 4, 4)
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(
                QRectF(x + 13, 0, text_width, 24),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )
            x += text_width + 29
        painter.end()

    def refresh_theme(self) -> None:
        self.update()


class RunVisualizer(QWidget):
    """Full analysis pane for one suite run."""

    case_selected = pyqtSignal(str)

    def __init__(self) -> None:
        super().__init__()
        self.result: SuiteResult | None = None
        layout = QVBoxLayout(self)

        tiles = QHBoxLayout()
        self.tiles = {
            "total": SummaryTile("Total"),
            "passed": SummaryTile("Passed", "passed"),
            "failed": SummaryTile("Failed", "failed"),
            "errored": SummaryTile("Errors", "errored"),
            "skipped": SummaryTile("Skipped", "skipped"),
            "pass_rate": SummaryTile("Pass rate"),
            "duration": SummaryTile("Duration"),
        }
        for tile in self.tiles.values():
            tiles.addWidget(tile)
        layout.addLayout(tiles)

        self.outcome_bar = OutcomeBar()
        layout.addWidget(self.outcome_bar)
        self.outcome_legend = OutcomeLegend()
        layout.addWidget(self.outcome_legend)

        chart_box = QWidget()
        chart_layout = QVBoxLayout(chart_box)
        chart_layout.setContentsMargins(0, 0, 0, 0)
        self.chart = TimingChart()
        self.chart.bar_clicked.connect(self._bar_clicked)
        chart_layout.addWidget(self.chart)
        chart_box.setMinimumHeight(260)

        lower = QWidget()
        lower_layout = QHBoxLayout(lower)

        slowest_box = QGroupBox("Slowest cases")
        slowest_layout = QVBoxLayout(slowest_box)
        self.slowest = QTableWidget(0, 2)
        self.slowest.setHorizontalHeaderLabels(["Case", "ms"])
        self._configure_scrollable_table(self.slowest, [280, 100])
        self.slowest.verticalHeader().setVisible(False)
        enable_result_sorting(self.slowest)
        attach_table_empty_state(
            self.slowest,
            icon_name="chart",
            title="No timings yet",
            guidance="Run a suite and the slowest cases are ranked here.",
        )
        slowest_layout.addWidget(self.slowest)
        lower_layout.addWidget(slowest_box)

        service_box = QGroupBox("By service")
        service_layout = QVBoxLayout(service_box)
        self.by_service = QTableWidget(0, 4)
        self.by_service.setHorizontalHeaderLabels(["Service", "Pass", "Fail", "Avg ms"])
        self._configure_scrollable_table(self.by_service, [240, 80, 80, 110])
        self.by_service.verticalHeader().setVisible(False)
        enable_result_sorting(self.by_service)
        attach_table_empty_state(
            self.by_service,
            icon_name="globe",
            title="No services yet",
            guidance="Run a suite to compare pass rate and average latency per service.",
        )
        service_layout.addWidget(self.by_service)
        lower_layout.addWidget(service_box)

        failures_box = QGroupBox("Failed assertions")
        failures_layout = QVBoxLayout(failures_box)
        self.failures = QTableWidget(0, 2)
        self.failures.setHorizontalHeaderLabels(["Case", "Reason"])
        self._configure_scrollable_table(self.failures, [220, 440])
        self.failures.verticalHeader().setVisible(False)
        enable_result_sorting(self.failures)
        attach_table_empty_state(
            self.failures,
            icon_name="verify",
            title="No failed assertions",
            guidance="Every assertion in this run passed.",
        )
        failures_layout.addWidget(self.failures)
        lower_layout.addWidget(failures_box)

        lower.setMinimumHeight(300)
        self.analysis_accordion = AccordionScrollArea()
        self.timing_section = self.analysis_accordion.add_section(
            "Response time per case (click a bar to open its result)",
            chart_box,
            expanded=True,
            summary="No timings",
        )
        self.breakdown_section = self.analysis_accordion.add_section(
            "Run breakdowns",
            lower,
            expanded=True,
            summary="No results",
        )
        layout.addWidget(self.analysis_accordion, 1)

    @staticmethod
    def _configure_scrollable_table(
        table: QTableWidget, column_widths: list[int]
    ) -> None:
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        for column, width in enumerate(column_widths):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(column, width)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setHorizontalScrollMode(
            QTableWidget.ScrollMode.ScrollPerPixel
        )

    def _bar_clicked(self, index: int) -> None:
        if self.result and 0 <= index < len(self.result.results):
            self.case_selected.emit(self.result.results[index].case_id)

    def set_result(self, result: SuiteResult | None) -> None:
        self.result = result
        if result is None:
            for tile in self.tiles.values():
                tile.set_value("0")
            self.chart.set_results([])
            self.outcome_bar.set_counts([])
            self.outcome_legend.set_counts([])
            for table in (self.slowest, self.by_service, self.failures):
                table.setRowCount(0)
            self.timing_section.set_summary("No timings")
            self.breakdown_section.set_summary("No results")
            return

        self.tiles["total"].set_value(str(result.total))
        self.tiles["passed"].set_value(str(result.passed))
        self.tiles["failed"].set_value(str(result.failed))
        self.tiles["errored"].set_value(str(result.errored))
        self.tiles["skipped"].set_value(str(result.skipped))
        self.tiles["pass_rate"].set_value(f"{result.pass_rate:.0f}%")
        self.tiles["duration"].set_value(f"{result.duration_ms / 1000:.2f}s")

        self.chart.set_results(result.results)
        self.timing_section.set_summary(
            f"{len(result.results)} cases · {_format_duration(result.duration_ms)}"
        )
        failed_total = result.failed + result.errored
        self.breakdown_section.set_summary(
            f"{failed_total} issue{'s' if failed_total != 1 else ''}"
        )
        counts = [
            ("PASS", result.passed),
            ("FAIL", result.failed),
            ("ERROR", result.errored),
            ("SKIPPED", result.skipped),
        ]
        self.outcome_bar.set_counts(counts)
        self.outcome_legend.set_counts(counts)

        slowest = result.slowest()
        self.slowest.setSortingEnabled(False)
        self.slowest.setRowCount(len(slowest))
        for row, case in enumerate(slowest):
            self.slowest.setItem(row, 0, QTableWidgetItem(case.case_name))
            self.slowest.setItem(row, 1, NumericTableItem(case.elapsed_ms))
        self.slowest.setSortingEnabled(True)

        services: dict[str, list[CaseResult]] = {}
        for case in result.executed:
            services.setdefault(case.service or "—", []).append(case)
        self.by_service.setSortingEnabled(False)
        self.by_service.setRowCount(len(services))
        for row, (service, cases) in enumerate(sorted(services.items())):
            passed = sum(1 for item in cases if item.passed and not item.error)
            average = sum(item.elapsed_ms for item in cases) / len(cases)
            self.by_service.setItem(row, 0, QTableWidgetItem(service))
            self.by_service.setItem(row, 1, NumericTableItem(passed))
            self.by_service.setItem(row, 2, NumericTableItem(len(cases) - passed))
            self.by_service.setItem(row, 3, NumericTableItem(average, f"{average:.0f}"))
        self.by_service.setSortingEnabled(True)

        rows: list[tuple[str, str]] = []
        for case in result.results:
            if case.error:
                rows.append((case.case_name, case.error))
            elif not case.passed and not case.skipped:
                if not case.failed_assertions:
                    rows.append(
                        (
                            case.case_name,
                            f"status {case.status_code}, expected {case.expected_status}",
                        )
                    )
                for failure in case.failed_assertions:
                    rows.append(
                        (case.case_name, f"{failure.assertion.label} — {failure.detail}")
                    )
        self.failures.setRowCount(len(rows))
        self.failures.setSortingEnabled(False)
        for row, (name, reason) in enumerate(rows):
            self.failures.setItem(row, 0, QTableWidgetItem(name))
            self.failures.setItem(row, 1, QTableWidgetItem(reason))
        self.failures.setSortingEnabled(True)

    def refresh_theme(self) -> None:
        self.chart.update()
        self.outcome_bar.refresh_theme()
        self.outcome_legend.refresh_theme()
        self.update()
