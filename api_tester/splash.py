"""Native Qt splash artwork for Rest Tester."""

from __future__ import annotations

from collections.abc import Sequence

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import QSplashScreen, QWidget

from .icons import app_pixmap

SPLASH_SIZE = QSize(760, 600)
MINIMUM_VISIBLE_MS = 900

_CYAN = QColor("#1ddcff")
_BLUE = QColor("#516fff")
_VIOLET = QColor("#da39ff")
_TEXT = QColor("#f7f9ff")
_MUTED = QColor("#97adcc")
_FAINT = QColor("#7d9ecf")


def _color(value: QColor | str, alpha: int | None = None) -> QColor:
    result = QColor(value)
    if alpha is not None:
        result.setAlpha(alpha)
    return result


def _font(
    pixel_size: int,
    *,
    bold: bool = False,
    spacing: float = 0.0,
    family: str = "Segoe UI",
) -> QFont:
    result = QFont(family)
    result.setPixelSize(pixel_size)
    result.setWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
    if spacing:
        result.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    return result


def _text(
    painter: QPainter,
    rect: QRectF,
    value: str,
    *,
    size: int,
    color: QColor | str,
    bold: bool = False,
    spacing: float = 0.0,
    alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft,
    family: str = "Segoe UI",
) -> None:
    painter.setFont(_font(size, bold=bold, spacing=spacing, family=family))
    painter.setPen(QPen(_color(color)))
    painter.drawText(rect, alignment | Qt.AlignmentFlag.AlignVCenter, value)


def _draw_soft_orb(
    painter: QPainter,
    center: QPointF,
    radius: float,
    color: QColor,
    opacity: int,
) -> None:
    gradient = QRadialGradient(center, radius)
    gradient.setColorAt(0.0, _color(color, opacity))
    gradient.setColorAt(0.55, _color(color, opacity // 3))
    gradient.setColorAt(1.0, _color(color, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawEllipse(center, radius, radius)


def _draw_chip(
    painter: QPainter,
    rect: QRectF,
    label: str,
    color: QColor,
    *,
    dot: bool = False,
    opacity: int = 115,
) -> None:
    painter.setPen(QPen(_color(color, opacity), 1.0))
    painter.setBrush(_color(color, 18))
    painter.drawRoundedRect(rect, rect.height() / 2.0, rect.height() / 2.0)
    text_rect = rect.adjusted(12.0 if not dot else 27.0, 0.0, -8.0, 0.0)
    if dot:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_color(color, 220))
        painter.drawEllipse(QPointF(rect.left() + 16.0, rect.center().y()), 3.2, 3.2)
    _text(
        painter,
        text_rect,
        label,
        size=8,
        color=_color(color, 180),
        bold=True,
        spacing=1.2,
    )


def _draw_background(painter: QPainter) -> None:
    background = QLinearGradient(0.0, 0.0, 760.0, 600.0)
    background.setColorAt(0.0, QColor("#030611"))
    background.setColorAt(0.48, QColor("#0b0a22"))
    background.setColorAt(1.0, QColor("#041427"))
    painter.fillRect(QRectF(0.0, 0.0, 760.0, 600.0), background)

    _draw_soft_orb(painter, QPointF(380.0, 275.0), 330.0, _BLUE, 68)
    _draw_soft_orb(painter, QPointF(70.0, 110.0), 155.0, _CYAN, 22)
    _draw_soft_orb(painter, QPointF(690.0, 455.0), 170.0, _VIOLET, 24)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_color("#7599e1", 34))
    for y in range(1, 600, 22):
        for x in range(1, 760, 22):
            painter.drawEllipse(QPointF(float(x), float(y)), 0.7, 0.7)


def _draw_ambient_api_details(painter: QPainter) -> None:
    _text(
        painter,
        QRectF(29.0, 82.0, 120.0, 14.0),
        "POST /endpoint",
        size=8,
        color=_color(_FAINT, 48),
        family="Consolas",
    )
    _text(
        painter,
        QRectF(29.0, 145.0, 145.0, 14.0),
        "Authorization: Bearer",
        size=8,
        color=_color(_FAINT, 48),
        family="Consolas",
    )
    _text(
        painter,
        QRectF(622.0, 419.0, 120.0, 14.0),
        "HTTP/1.1 200 OK",
        size=8,
        color=_color(_FAINT, 48),
        family="Consolas",
    )
    _text(
        painter,
        QRectF(651.0, 483.0, 100.0, 14.0),
        "{ valid: true }",
        size=8,
        color=_color(_FAINT, 48),
        family="Consolas",
    )

    painter.setPen(QPen(_color("#6997d5", 44), 0.8))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(28.0, 106.0), QPointF(124.0, 106.0))
    painter.drawLine(QPointF(28.0, 122.0), QPointF(96.0, 122.0))
    painter.drawLine(QPointF(28.0, 138.0), QPointF(111.0, 138.0))
    painter.drawLine(QPointF(637.0, 444.0), QPointF(732.0, 444.0))
    painter.drawLine(QPointF(659.0, 460.0), QPointF(732.0, 460.0))
    painter.drawLine(QPointF(644.0, 476.0), QPointF(732.0, 476.0))
    painter.drawEllipse(QPointF(110.0, 178.0), 25.0, 25.0)
    painter.drawEllipse(QPointF(110.0, 178.0), 15.0, 15.0)


def _draw_line_chart(
    painter: QPainter,
    origin: QPointF,
    title: str,
    points: Sequence[tuple[float, float]],
    *,
    width: float,
    footer_left: str,
    footer_right: str = "",
) -> None:
    x, y = origin.x(), origin.y()
    _text(
        painter,
        QRectF(x, y, width, 12.0),
        title,
        size=7,
        color=_color("#86afe0", 76),
        bold=True,
        spacing=1.0,
        family="Consolas",
    )
    top = y + 19.0
    painter.setPen(QPen(_color("#6895d1", 42), 0.6))
    for offset in (0.0, 18.0, 36.0):
        painter.drawLine(QPointF(x, top + offset), QPointF(x + width, top + offset))
    path = QPainterPath()
    for index, (px, py) in enumerate(points):
        point = QPointF(x + px, top + py)
        path.moveTo(point) if index == 0 else path.lineTo(point)
    painter.setPen(QPen(_color(_CYAN, 72), 1.2))
    painter.drawPath(path)
    _text(
        painter,
        QRectF(x, y + 58.0, width / 2.0, 10.0),
        footer_left,
        size=6,
        color=_color(_FAINT, 62),
        family="Consolas",
    )
    if footer_right:
        _text(
            painter,
            QRectF(x + width / 2.0, y + 58.0, width / 2.0, 10.0),
            footer_right,
            size=6,
            color=_color(_FAINT, 62),
            alignment=Qt.AlignmentFlag.AlignRight,
            family="Consolas",
        )


def _draw_load_testing_details(painter: QPainter) -> None:
    _draw_line_chart(
        painter,
        QPointF(556.0, 55.0),
        "LOAD / RESPONSE TIME",
        (
            (0.0, 30.0),
            (19.0, 27.0),
            (37.0, 29.0),
            (55.0, 18.0),
            (73.0, 22.0),
            (91.0, 9.0),
            (109.0, 14.0),
            (127.0, 4.0),
            (145.0, 12.0),
            (163.0, 1.0),
        ),
        width=163.0,
        footer_left="p95  284 ms",
        footer_right="42 rps",
    )

    x, y = 210.0, 72.0
    _text(
        painter,
        QRectF(x, y, 90.0, 12.0),
        "THROUGHPUT",
        size=7,
        color=_color("#7ea3d3", 66),
        bold=True,
        spacing=1.0,
        family="Consolas",
    )
    heights = (12.0, 20.0, 14.0, 30.0, 23.0, 34.0, 26.0)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_color("#3794dc", 55))
    for index, height in enumerate(heights):
        painter.drawRect(QRectF(x + index * 12.0, y + 50.0 - height, 7.0, height))
    painter.setPen(QPen(_color("#6491cc", 42), 0.6))
    painter.drawLine(QPointF(x, y + 50.0), QPointF(x + 86.0, y + 50.0))
    _text(
        painter,
        QRectF(x, y + 53.0, 100.0, 10.0),
        "1.8k req/min",
        size=6,
        color=_color(_FAINT, 56),
        family="Consolas",
    )

    x, y = 27.0, 421.0
    _text(
        painter,
        QRectF(x, y, 100.0, 12.0),
        "VIRTUAL USERS",
        size=7,
        color=_color("#86afe0", 68),
        bold=True,
        spacing=1.0,
        family="Consolas",
    )
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_color("#359bdc", 58))
    for bx, height in ((9.0, 16.0), (25.0, 23.0), (41.0, 32.0), (57.0, 42.0)):
        painter.drawRect(QRectF(x + bx, y + 64.0 - height, 8.0, height))
    path = QPainterPath(QPointF(x + 78.0, y + 49.0))
    path.cubicTo(
        QPointF(x + 98.0, y + 40.0),
        QPointF(x + 104.0, y + 54.0),
        QPointF(x + 121.0, y + 34.0),
    )
    path.cubicTo(
        QPointF(x + 137.0, y + 24.0),
        QPointF(x + 149.0, y + 32.0),
        QPointF(x + 168.0, y + 21.0),
    )
    painter.setPen(QPen(_color(_VIOLET, 58), 1.1))
    painter.drawPath(path)
    painter.setPen(QPen(_color("#6491cc", 42), 0.6))
    painter.drawLine(QPointF(x, y + 64.0), QPointF(x + 170.0, y + 64.0))
    _text(
        painter,
        QRectF(x, y + 67.0, 120.0, 10.0),
        "RAMP  10 -> 100",
        size=6,
        color=_color(_FAINT, 56),
        family="Consolas",
    )

    x, y = 603.0, 505.0
    _text(
        painter,
        QRectF(x, y, 115.0, 12.0),
        "DATA RUNNER",
        size=7,
        color=_color("#9d8bd6", 70),
        bold=True,
        spacing=1.0,
        family="Consolas",
    )
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_color("#322d50", 95))
    painter.drawRoundedRect(QRectF(x, y + 17.0, 119.0, 3.0), 1.5, 1.5)
    painter.setBrush(_color(_VIOLET, 80))
    painter.drawRoundedRect(QRectF(x, y + 17.0, 91.0, 3.0), 1.5, 1.5)
    _text(
        painter,
        QRectF(x, y + 23.0, 125.0, 10.0),
        "ROWS  076 / 100",
        size=6,
        color=_color("#9789c2", 66),
        family="Consolas",
    )
    _text(
        painter,
        QRectF(x, y + 35.0, 130.0, 10.0),
        "PASS  074   FAIL  002",
        size=6,
        color=_color("#9789c2", 66),
        family="Consolas",
    )


def _draw_data_paths(painter: QPainter) -> None:
    painter.setBrush(Qt.BrushStyle.NoBrush)
    request = QPainterPath(QPointF(-18.0, 352.0))
    request.cubicTo(
        QPointF(91.0, 352.0),
        QPointF(111.0, 214.0),
        QPointF(219.0, 214.0),
    )
    request.cubicTo(
        QPointF(313.0, 214.0),
        QPointF(312.0, 339.0),
        QPointF(380.0, 339.0),
    )
    painter.setPen(QPen(_color("#2f92ff", 168), 3.2))
    painter.drawPath(request)

    response = QPainterPath(QPointF(778.0, 250.0))
    response.cubicTo(
        QPointF(668.0, 250.0),
        QPointF(648.0, 397.0),
        QPointF(538.0, 397.0),
    )
    response.cubicTo(
        QPointF(451.0, 397.0),
        QPointF(451.0, 282.0),
        QPointF(380.0, 282.0),
    )
    painter.setPen(QPen(_color("#ad42ff", 166), 3.2))
    painter.drawPath(response)

    painter.setPen(Qt.PenStyle.NoPen)
    for x, y, color in (
        (86.0, 322.0, _CYAN),
        (160.0, 231.0, QColor("#36afff")),
        (675.0, 286.0, _VIOLET),
        (598.0, 374.0, QColor("#b449ff")),
    ):
        painter.setBrush(_color(color, 220))
        painter.drawEllipse(QPointF(x, y), 3.5, 3.5)

    _draw_chip(
        painter,
        QRectF(43.0, 303.0, 104.0, 31.0),
        "REQUEST",
        _CYAN,
        dot=True,
        opacity=150,
    )
    _draw_chip(
        painter,
        QRectF(610.0, 268.0, 107.0, 31.0),
        "RESPONSE",
        _VIOLET,
        dot=True,
        opacity=150,
    )
    _draw_chip(painter, QRectF(112.0, 366.0, 61.0, 22.0), "GET", _CYAN, opacity=55)
    _draw_chip(
        painter,
        QRectF(181.0, 278.0, 67.0, 22.0),
        "HEADERS",
        QColor("#2b88d8"),
        opacity=55,
    )
    _draw_chip(
        painter,
        QRectF(591.0, 346.0, 68.0, 22.0),
        "200 OK",
        _VIOLET,
        opacity=55,
    )
    _draw_chip(
        painter,
        QRectF(650.0, 537.0, 63.0, 22.0),
        "JSON",
        _VIOLET,
        opacity=42,
    )


def _draw_centre(painter: QPainter) -> None:
    center = QPointF(380.0, 252.0)
    _draw_soft_orb(painter, QPointF(380.0, 216.0), 94.0, QColor("#126cff"), 78)
    painter.setPen(QPen(_color("#102548", 220), 18.0))
    painter.setBrush(_color("#090f21", 242))
    painter.drawEllipse(center, 119.0, 119.0)

    ring = QLinearGradient(261.0, 150.0, 499.0, 354.0)
    ring.setColorAt(0.0, _CYAN)
    ring.setColorAt(0.5, _BLUE)
    ring.setColorAt(1.0, _VIOLET)
    painter.setPen(QPen(QBrush(ring), 1.5))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(center, 119.0, 119.0)
    painter.setPen(QPen(_color("#6292ff", 35), 1.0))
    painter.drawEllipse(center, 97.0, 97.0)
    painter.drawEllipse(center, 78.0, 78.0)

    icon = app_pixmap(108)
    painter.drawPixmap(326, 158, icon)
    painter.setPen(QPen(_color("#ffffff", 34), 1.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(328.0, 160.0, 104.0, 104.0), 29.0, 29.0)

    _text(
        painter,
        QRectF(270.0, 278.0, 220.0, 38.0),
        "Rest Tester",
        size=31,
        color=_TEXT,
        bold=True,
        alignment=Qt.AlignmentFlag.AlignCenter,
    )
    _text(
        painter,
        QRectF(280.0, 314.0, 200.0, 14.0),
        "API TESTING WORKSPACE",
        size=8,
        color=QColor("#7d9ecf"),
        bold=True,
        spacing=3.1,
        alignment=Qt.AlignmentFlag.AlignCenter,
    )


def _draw_workflow(painter: QPainter, progress: int, status: str) -> None:
    slogan = QLinearGradient(330.0, 0.0, 615.0, 0.0)
    slogan.setColorAt(0.0, _CYAN)
    slogan.setColorAt(0.48, _BLUE)
    slogan.setColorAt(1.0, _VIOLET)
    painter.setFont(_font(21, bold=True))
    painter.setPen(QPen(QBrush(slogan), 1.0))
    painter.drawText(
        QRectF(260.0, 389.0, 240.0, 31.0),
        Qt.AlignmentFlag.AlignCenter,
        "Send. Inspect. Assure.",
    )
    _text(
        painter,
        QRectF(230.0, 425.0, 300.0, 20.0),
        "Reliable API testing for every service you define.",
        size=12,
        color=_MUTED,
        alignment=Qt.AlignmentFlag.AlignCenter,
    )

    labels = (
        ("+", "BUILD", _CYAN),
        ("\u25f7", "MEASURE", QColor("#669fff")),
        ("\u2713", "VERIFY", _VIOLET),
    )
    for index, (mark, label, color) in enumerate(labels):
        x = 250.0 + index * 92.0
        rect = QRectF(x, 461.0, 76.0, 30.0)
        painter.setPen(QPen(_color(color, 145), 1.0))
        painter.setBrush(_color(color, 14))
        painter.drawRoundedRect(rect, 10.0, 10.0)
        _text(
            painter,
            QRectF(x + 10.0, 461.0, 18.0, 30.0),
            mark,
            size=16,
            color=_color(color, 215),
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        _text(
            painter,
            QRectF(x + 31.0, 461.0, 40.0, 30.0),
            label,
            size=8,
            color=_color(color, 185),
            bold=True,
        )

    progress_gradient = QLinearGradient(230.0, 0.0, 530.0, 0.0)
    progress_gradient.setColorAt(0.0, _CYAN)
    progress_gradient.setColorAt(0.55, _BLUE)
    progress_gradient.setColorAt(1.0, _VIOLET)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1d2946"))
    painter.drawRoundedRect(QRectF(230.0, 514.0, 300.0, 4.0), 2.0, 2.0)
    completed_width = 300.0 * max(0, min(progress, 100)) / 100.0
    if completed_width:
        painter.setBrush(QBrush(progress_gradient))
        painter.drawRoundedRect(
            QRectF(230.0, 514.0, completed_width, 4.0), 2.0, 2.0
        )
        painter.setBrush(_VIOLET)
        painter.drawEllipse(QPointF(230.0 + completed_width, 516.0), 3.0, 3.0)
    _text(
        painter,
        QRectF(230.0, 526.0, 300.0, 16.0),
        status.upper(),
        size=8,
        color=QColor("#6f8fbc"),
        spacing=1.5,
        alignment=Qt.AlignmentFlag.AlignCenter,
    )

    footer = QLinearGradient(52.0, 0.0, 708.0, 0.0)
    footer.setColorAt(0.0, _CYAN)
    footer.setColorAt(1.0, _VIOLET)
    painter.setPen(QPen(QBrush(footer), 1.0))
    painter.drawLine(QPointF(52.0, 557.0), QPointF(708.0, 557.0))
    _text(
        painter,
        QRectF(52.0, 563.0, 260.0, 22.0),
        "REQUEST  •  VERIFY  •  REPEAT",
        size=8,
        color=QColor("#5977a3"),
        bold=True,
        spacing=1.6,
    )
    _text(
        painter,
        QRectF(500.0, 563.0, 208.0, 22.0),
        "Ready when you are",
        size=9,
        color=QColor("#6985af"),
        alignment=Qt.AlignmentFlag.AlignRight,
    )


def splash_pixmap(
    progress: int = 0,
    status: str = "Starting Rest Tester",
) -> QPixmap:
    """Render the complete splash into a deterministic 760 x 600 pixmap."""
    pixmap = QPixmap(SPLASH_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    _draw_background(painter)
    _draw_ambient_api_details(painter)
    _draw_load_testing_details(painter)
    _draw_data_paths(painter)
    _draw_centre(painter)
    _draw_workflow(painter, progress, status)
    painter.end()
    return pixmap


class SplashScreen(QSplashScreen):
    """Frameless launch screen matching the product's API-testing workflow."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, splash_pixmap())
        self.setObjectName("startupSplash")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self._progress = 0
        self._status = "Starting Rest Tester"

    @property
    def progress(self) -> int:
        return self._progress

    @property
    def status(self) -> str:
        return self._status

    def set_progress(self, progress: int, status: str) -> None:
        """Update the splash from a completed application-startup milestone."""
        self._progress = max(0, min(int(progress), 100))
        self._status = status
        self.setPixmap(splash_pixmap(self._progress, self._status))
