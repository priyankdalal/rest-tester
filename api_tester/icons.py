"""Small consistent line icons drawn with Qt primitives."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)

#: Sizes baked into the application icon. Windows picks 16/32 for the title bar
#: and task bar, 256 for the large shell views, so all of them are drawn rather
#: than scaled from one bitmap.
APP_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

_ICON_TOP = "#3a97ff"
_ICON_BOTTOM = "#0058c7"


def app_pixmap(size: int = 256) -> QPixmap:
    """The product mark: a rounded blue tile with a request/response pair.

    Everything is expressed as a fraction of ``size`` so the same drawing reads
    correctly at 16 px and at 256 px.
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    def at(value: float) -> float:
        return value * size

    gradient = QLinearGradient(0.0, 0.0, 0.0, float(size))
    gradient.setColorAt(0.0, QColor(_ICON_TOP))
    gradient.setColorAt(1.0, QColor(_ICON_BOTTOM))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawRoundedRect(
        QRectF(at(0.04), at(0.04), at(0.92), at(0.92)), at(0.22), at(0.22)
    )

    stroke = QPen(QColor("#ffffff"), max(1.0, at(0.085)))
    stroke.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroke.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(stroke)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # The request leaves to the right, the response comes back to the left.
    painter.drawLine(QPointF(at(0.26), at(0.38)), QPointF(at(0.70), at(0.38)))
    painter.drawLine(QPointF(at(0.56), at(0.25)), QPointF(at(0.72), at(0.38)))
    painter.drawLine(QPointF(at(0.72), at(0.38)), QPointF(at(0.56), at(0.51)))

    painter.drawLine(QPointF(at(0.74), at(0.62)), QPointF(at(0.30), at(0.62)))
    painter.drawLine(QPointF(at(0.44), at(0.49)), QPointF(at(0.28), at(0.62)))
    painter.drawLine(QPointF(at(0.28), at(0.62)), QPointF(at(0.44), at(0.75)))
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    """The application icon, rendered at every size the shell asks for."""
    result = QIcon()
    for size in APP_ICON_SIZES:
        result.addPixmap(app_pixmap(size))
    return result


def icon(name: str, color: str = "#65758B", size: int = 18) -> QIcon:
    # Glyph paths use an 18x18 design grid. Draw them on a 20x20 logical
    # canvas with a one-pixel inset before scaling to the requested size.
    # Drawing directly into 15/16px pixmaps clips paths that reach x/y=18,
    # making toolbar and tab icons appear shifted toward the right/bottom.
    logical_size = 20
    pixmap = QPixmap(logical_size, logical_size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(1.0, 1.0)
    pen = QPen(QColor(color), 1.7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    if name == "search":
        painter.drawEllipse(QRectF(2.5, 2.5, 9.5, 9.5))
        painter.drawLine(QPointF(11, 11), QPointF(16, 16))
    elif name == "status-dot":
        painter.setBrush(QBrush(QColor(color)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(5, 5, 8, 8))
    elif name == "wrap":
        painter.drawLine(QPointF(2, 5), QPointF(12, 5))
        painter.drawArc(QRectF(9, 5, 7, 7), 90 * 16, -180 * 16)
        painter.drawLine(QPointF(12.5, 12), QPointF(5, 12))
        painter.drawLine(QPointF(5, 12), QPointF(8, 9))
        painter.drawLine(QPointF(5, 12), QPointF(8, 15))
    elif name == "api-explorer":
        painter.drawRoundedRect(QRectF(2, 3, 14, 12), 2, 2)
        painter.drawLine(QPointF(5, 7), QPointF(8, 9))
        painter.drawLine(QPointF(8, 9), QPointF(5, 11))
        painter.drawLine(QPointF(10, 11), QPointF(13, 11))
    elif name == "test-suites":
        painter.drawRoundedRect(QRectF(3, 3, 12, 13), 2, 2)
        painter.drawRoundedRect(QRectF(6, 1.5, 6, 3), 1, 1)
        painter.drawLine(QPointF(5.5, 8), QPointF(7, 9.5))
        painter.drawLine(QPointF(7, 9.5), QPointF(9.5, 6.5))
        painter.drawLine(QPointF(11, 8), QPointF(13, 8))
        painter.drawLine(QPointF(5.5, 13), QPointF(7, 14.5))
        painter.drawLine(QPointF(7, 14.5), QPointF(9.5, 11.5))
        painter.drawLine(QPointF(11, 13), QPointF(13, 13))
    elif name == "data-runner":
        painter.drawRoundedRect(QRectF(2, 3, 14, 12), 2, 2)
        painter.drawLine(QPointF(2, 7), QPointF(16, 7))
        painter.drawLine(QPointF(7, 7), QPointF(7, 15))
        painter.drawLine(QPointF(12, 7), QPointF(12, 15))
        painter.setBrush(QBrush(QColor(color)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            QPolygonF([QPointF(9, 9.5), QPointF(9, 12.5), QPointF(11.2, 11)])
        )
    elif name == "load-testing":
        painter.drawLine(QPointF(2.5, 2), QPointF(2.5, 16))
        painter.drawLine(QPointF(2.5, 16), QPointF(16, 16))
        painter.drawRect(QRectF(4.5, 12, 2.2, 4))
        painter.drawRect(QRectF(8, 8.5, 2.2, 7.5))
        painter.drawRect(QRectF(11.5, 4.5, 2.2, 11.5))
        painter.setBrush(QBrush(QColor(color)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPolygon(
            QPolygonF(
                [
                    QPointF(14.4, 2.2),
                    QPointF(12.4, 6.4),
                    QPointF(13.8, 6.4),
                    QPointF(12.6, 9.2),
                    QPointF(16, 5.0),
                    QPointF(14.2, 5.0),
                ]
            )
        )
    elif name == "bookmark":
        painter.drawRoundedRect(QRectF(4, 2, 10, 14), 1.5, 1.5)
        painter.drawLine(QPointF(4, 13), QPointF(9, 9.5))
        painter.drawLine(QPointF(9, 9.5), QPointF(14, 13))
    elif name == "folder":
        painter.drawRoundedRect(QRectF(2, 5, 14, 10.5), 2, 2)
        painter.drawLine(QPointF(2.5, 5), QPointF(2.5, 3))
        painter.drawLine(QPointF(2.5, 3), QPointF(7, 3))
        painter.drawLine(QPointF(7, 3), QPointF(9, 5))
    elif name == "globe":
        painter.drawEllipse(QRectF(2, 2, 14, 14))
        painter.drawEllipse(QRectF(5.5, 2, 7, 14))
        painter.drawLine(QPointF(2.5, 7), QPointF(15.5, 7))
        painter.drawLine(QPointF(2.5, 11), QPointF(15.5, 11))
    elif name == "send":
        painter.drawLine(QPointF(2, 3), QPointF(16, 9))
        painter.drawLine(QPointF(16, 9), QPointF(2, 15))
        painter.drawLine(QPointF(2, 15), QPointF(5, 9))
        painter.drawLine(QPointF(5, 9), QPointF(2, 3))
        painter.drawLine(QPointF(5, 9), QPointF(16, 9))
    elif name == "verify":
        painter.drawEllipse(QRectF(2, 2, 14, 14))
        painter.drawLine(QPointF(5, 9), QPointF(8, 12))
        painter.drawLine(QPointF(8, 12), QPointF(13.5, 6))
    elif name == "add-to-suite":
        painter.drawRoundedRect(QRectF(2, 3, 11, 13), 2, 2)
        painter.drawLine(QPointF(5, 7), QPointF(10, 7))
        painter.drawLine(QPointF(5, 10), QPointF(8, 10))
        painter.drawLine(QPointF(15, 8), QPointF(15, 14))
        painter.drawLine(QPointF(12, 11), QPointF(18, 11))
    elif name == "more":
        painter.setBrush(QBrush(QColor(color)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(2, 7, 3, 3))
        painter.drawEllipse(QRectF(7.5, 7, 3, 3))
        painter.drawEllipse(QRectF(13, 7, 3, 3))
    elif name == "settings":
        painter.drawEllipse(QRectF(6, 6, 6, 6))
        painter.drawEllipse(QRectF(2, 2, 14, 14))
        painter.drawLine(9, 0, 9, 3)
        painter.drawLine(9, 15, 9, 18)
        painter.drawLine(0, 9, 3, 9)
        painter.drawLine(15, 9, 18, 9)
    elif name == "copy":
        painter.drawRect(QRectF(5, 2, 10, 11))
        painter.drawRect(QRectF(2, 5, 10, 11))
    elif name == "fullscreen":
        painter.drawLine(QPointF(2, 7), QPointF(2, 2))
        painter.drawLine(QPointF(2, 2), QPointF(7, 2))
        painter.drawLine(QPointF(11, 2), QPointF(16, 2))
        painter.drawLine(QPointF(16, 2), QPointF(16, 7))
        painter.drawLine(QPointF(16, 11), QPointF(16, 16))
        painter.drawLine(QPointF(16, 16), QPointF(11, 16))
        painter.drawLine(QPointF(7, 16), QPointF(2, 16))
        painter.drawLine(QPointF(2, 16), QPointF(2, 11))
    elif name == "compare":
        painter.drawRoundedRect(QRectF(2, 3, 14, 12), 1.5, 1.5)
        painter.drawLine(QPointF(9, 3), QPointF(9, 15))
        painter.drawLine(QPointF(4.5, 7), QPointF(7, 7))
        painter.drawLine(QPointF(11, 11), QPointF(13.5, 11))
    elif name == "export":
        painter.drawLine(QPointF(9, 2), QPointF(9, 11))
        painter.drawLine(QPointF(5.5, 5.5), QPointF(9, 2))
        painter.drawLine(QPointF(12.5, 5.5), QPointF(9, 2))
        painter.drawLine(QPointF(3, 10), QPointF(3, 16))
        painter.drawLine(QPointF(3, 16), QPointF(15, 16))
        painter.drawLine(QPointF(15, 16), QPointF(15, 10))
    elif name == "save":
        painter.drawRect(QRectF(2, 2, 14, 14))
        painter.drawRect(QRectF(5, 2, 7, 5))
        painter.drawRect(QRectF(5, 10, 8, 6))
    elif name == "new":
        painter.drawRoundedRect(QRectF(3, 2, 12, 14), 1.5, 1.5)
        painter.drawLine(QPointF(6, 7), QPointF(12, 7))
        painter.drawLine(QPointF(9, 4), QPointF(9, 10))
        painter.drawLine(QPointF(6, 13), QPointF(12, 13))
    elif name == "add":
        painter.drawEllipse(QRectF(2, 2, 14, 14))
        painter.drawLine(QPointF(5, 9), QPointF(13, 9))
        painter.drawLine(QPointF(9, 5), QPointF(9, 13))
    elif name == "seed":
        painter.drawLine(QPointF(9, 1), QPointF(9, 5))
        painter.drawLine(QPointF(9, 13), QPointF(9, 17))
        painter.drawLine(QPointF(1, 9), QPointF(5, 9))
        painter.drawLine(QPointF(13, 9), QPointF(17, 9))
        painter.drawLine(QPointF(4, 4), QPointF(6.5, 6.5))
        painter.drawLine(QPointF(11.5, 11.5), QPointF(14, 14))
        painter.drawLine(QPointF(14, 4), QPointF(11.5, 6.5))
        painter.drawLine(QPointF(6.5, 11.5), QPointF(4, 14))
        painter.drawEllipse(QRectF(6.5, 6.5, 5, 5))
    elif name == "clear":
        eraser = QPolygonF(
            [
                QPointF(3, 11),
                QPointF(10.5, 3.5),
                QPointF(15, 8),
                QPointF(7.5, 15.5),
                QPointF(3, 15.5),
            ]
        )
        painter.drawPolygon(eraser)
        painter.drawLine(QPointF(7, 7), QPointF(11.5, 11.5))
        painter.drawLine(QPointF(7.5, 15.5), QPointF(16, 15.5))
    elif name == "beautify":
        painter.drawLine(QPointF(5, 3), QPointF(2, 3))
        painter.drawLine(QPointF(2, 3), QPointF(2, 7))
        painter.drawLine(QPointF(13, 3), QPointF(16, 3))
        painter.drawLine(QPointF(16, 3), QPointF(16, 7))
        painter.drawLine(QPointF(5, 15), QPointF(2, 15))
        painter.drawLine(QPointF(2, 15), QPointF(2, 11))
        painter.drawLine(QPointF(13, 15), QPointF(16, 15))
        painter.drawLine(QPointF(16, 15), QPointF(16, 11))
        painter.drawLine(QPointF(9, 6), QPointF(9, 12))
        painter.drawLine(QPointF(6, 9), QPointF(12, 9))
    elif name == "minify":
        painter.drawLine(QPointF(2, 2), QPointF(7, 7))
        painter.drawLine(QPointF(7, 3), QPointF(7, 7))
        painter.drawLine(QPointF(3, 7), QPointF(7, 7))
        painter.drawLine(QPointF(16, 2), QPointF(11, 7))
        painter.drawLine(QPointF(11, 3), QPointF(11, 7))
        painter.drawLine(QPointF(15, 7), QPointF(11, 7))
        painter.drawLine(QPointF(2, 16), QPointF(7, 11))
        painter.drawLine(QPointF(7, 15), QPointF(7, 11))
        painter.drawLine(QPointF(3, 11), QPointF(7, 11))
        painter.drawLine(QPointF(16, 16), QPointF(11, 11))
        painter.drawLine(QPointF(11, 15), QPointF(11, 11))
        painter.drawLine(QPointF(15, 11), QPointF(11, 11))
    elif name == "duplicate":
        painter.drawRoundedRect(QRectF(5, 2, 11, 11), 1.5, 1.5)
        painter.drawRoundedRect(QRectF(2, 5, 11, 11), 1.5, 1.5)
        painter.drawLine(QPointF(5, 10.5), QPointF(10, 10.5))
        painter.drawLine(QPointF(7.5, 8), QPointF(7.5, 13))
    elif name == "edit":
        painter.drawLine(QPointF(4, 14), QPointF(13.5, 4.5))
        painter.drawLine(QPointF(11.5, 2.5), QPointF(15.5, 6.5))
        painter.drawLine(QPointF(3, 15), QPointF(7, 14))
        painter.drawLine(QPointF(3, 15), QPointF(4, 11))
    elif name == "sign-in":
        painter.drawRoundedRect(QRectF(2, 3, 8, 12), 1.5, 1.5)
        painter.drawLine(QPointF(7, 9), QPointF(16, 9))
        painter.drawLine(QPointF(12, 5), QPointF(16, 9))
        painter.drawLine(QPointF(12, 13), QPointF(16, 9))
    elif name == "renew":
        painter.drawArc(QRectF(3, 3, 12, 12), 30 * 16, 285 * 16)
        painter.drawLine(QPointF(13, 3), QPointF(16, 3))
        painter.drawLine(QPointF(16, 3), QPointF(16, 6))
    elif name == "sign-out":
        painter.drawRoundedRect(QRectF(8, 3, 8, 12), 1.5, 1.5)
        painter.drawLine(QPointF(11, 9), QPointF(2, 9))
        painter.drawLine(QPointF(6, 5), QPointF(2, 9))
        painter.drawLine(QPointF(6, 13), QPointF(2, 9))
    elif name == "move-up":
        painter.drawLine(QPointF(9, 15), QPointF(9, 3))
        painter.drawLine(QPointF(4, 8), QPointF(9, 3))
        painter.drawLine(QPointF(14, 8), QPointF(9, 3))
    elif name == "move-down":
        painter.drawLine(QPointF(9, 3), QPointF(9, 15))
        painter.drawLine(QPointF(4, 10), QPointF(9, 15))
        painter.drawLine(QPointF(14, 10), QPointF(9, 15))
    elif name == "open":
        painter.drawLine(QPointF(2, 6), QPointF(5, 3))
        painter.drawLine(QPointF(5, 3), QPointF(9, 3))
        painter.drawLine(QPointF(9, 3), QPointF(11, 6))
        painter.drawRoundedRect(QRectF(2, 6, 14, 9), 1.5, 1.5)
        painter.drawLine(QPointF(5, 9), QPointF(13, 9))
    elif name == "save-as":
        painter.drawRect(QRectF(2, 2, 12, 12))
        painter.drawRect(QRectF(5, 2, 6, 4))
        painter.drawLine(QPointF(11, 15), QPointF(16, 10))
        painter.drawLine(QPointF(13.5, 12.5), QPointF(16, 15))
    elif name == "play":
        painter.drawPolygon(
            QPolygonF([QPointF(5, 3), QPointF(15, 9), QPointF(5, 15)])
        )
    elif name == "stop":
        painter.drawRoundedRect(QRectF(4, 4, 10, 10), 1.5, 1.5)
    elif name == "filter":
        painter.drawLine(QPointF(2, 3), QPointF(16, 3))
        painter.drawLine(QPointF(5, 8), QPointF(13, 8))
        painter.drawLine(QPointF(8, 13), QPointF(10, 13))
    elif name == "code":
        painter.drawLine(QPointF(6, 4), QPointF(2, 9))
        painter.drawLine(QPointF(2, 9), QPointF(6, 14))
        painter.drawLine(QPointF(12, 4), QPointF(16, 9))
        painter.drawLine(QPointF(16, 9), QPointF(12, 14))
        painter.drawLine(QPointF(10, 2), QPointF(8, 16))
    elif name == "eye":
        painter.drawEllipse(QRectF(2, 5, 14, 8))
        painter.drawEllipse(QRectF(7, 7, 4, 4))
    elif name == "star":
        points = [
            QPointF(9, 1), QPointF(11, 6), QPointF(16, 6),
            QPointF(12, 10), QPointF(14, 16), QPointF(9, 12),
            QPointF(4, 16), QPointF(6, 10), QPointF(2, 6), QPointF(7, 6),
        ]
        painter.drawPolygon(QPolygonF(points))
    elif name == "trash":
        painter.drawLine(QPointF(2.5, 4.5), QPointF(15.5, 4.5))
        painter.drawLine(QPointF(7, 4.5), QPointF(7, 2.5))
        painter.drawLine(QPointF(7, 2.5), QPointF(11, 2.5))
        painter.drawLine(QPointF(11, 2.5), QPointF(11, 4.5))
        painter.drawLine(QPointF(4, 4.5), QPointF(5, 15.5))
        painter.drawLine(QPointF(5, 15.5), QPointF(13, 15.5))
        painter.drawLine(QPointF(13, 15.5), QPointF(14, 4.5))
        painter.drawLine(QPointF(7.5, 7), QPointF(7.5, 13))
        painter.drawLine(QPointF(10.5, 7), QPointF(10.5, 13))
    elif name == "pause":
        painter.drawLine(QPointF(6, 3), QPointF(6, 15))
        painter.drawLine(QPointF(12, 3), QPointF(12, 15))
    elif name == "wand":
        painter.drawLine(QPointF(3, 15), QPointF(12, 6))
        painter.drawLine(QPointF(12, 6), QPointF(15, 3))
        painter.drawLine(QPointF(9, 2), QPointF(9, 4.5))
        painter.drawLine(QPointF(2, 9), QPointF(4.5, 9))
        painter.drawLine(QPointF(4, 4), QPointF(5.5, 5.5))
        painter.drawLine(QPointF(15.5, 8), QPointF(17, 9.5))
        painter.drawLine(QPointF(17, 9.5), QPointF(15.5, 11))
    elif name == "warning":
        painter.drawPolygon(
            QPolygonF([QPointF(9, 2), QPointF(16.5, 15.5), QPointF(1.5, 15.5)])
        )
        painter.drawLine(QPointF(9, 7), QPointF(9, 11.5))
        painter.drawPoint(QPointF(9, 13.5))
    elif name == "chart":
        painter.drawLine(QPointF(3, 16), QPointF(3, 3))
        painter.drawLine(QPointF(3, 16), QPointF(16, 16))
        painter.drawLine(QPointF(6, 13), QPointF(6, 8))
        painter.drawLine(QPointF(10, 13), QPointF(10, 5))
        painter.drawLine(QPointF(14, 13), QPointF(14, 10))
    elif name == "chevron-left":
        painter.drawLine(QPointF(11.5, 3.5), QPointF(6, 9))
        painter.drawLine(QPointF(6, 9), QPointF(11.5, 14.5))
    elif name == "chevron-right":
        painter.drawLine(QPointF(6.5, 3.5), QPointF(12, 9))
        painter.drawLine(QPointF(12, 9), QPointF(6.5, 14.5))
    else:
        painter.drawRoundedRect(QRectF(2, 2, 14, 14), 3, 3)
        painter.drawLine(5, 6, 13, 6)
        painter.drawLine(5, 9, 13, 9)
        painter.drawLine(5, 12, 10, 12)
    painter.end()
    scaled = pixmap.scaled(
        size,
        size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    return QIcon(scaled)


def solid_icon(name: str, color: str, size: int = 18) -> QIcon:
    """An icon that keeps ``color`` in every state, including disabled.

    Qt normally derives a greyed disabled pixmap, which is unreadable on a
    filled accent button whose label stays white while disabled.
    """
    source = icon(name, color, size)
    pixmap = source.pixmap(size, size)
    result = QIcon()
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Active,
                 QIcon.Mode.Selected, QIcon.Mode.Disabled):
        result.addPixmap(pixmap, mode)
    return result
