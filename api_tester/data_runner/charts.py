"""Lightweight QPainter-drawn charts for the Data Runner Run tab.

The app deliberately carries no charting dependency (only PyQt6/requests/msal
are in ``requirements.txt``), so these widgets follow the same hand-drawn
convention as :mod:`api_tester.icons` rather than pulling in QtCharts or a
plotting library — they are small, bounded-data visualisations, not a
general-purpose plotting surface.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from api_tester import theme

# Categorical fallback palette for charts whose buckets are discovered at
# runtime (error categories, status codes) and so have no fixed colour.
_CATEGORY_PALETTE = (
    "#ef3340",
    "#f59e0b",
    "#0878f9",
    "#6b3fa0",
    "#16a34a",
    "#0ea5e9",
    "#db2777",
    "#65758b",
)


def _tinted(color: str, alpha: int) -> QColor:
    """Returns ``color`` at ``alpha`` for area/bar fills under a solid stroke."""
    tint = QColor(color)
    tint.setAlpha(alpha)
    return tint


def _draw_plot_panel(
    painter: QPainter,
    rect: QRectF,
    *,
    dotted: bool = False,
    rows: int = 4,
    columns: int = 6,
) -> None:
    """Draws the checked plot panel shared by every chart in this module.

    A plain white plot area makes it hard to judge a value against the axis,
    so each chart sits on a soft panel ruled in both directions; the grid is
    the reference the eye needs, and keeping it in one helper is what makes
    the charts look like one family.
    """
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(theme.SURFACE_ALT))
    painter.drawRoundedRect(rect, 6.0, 6.0)

    grid_pen = QPen(QColor(theme.BORDER), 1.0)
    if dotted:
        grid_pen.setStyle(Qt.PenStyle.DotLine)
    painter.setPen(grid_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for index in range(1, rows):
        y = rect.top() + rect.height() * index / rows
        painter.drawLine(QPointF(rect.left() + 2.0, y), QPointF(rect.right() - 2.0, y))
    for index in range(1, columns):
        x = rect.left() + rect.width() * index / columns
        painter.drawLine(QPointF(x, rect.top() + 2.0), QPointF(x, rect.bottom() - 2.0))
    painter.restore()


_LEGEND_SWATCH_GAP = 14.0
_LEGEND_VALUE_GAP = 12.0


def _legend_font(painter: QPainter):
    font = painter.font()
    font.setPointSize(max(8, font.pointSize() - 1))
    return font


def _legend_width(painter: QPainter, entries: list[tuple[str, str, str]]) -> float:
    """Width the legend needs to show every label and value unelided.

    The donut/pie split used to be a fixed fraction of the widget, which
    elided "Cancelled" down to "Can…" in the narrow load-testing rail. Giving
    the legend what it measures and the chart the remainder keeps both
    readable at any width.
    """
    if not entries:
        return 0.0
    painter.save()
    painter.setFont(_legend_font(painter))
    metrics = painter.fontMetrics()
    label = max(metrics.horizontalAdvance(label) for label, _value, _color in entries)
    value = max(metrics.horizontalAdvance(value) for _label, value, _color in entries)
    painter.restore()
    return _LEGEND_SWATCH_GAP + label + _LEGEND_VALUE_GAP + value


def _draw_side_legend(
    painter: QPainter,
    rect: QRectF,
    entries: list[tuple[str, str, str]],
) -> None:
    """Draws a right-hand legend of ``(label, value, colour)`` rows.

    Shared by the donut and the pie so a slice and its number are read
    together instead of forcing a colour-to-label lookup on the chart itself.
    """
    if not entries:
        return
    row_height = min(22.0, rect.height() / len(entries))
    painter.setFont(_legend_font(painter))
    metrics = painter.fontMetrics()
    value_width = max(metrics.horizontalAdvance(value) for _label, value, _color in entries)
    top = rect.top() + max(0.0, (rect.height() - row_height * len(entries)) / 2)

    for index, (label, value, color) in enumerate(entries):
        y = top + index * row_height
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(rect.left(), y + row_height / 2 - 4.0, 8.0, 8.0))

        label_rect = QRectF(
            rect.left() + _LEGEND_SWATCH_GAP,
            y,
            max(10.0, rect.width() - _LEGEND_SWATCH_GAP - value_width - _LEGEND_VALUE_GAP),
            row_height,
        )
        painter.setPen(QPen(QColor(theme.TEXT)))
        painter.drawText(
            label_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(label_rect.width())),
        )
        painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
        painter.drawText(
            QRectF(rect.right() - value_width, y, value_width, row_height),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            value,
        )

_OUTCOME_ORDER = ("passed", "failed", "error", "invalid", "skipped", "cancelled")
_OUTCOME_LABELS = {
    "passed": "Passed",
    "failed": "Failed",
    "error": "Errored",
    "invalid": "Invalid",
    "skipped": "Skipped",
    "cancelled": "Cancelled",
}
_OUTCOME_COLORS = {
    "passed": "#16a34a",
    "failed": "#dc2626",
    "error": "#dc2626",
    "invalid": "#d97706",
    "skipped": "#65758b",
    "cancelled": "#65758b",
}


class OutcomeBreakdownChart(QWidget):
    """A donut of passed/failed/errored/invalid/skipped counts, legend at right.

    The stacked bar this replaced could not show a total, and thin segments
    were unreadable once one outcome dominated; a donut carries the total in
    its hole and gives every outcome a labelled legend row.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._counts: dict[str, int] = {key: 0 for key in _OUTCOME_ORDER}
        self.setMinimumHeight(136)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_counts(self, counts: dict[str, int]) -> None:
        self._counts = {key: int(counts.get(key, 0)) for key in _OUTCOME_ORDER}
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        total = sum(self._counts.values())
        entries = [
            (_OUTCOME_LABELS[key], f"{self._counts[key]:,}", _OUTCOME_COLORS[key])
            for key in _OUTCOME_ORDER
        ]
        # No plot panel is drawn: a ring has no axis for a grid to reference.
        legend_width = min(_legend_width(painter, entries), rect.width() * 0.62)
        diameter = max(56.0, min(rect.height() - 8.0, rect.width() - legend_width - 24.0))
        ring = QRectF(8.0, (rect.height() - diameter) / 2, diameter, diameter)
        thickness = ring.width() * 0.26

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BORDER))
        painter.drawEllipse(ring)

        if total > 0:
            # Qt angles are in 1/16th degrees, measured counter-clockwise from
            # 3 o'clock; starting at 90 puts the first slice at 12 o'clock.
            start = 90 * 16
            for key in _OUTCOME_ORDER:
                count = self._counts[key]
                if count <= 0:
                    continue
                span = int(-360 * 16 * (count / total))
                painter.setBrush(QColor(_OUTCOME_COLORS[key]))
                painter.drawPie(ring, start, span)
                start += span

        hole = ring.adjusted(thickness, thickness, -thickness, -thickness)
        painter.setBrush(QColor(theme.SURFACE))
        painter.drawEllipse(hole)

        painter.setPen(QPen(QColor(theme.TEXT)))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(max(9, font.pointSize() + 1))
        painter.setFont(font)
        painter.drawText(
            QRectF(hole.left(), hole.top() + hole.height() * 0.18, hole.width(), hole.height() * 0.44),
            Qt.AlignmentFlag.AlignCenter,
            f"{total:,}",
        )
        font.setBold(False)
        font.setPointSize(max(7, font.pointSize() - 3))
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
        painter.drawText(
            QRectF(hole.left(), hole.center().y() + 2.0, hole.width(), hole.height() * 0.36),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            "total",
        )

        legend_left = ring.right() + 16.0
        _draw_side_legend(
            painter,
            QRectF(legend_left, rect.top() + 4.0, max(40.0, rect.right() - legend_left), rect.height() - 8.0),
            entries,
        )


class PieChart(QWidget):
    """A pie of labelled buckets with a right-hand legend.

    Shares :meth:`set_entries` with :class:`LabeledBarChart` so a call site can
    swap one for the other; used where the question is "what share of the
    failures is this?" rather than "how do these magnitudes compare?".
    """

    def __init__(
        self,
        *,
        max_entries: int = 8,
        colors: tuple[str, ...] = _CATEGORY_PALETTE,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._max_entries = max_entries
        self._palette = colors
        self._entries: list[tuple[str, float, str | None]] = []
        self.setMinimumHeight(136)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_entries(
        self,
        entries: dict[str, float] | list[tuple[str, float]],
        *,
        colors: dict[str, str] | None = None,
        sort_descending: bool = True,
    ) -> None:
        items = list(entries.items()) if isinstance(entries, dict) else list(entries)
        if sort_descending:
            items.sort(key=lambda pair: pair[1], reverse=True)
        items = items[: self._max_entries]
        self._entries = [(label, float(value), (colors or {}).get(label)) for label, value in items]
        self.update()

    def _color_for(self, index: int, override: str | None) -> str:
        return override or self._palette[index % len(self._palette)]

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))

        if not self._entries:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No data yet")
            return

        legend_entries = [
            (label, f"{value:,.0f}", self._color_for(index, override))
            for index, (label, value, override) in enumerate(self._entries)
        ]
        legend_width = min(_legend_width(painter, legend_entries), rect.width() * 0.62)
        diameter = max(56.0, min(rect.height() - 8.0, rect.width() - legend_width - 20.0))
        # No plot panel is drawn: a pie has no axis for a grid to reference, and
        # without the panel the slices fill the circle rather than inset from it.
        slice_rect = QRectF(8.0, (rect.height() - diameter) / 2, diameter, diameter)

        total = sum(value for _label, value, _color in self._entries)
        painter.setPen(Qt.PenStyle.NoPen)
        if total <= 0:
            painter.setBrush(QColor(theme.BORDER))
            painter.drawEllipse(slice_rect)
        else:
            start = 90 * 16
            for index, (_label, value, override) in enumerate(self._entries):
                if value <= 0:
                    continue
                span = int(-360 * 16 * (value / total))
                painter.setBrush(QColor(self._color_for(index, override)))
                painter.drawPie(slice_rect, start, span)
                start += span

        legend_left = slice_rect.right() + 16.0
        _draw_side_legend(
            painter,
            QRectF(legend_left, rect.top() + 4.0, max(40.0, rect.right() - legend_left), rect.height() - 8.0),
            legend_entries,
        )


class SparklineChart(QWidget):
    """A bounded-history line or bar sparkline for one numeric series."""

    def __init__(
        self,
        *,
        mode: str = "line",
        color: str = "#0878f9",
        max_points: int = 120,
        dotted_grid: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if mode not in ("line", "bar"):
            raise ValueError("mode must be 'line' or 'bar'")
        self._mode = mode
        self._color = color
        self._max_points = max_points
        self._dotted_grid = dotted_grid
        self._values: list[float] = []
        self.setMinimumHeight(86)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def clear(self) -> None:
        self._values = []
        self.update()

    def append(self, value: float) -> None:
        self._values.append(float(value))
        if len(self._values) > self._max_points:
            self._values = self._values[-self._max_points :]
        self.update()

    def set_values(self, values: list[float]) -> None:
        self._values = [float(v) for v in values[-self._max_points :]]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        _draw_plot_panel(painter, rect, dotted=self._dotted_grid)

        if not self._values:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No data yet")
            return

        maximum = max(self._values) or 1.0
        minimum = min(0.0, min(self._values))
        span = (maximum - minimum) or 1.0
        padding = 6.0
        plot = rect.adjusted(padding, padding, -padding, -padding)

        def y_for(value: float) -> float:
            return plot.bottom() - ((value - minimum) / span) * plot.height()

        # A vertical gradient keeps the fill from reading as a solid block of
        # colour at high bar counts while still anchoring every bar to the axis.
        gradient = QLinearGradient(plot.left(), plot.top(), plot.left(), plot.bottom())
        gradient.setColorAt(0.0, _tinted(self._color, 235))
        gradient.setColorAt(1.0, _tinted(self._color, 70))

        count = len(self._values)
        if self._mode == "bar":
            slot_width = plot.width() / count
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            # Below ~2px a gap per bar erases the series, so dense runs are
            # drawn as a continuous filled profile instead.
            gap = slot_width * 0.3 if slot_width >= 3.0 else 0.0
            for index, value in enumerate(self._values):
                x = plot.left() + index * slot_width
                top = y_for(value)
                height = max(1.0, plot.bottom() - top)
                painter.drawRect(QRectF(x + gap / 2, top, max(1.0, slot_width - gap), height))
        else:
            path = QPainterPath()
            step = plot.width() / max(1, count - 1) if count > 1 else 0.0
            for index, value in enumerate(self._values):
                x = plot.left() + index * step
                y = y_for(value)
                if index == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

            area = QPainterPath(path)
            area.lineTo(plot.left() + (count - 1) * step, plot.bottom())
            area.lineTo(plot.left(), plot.bottom())
            area.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawPath(area)

            painter.setPen(QPen(QColor(self._color), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)


class MultiSeriesLineChart(QWidget):
    """Compact multi-series chart with a shared scale, grid, and legend."""

    def __init__(
        self,
        series: tuple[tuple[str, str], ...],
        *,
        max_points: int = 120,
        unit: str = "",
        style: str = "line",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if style not in ("line", "bar"):
            raise ValueError("style must be 'line' or 'bar'")
        self._series = series
        self._max_points = max_points
        self._unit = unit
        self._style = style
        self._values: dict[str, list[float]] = {name: [] for name, _color in series}
        # Kept low deliberately: a taller minimum than the group box can grant
        # makes the widget overflow its parent, and the box clips the plot.
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def clear(self) -> None:
        for values in self._values.values():
            values.clear()
        self.update()

    def append(self, values: dict[str, float]) -> None:
        for name, _color in self._series:
            series_values = self._values[name]
            series_values.append(float(values.get(name, 0.0)))
            if len(series_values) > self._max_points:
                self._values[name] = series_values[-self._max_points :]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))

        all_values = [value for values in self._values.values() for value in values]
        scale_max = (max(all_values) or 1.0) * 1.1 if all_values else 1.0

        legend_rows = self._legend_rows(painter, rect.width())
        legend_height = self._legend_height(painter, legend_rows)

        axis_font = painter.font()
        axis_font.setPointSize(max(7, axis_font.pointSize() - 1))
        painter.save()
        painter.setFont(axis_font)
        metrics = painter.fontMetrics()
        axis_labels = [f"{scale_max * (4 - index) / 4:.0f}{self._unit}" for index in range(5)]
        axis_width = max(metrics.horizontalAdvance(label) for label in axis_labels)
        axis_height = float(metrics.height())
        painter.restore()

        # Every reserve is measured: the top must clear the wrapped legend and
        # half of the topmost axis label, the bottom half of the lowest one, so
        # neither is clipped at any widget size.
        top = max(legend_height + 6.0, axis_height / 2.0)
        bottom = axis_height / 2.0 + 2.0
        show_axis = rect.height() - top - bottom >= 56.0 and rect.width() >= axis_width + 90.0
        left = min(axis_width + 10.0, rect.width() * 0.4) if show_axis else 8.0
        plot = QRectF(
            left,
            top,
            max(24.0, rect.width() - left - 12.0),
            max(24.0, rect.height() - top - bottom),
        )

        _draw_plot_panel(painter, plot)

        if not all_values:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Waiting for live samples")
            self._draw_legend(painter, legend_rows)
            return

        if show_axis:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.setFont(axis_font)
            for index, label in enumerate(axis_labels):
                y = plot.top() + (plot.height() * index / 4)
                painter.drawText(
                    QRectF(0.0, y - axis_height / 2.0, left - 6.0, axis_height),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    label,
                )

        if self._style == "bar":
            self._draw_bars(painter, plot, scale_max)
        else:
            self._draw_lines(painter, plot, scale_max)

        self._draw_legend(painter, legend_rows)

    def _draw_lines(self, painter: QPainter, plot: QRectF, scale_max: float) -> None:
        for name, color in self._series:
            values = self._values[name]
            if not values:
                continue
            path = QPainterPath()
            step = plot.width() / max(1, len(values) - 1) if len(values) > 1 else 0.0
            for index, value in enumerate(values):
                x = plot.left() + index * step
                y = plot.bottom() - (value / scale_max) * plot.height()
                if index == 0:
                    path.moveTo(x, y)
                else:
                    path.lineTo(x, y)

            area = QPainterPath(path)
            area.lineTo(plot.left() + (len(values) - 1) * step, plot.bottom())
            area.lineTo(plot.left(), plot.bottom())
            area.closeSubpath()
            gradient = QLinearGradient(plot.left(), plot.top(), plot.left(), plot.bottom())
            gradient.setColorAt(0.0, _tinted(color, 110))
            gradient.setColorAt(1.0, _tinted(color, 20))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(gradient)
            painter.drawPath(area)

            painter.setPen(QPen(QColor(color), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

    def _draw_bars(self, painter: QPainter, plot: QRectF, scale_max: float) -> None:
        """Draws each sample as a filled bar, series side by side per slot."""
        count = max((len(values) for values in self._values.values()), default=0)
        if count <= 0:
            return
        slot_width = plot.width() / count
        series_count = max(1, len(self._series))
        gap = slot_width * 0.3 if slot_width >= 3.0 * series_count else 0.0
        bar_width = max(1.0, (slot_width - gap) / series_count)

        painter.setPen(Qt.PenStyle.NoPen)
        for series_index, (name, color) in enumerate(self._series):
            gradient = QLinearGradient(plot.left(), plot.top(), plot.left(), plot.bottom())
            gradient.setColorAt(0.0, _tinted(color, 235))
            gradient.setColorAt(1.0, _tinted(color, 70))
            painter.setBrush(gradient)
            for index, value in enumerate(self._values[name]):
                x = plot.left() + index * slot_width + gap / 2 + series_index * bar_width
                top = plot.bottom() - (value / scale_max) * plot.height()
                painter.drawRect(QRectF(x, top, bar_width, max(1.0, plot.bottom() - top)))

    def _legend_rows(
        self, painter: QPainter, width: float
    ) -> list[list[tuple[str, str, float]]]:
        """Wraps the legend into as many rows as the width demands.

        A single right-aligned row silently ran off the left edge and collided
        with the axis labels once more than a few series were present, so
        entries flow onto additional rows and the plot gives up the height.
        """
        metrics = painter.fontMetrics()
        available = max(40.0, width - 24.0)
        rows: list[list[tuple[str, str, float]]] = []
        current: list[tuple[str, str, float]] = []
        used = 0.0
        for name, color in self._series:
            entry_width = min(metrics.horizontalAdvance(name) + 22.0, available)
            if current and used + entry_width > available:
                rows.append(current)
                current = []
                used = 0.0
            current.append((name, color, entry_width))
            used += entry_width
        if current:
            rows.append(current)
        return rows

    def _legend_height(
        self, painter: QPainter, rows: list[list[tuple[str, str, float]]]
    ) -> float:
        if not rows:
            return 0.0
        return 6.0 + len(rows) * (painter.fontMetrics().height() + 4.0)

    def _draw_legend(
        self, painter: QPainter, rows: list[list[tuple[str, str, float]]]
    ) -> None:
        metrics = painter.fontMetrics()
        row_height = metrics.height() + 4.0
        right = float(self.width()) - 12.0
        for row_index, row in enumerate(rows):
            x = max(8.0, right - sum(entry_width for _n, _c, entry_width in row))
            y = 6.0 + row_index * row_height
            for name, color, entry_width in row:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(color))
                painter.drawEllipse(QRectF(x, y + row_height / 2.0 - 3.5, 7.0, 7.0))
                painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
                text_width = max(10.0, entry_width - 11.0)
                painter.drawText(
                    QRectF(x + 11.0, y, text_width, row_height),
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                    metrics.elidedText(name, Qt.TextElideMode.ElideRight, int(text_width)),
                )
                x += entry_width


class LabeledBarChart(QWidget):
    """A compact horizontal bar chart for a handful of labeled values.

    Reused for status-code distributions, error-category breakdowns, and
    latency percentile bars: anywhere a small set of named buckets needs an
    at-a-glance comparison instead of a full aggregation table.
    """

    def __init__(
        self,
        *,
        color: str = "#0878f9",
        value_format: str = "{:.0f}",
        max_entries: int = 8,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._color = color
        self._value_format = value_format
        self._max_entries = max_entries
        self._entries: list[tuple[str, float, str | None]] = []
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_entries(
        self,
        entries: dict[str, float] | list[tuple[str, float]],
        *,
        colors: dict[str, str] | None = None,
        sort_descending: bool = True,
    ) -> None:
        items = list(entries.items()) if isinstance(entries, dict) else list(entries)
        if sort_descending:
            items.sort(key=lambda pair: pair[1], reverse=True)
        items = items[: self._max_entries]
        self._entries = [(label, float(value), (colors or {}).get(label)) for label, value in items]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))

        if not self._entries:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No data yet")
            return

        maximum = max(value for _label, value, _color in self._entries) or 1.0
        row_height = rect.height() / len(self._entries)
        label_width = 76.0
        value_width = 56.0
        font = painter.font()
        font.setPointSize(max(8, font.pointSize() - 1))
        painter.setFont(font)
        metrics = painter.fontMetrics()

        for index, (label, value, color_override) in enumerate(self._entries):
            row = QRectF(rect.left(), rect.top() + index * row_height, rect.width(), row_height)

            label_rect = QRectF(row.left(), row.top(), label_width, row.height())
            painter.setPen(QPen(QColor(theme.TEXT)))
            elided = metrics.elidedText(label, Qt.TextElideMode.ElideRight, int(label_width - 4))
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided)

            track = QRectF(
                row.left() + label_width,
                row.top() + row.height() * 0.28,
                row.width() - label_width - value_width,
                row.height() * 0.44,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.BORDER))
            painter.drawRoundedRect(track, 3.0, 3.0)

            fraction = value / maximum if maximum else 0.0
            bar = QRectF(track.left(), track.top(), track.width() * fraction, track.height())
            painter.setBrush(QColor(color_override or self._color))
            painter.drawRoundedRect(bar, 3.0, 3.0)

            value_rect = QRectF(row.right() - value_width, row.top(), value_width, row.height())
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(
                value_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                self._value_format.format(value),
            )


class ScatterChart(QWidget):
    """Bounded-history scatter of (x, y) samples.

    Used for latency-vs-row-number: a percentile summary or trend sparkline
    can hide clustering (e.g. a slow patch of rows); a scatter surfaces it.
    """

    def __init__(
        self,
        *,
        color: str = "#7c3aed",
        max_points: int = 400,
        unit: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._color = color
        self._max_points = max_points
        self._unit = unit
        self._points: list[tuple[float, float]] = []
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def clear(self) -> None:
        self._points = []
        self.update()

    def append(self, x: float, y: float) -> None:
        self._points.append((float(x), float(y)))
        if len(self._points) > self._max_points:
            self._points = self._points[-self._max_points :]
        self.update()

    def set_points(self, points: list[tuple[float, float]]) -> None:
        self._points = [(float(x), float(y)) for x, y in points[-self._max_points :]]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        _draw_plot_panel(painter, rect)

        if not self._points:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "No data yet")
            return

        xs = [x for x, _y in self._points]
        ys = [y for _x, y in self._points]
        x_min, x_max = min(xs), max(xs)
        y_max = max(ys) or 1.0
        x_span = (x_max - x_min) or 1.0
        y_span = y_max or 1.0
        padding = 8.0
        plot = rect.adjusted(padding, padding, -padding, -padding)

        font = painter.font()
        font.setPointSize(max(7, font.pointSize() - 1))
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
        painter.drawText(
            QRectF(plot.left(), 2.0, 120.0, 14.0),
            Qt.AlignmentFlag.AlignLeft,
            f"max {y_max:.0f}{self._unit}",
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(self._color))
        for x, y in self._points:
            px = plot.left() + ((x - x_min) / x_span) * plot.width()
            py = plot.bottom() - (y / y_span) * plot.height()
            painter.drawEllipse(QPointF(px, py), 2.2, 2.2)
