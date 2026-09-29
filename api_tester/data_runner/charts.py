"""Lightweight QPainter-drawn charts for the Data Runner Run tab.

The app deliberately carries no charting dependency (only PyQt6/requests/msal
are in ``requirements.txt``), so these widgets follow the same hand-drawn
convention as :mod:`api_tester.icons` rather than pulling in QtCharts or a
plotting library — they are small, bounded-data visualisations, not a
general-purpose plotting surface.
"""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QSizePolicy, QWidget

from api_tester import theme

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
    """A horizontal stacked bar of passed/failed/errored/invalid/skipped counts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._counts: dict[str, int] = {key: 0 for key in _OUTCOME_ORDER}
        self.setMinimumHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_counts(self, counts: dict[str, int]) -> None:
        self._counts = {key: int(counts.get(key, 0)) for key in _OUTCOME_ORDER}
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        total = sum(self._counts.values())
        bar_rect = QRectF(0.0, 4.0, float(self.width()), 18.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.BORDER))
        painter.drawRoundedRect(bar_rect, 4.0, 4.0)

        if total > 0:
            x = bar_rect.left()
            for key in _OUTCOME_ORDER:
                count = self._counts[key]
                if count <= 0:
                    continue
                width = bar_rect.width() * (count / total)
                segment = QRectF(x, bar_rect.top(), width, bar_rect.height())
                painter.setBrush(QColor(_OUTCOME_COLORS[key]))
                painter.drawRect(segment)
                x += width

        legend_y = 34
        legend_x = 0.0
        row_height = 16.0
        max_width = float(self.width())
        painter.setPen(QPen(QColor(theme.TEXT)))
        font = painter.font()
        font.setPointSize(max(8, font.pointSize() - 1))
        painter.setFont(font)
        for key in _OUTCOME_ORDER:
            count = self._counts[key]
            text = f"{_OUTCOME_LABELS[key]}: {count}"
            text_width = painter.fontMetrics().horizontalAdvance(text)
            entry_width = 14 + text_width + 16
            if legend_x > 0.0 and legend_x + entry_width > max_width:
                legend_x = 0.0
                legend_y += row_height
            swatch = QRectF(legend_x, legend_y, 10.0, 10.0)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_OUTCOME_COLORS[key]))
            painter.drawRoundedRect(swatch, 2.0, 2.0)
            painter.setPen(QPen(QColor(theme.TEXT)))
            painter.drawText(int(legend_x + 14), int(legend_y + 9), text)
            legend_x += entry_width


class SparklineChart(QWidget):
    """A bounded-history line or bar sparkline for one numeric series."""

    def __init__(
        self,
        *,
        mode: str = "line",
        color: str = "#0878f9",
        max_points: int = 120,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if mode not in ("line", "bar"):
            raise ValueError("mode must be 'line' or 'bar'")
        self._mode = mode
        self._color = color
        self._max_points = max_points
        self._values: list[float] = []
        self.setMinimumHeight(70)
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
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.SURFACE_ALT))
        painter.drawRoundedRect(rect, 4.0, 4.0)

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

        count = len(self._values)
        if self._mode == "bar":
            slot_width = plot.width() / count
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self._color))
            for index, value in enumerate(self._values):
                x = plot.left() + index * slot_width
                top = y_for(value)
                painter.drawRect(QRectF(x + slot_width * 0.15, top, slot_width * 0.7, plot.bottom() - top))
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
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._series = series
        self._max_points = max_points
        self._unit = unit
        self._values: dict[str, list[float]] = {name: [] for name, _color in series}
        self.setMinimumHeight(190)
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

        legend_height = 24.0
        plot = rect.adjusted(42.0, legend_height + 8.0, -12.0, -24.0)
        all_values = [value for values in self._values.values() for value in values]

        painter.setPen(QPen(QColor(theme.BORDER), 1.0))
        for index in range(5):
            y = plot.top() + (plot.height() * index / 4)
            painter.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))

        if not all_values:
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Waiting for live samples")
            self._draw_legend(painter)
            return

        maximum = max(all_values) or 1.0
        scale_max = maximum * 1.1
        painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
        font = painter.font()
        font.setPointSize(max(7, font.pointSize() - 1))
        painter.setFont(font)
        for index in range(5):
            value = scale_max * (4 - index) / 4
            label = f"{value:.0f}{self._unit}"
            y = plot.top() + (plot.height() * index / 4)
            painter.drawText(QRectF(0.0, y - 8.0, 36.0, 16.0), Qt.AlignmentFlag.AlignRight, label)

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
            painter.setPen(QPen(QColor(color), 2.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

        self._draw_legend(painter)

    def _draw_legend(self, painter: QPainter) -> None:
        x = max(8.0, float(self.width()) - 12.0)
        entries: list[tuple[str, str, int]] = []
        for name, color in reversed(self._series):
            width = painter.fontMetrics().horizontalAdvance(name) + 22
            x -= width
            entries.append((name, color, int(x)))
        for name, color, entry_x in reversed(entries):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF(float(entry_x), 8.0, 7.0, 7.0))
            painter.setPen(QPen(QColor(theme.TEXT_MUTED)))
            painter.drawText(entry_x + 11, 16, name)


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
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.SURFACE_ALT))
        painter.drawRoundedRect(rect, 4.0, 4.0)

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
