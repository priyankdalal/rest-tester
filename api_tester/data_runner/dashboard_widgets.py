"""Dashboard-style widgets for the Data Runner "Execute & review" step: a
checkmark wizard stepper and compact stat cards, matching the visual
language used elsewhere in the app (hand-drawn QPainter primitives, no new
dependency).
"""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from api_tester import theme

_CIRCLE_DIAMETER = 22.0


class WizardStepper(QWidget):
    """A horizontal checkmark stepper: circle-per-step, connected by a line,
    with the current step highlighted and finished steps ticked off."""

    stepClicked = pyqtSignal(int)

    def __init__(self, labels: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._labels = list(labels)
        self._current = 0
        self._completed: set[int] = set()
        self.setMinimumHeight(52)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_current(self, index: int) -> None:
        if 0 <= index < len(self._labels):
            self._current = index
            self.update()

    def set_completed(self, indices: set[int]) -> None:
        self._completed = set(indices)
        self.update()

    def _step_centers(self) -> list[float]:
        count = len(self._labels)
        if count == 0:
            return []
        margin = 28.0
        usable = max(1.0, self.width() - 2 * margin)
        if count == 1:
            return [self.width() / 2.0]
        return [margin + usable * (index / (count - 1)) for index in range(count)]

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        centers = self._step_centers()
        if not centers:
            return
        x = event.position().x()
        closest = min(range(len(centers)), key=lambda i: abs(centers[i] - x))
        self.stepClicked.emit(closest)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        centers = self._step_centers()
        if not centers:
            return
        y = 14.0
        radius = _CIRCLE_DIAMETER / 2.0

        # Connecting line, drawn first so circles paint over its ends.
        if len(centers) > 1:
            for index in range(len(centers) - 1):
                done = index in self._completed
                pen = QPen(QColor(theme.PASS if done else theme.BORDER), 2.0)
                painter.setPen(pen)
                painter.drawLine(int(centers[index] + radius), int(y), int(centers[index + 1] - radius), int(y))

        for index, center_x in enumerate(centers):
            rect = QRectF(center_x - radius, y - radius, _CIRCLE_DIAMETER, _CIRCLE_DIAMETER)
            is_current = index == self._current
            is_done = index in self._completed
            painter.setPen(Qt.PenStyle.NoPen)
            if is_done:
                painter.setBrush(QColor(theme.PASS))
            elif is_current:
                painter.setBrush(QColor(theme.PRIMARY))
            else:
                painter.setBrush(QColor(theme.SURFACE_ALT))
            painter.drawEllipse(rect)
            if not is_done and not is_current:
                painter.setPen(QPen(QColor(theme.BORDER_STRONG), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(rect)

            if is_done:
                # Checkmark.
                check_pen = QPen(QColor(theme.TEXT_INVERSE), 2.0)
                check_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                check_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(check_pen)
                path = QPainterPath()
                path.moveTo(center_x - 5.5, y)
                path.lineTo(center_x - 1.5, y + 4.0)
                path.lineTo(center_x + 5.5, y - 5.0)
                painter.drawPath(path)
            else:
                painter.setPen(QColor(theme.TEXT_INVERSE if is_current else theme.TEXT_MUTED))
                font = painter.font()
                font.setPointSize(max(7, font.pointSize() - 1))
                font.setBold(is_current)
                painter.setFont(font)
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(index + 1))

            label = self._labels[index]
            label_rect = QRectF(center_x - 70.0, y + radius + 4.0, 140.0, 16.0)
            painter.setPen(QColor(theme.TEXT if (is_current or is_done) else theme.TEXT_MUTED))
            font = painter.font()
            font.setBold(is_current)
            font.setPointSize(max(7, font.pointSize()))
            painter.setFont(font)
            painter.drawText(label_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, label)


class StatCard(QFrame):
    """A small bordered card showing one live metric: a caption, a big value,
    and an optional muted subtitle (e.g. a percentage or breakdown)."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dataRunnerStatCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        title_label = QLabel(title.upper())
        title_label.setObjectName("dataRunnerStatCardTitle")
        layout.addWidget(title_label)

        self.value_label = QLabel("–")
        self.value_label.setObjectName("dataRunnerStatCardValue")
        layout.addWidget(self.value_label)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("dataRunnerStatCardSubtitle")
        layout.addWidget(self.subtitle_label)

    def set_value(self, text: str, *, color: str | None = None) -> None:
        self.value_label.setText(text)
        self.value_label.setStyleSheet(f"color: {color};" if color else "")

    def set_subtitle(self, text: str) -> None:
        # The label stays in the layout even when empty: hiding it shrank the
        # cards that have no subtitle (Throughput, Remaining), and the row
        # centres its children vertically, so those cards sat misaligned
        # against the rest of the strip.
        self.subtitle_label.setText(text)


class ContextCard(QFrame):
    """A titled card used in the run's left-hand context rail (input source,
    execution template, mapping summary, run settings)."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dataRunnerContextCard")
        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(10, 8, 10, 10)
        self.outer_layout.setSpacing(6)

        title_label = QLabel(title.upper())
        title_label.setObjectName("dataRunnerContextCardTitle")
        self.outer_layout.addWidget(title_label)

        self.body = QVBoxLayout()
        self.body.setSpacing(4)
        self.outer_layout.addLayout(self.body)

    def clear_body(self) -> None:
        while self.body.count():
            child = self.body.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()
            layout = child.layout()
            if layout is not None:
                _clear_layout(layout)


def _clear_layout(layout) -> None:
    while layout.count():
        child = layout.takeAt(0)
        widget = child.widget()
        if widget is not None:
            widget.deleteLater()
        nested = child.layout()
        if nested is not None:
            _clear_layout(nested)
