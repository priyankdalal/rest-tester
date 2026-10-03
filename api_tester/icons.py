"""Small consistent line icons drawn with Qt primitives."""

from __future__ import annotations

import math
from typing import Literal

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
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

BulbColor = Literal["grey", "green", "red", "orange", "yellow", "blue"]


def light_bulb_icon(color: BulbColor = "yellow", size: int = 18) -> QIcon:
    """Reusable status bulb with named colours; use icon() for a custom colour."""
    from . import theme

    colors: dict[BulbColor, str] = {
        "grey": theme.SKIP,
        "green": theme.PASS,
        "red": theme.FAIL,
        "orange": theme.WARN,
        "yellow": "#eab308",
        "blue": theme.PRIMARY,
    }
    return icon("light-bulb", colors[color], size)


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


def _cog_polygon(
    teeth: int = 8, outer: float = 8.2, inner: float = 6.3
) -> QPolygonF:
    """A gear outline on the 18x18 grid, centred on (9, 9).

    Alternating outer/inner vertices are emitted for each tooth so a single
    stroked polygon reads as a cog without needing a filled path.
    """
    step = 2.0 * math.pi / teeth
    tooth = step * 0.26
    points: list[QPointF] = []
    for index in range(teeth):
        base = index * step - math.pi / 2.0
        for angle, radius in (
            (base - tooth, outer),
            (base + tooth, outer),
            (base + step / 2.0 - tooth, inner),
            (base + step / 2.0 + tooth, inner),
        ):
            points.append(
                QPointF(9.0 + math.cos(angle) * radius, 9.0 + math.sin(angle) * radius)
            )
    return QPolygonF(points)


def _bell_path() -> QPainterPath:
    """A notification bell: dome, flared rim and clapper."""
    path = QPainterPath()
    path.moveTo(2.9, 13.1)
    path.cubicTo(4.8, 11.6, 4.9, 9.9, 4.9, 7.9)
    path.cubicTo(4.9, 5.6, 6.7, 3.5, 9.0, 3.5)
    path.cubicTo(11.3, 3.5, 13.1, 5.6, 13.1, 7.9)
    path.cubicTo(13.1, 9.9, 13.2, 11.6, 15.1, 13.1)
    path.closeSubpath()
    path.moveTo(7.4, 14.8)
    path.cubicTo(7.8, 16.2, 10.2, 16.2, 10.6, 14.8)
    return path


def _crescent_path() -> QPainterPath:
    """A moon: a disc with an offset disc removed, stroked as one outline."""
    disc = QPainterPath()
    disc.addEllipse(QPointF(9.0, 9.2), 6.6, 6.6)
    bite = QPainterPath()
    bite.addEllipse(QPointF(12.9, 5.4), 6.4, 6.4)
    return disc.subtracted(bite)


def badged_icon(
    name: str, color: str, dot_color: str, size: int = 18
) -> QIcon:
    """``name`` with a small filled dot in the upper-right corner.

    The dot carries its own colour so an unread badge can use an accent while
    the glyph stays in the surrounding text colour. Compositing happens on the
    supersampled pixmap so the dot stays as crisp as the glyph.
    """
    supersample = max(1, int(math.ceil(_device_pixel_ratio())))
    pixmap = icon(name, color, size).pixmap(size * supersample, size * supersample)
    pixmap.setDevicePixelRatio(float(supersample))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(dot_color)))
    radius = max(1.8, size * 0.135)
    painter.drawEllipse(
        QPointF(size - radius - size * 0.03, radius + size * 0.03), radius, radius
    )
    painter.end()
    return QIcon(pixmap)


def _device_pixel_ratio() -> float:
    """The scale factor of the screen the UI is on, defaulting to 1.0.

    Icons are rasterised at this ratio so Qt never has to stretch them; on a
    125%/150% display an unscaled pixmap would otherwise be upsampled and look
    soft.
    """
    app = QGuiApplication.instance()
    if app is None:
        return 1.0
    screen = app.primaryScreen()
    if screen is None:
        return 1.0
    return max(1.0, float(screen.devicePixelRatio()))


def icon(name: str, color: str = "#65758B", size: int = 18) -> QIcon:
    # Glyph paths use an 18x18 design grid drawn inside a 20x20 logical box
    # with a one-pixel inset, so paths reaching x/y=18 are not clipped.
    #
    # The painter is scaled straight from that logical box to the target
    # device resolution, so the vector paths are rasterised once at their
    # final size. Drawing at a fixed 20px and then bitmap-scaling resampled
    # an already-antialiased image and produced visibly soft edges.
    logical_size = 20
    # Rasterise at an integer multiple of the requested size. An integer
    # factor keeps the icon's logical size exactly ``size`` (so layout never
    # drifts) while giving Qt a high-resolution source to sample from on
    # fractional-scale displays such as 125% or 150%.
    supersample = max(1, int(math.ceil(_device_pixel_ratio())))
    target = max(1, size * supersample)
    pixmap = QPixmap(target, target)
    pixmap.fill(Qt.GlobalColor.transparent)
    pixmap.setDevicePixelRatio(float(supersample))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    # The painter already works in device-independent units (the pixmap's
    # devicePixelRatio handles the supersampling), so map the 20-unit design
    # grid onto the icon's logical size rather than its device size.
    scale = size / logical_size
    painter.scale(scale, scale)
    painter.translate(1.0, 1.0)
    # The pen is expressed in logical grid units, so the stroke keeps the same
    # proportional weight at every size and device pixel ratio.
    pen = QPen(QColor(color), 1.7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)

    if name == "search":
        painter.drawEllipse(QRectF(2.5, 2.5, 9.5, 9.5))
        painter.drawLine(QPointF(11, 11), QPointF(16, 16))
    elif name == "info-circle":
        painter.drawEllipse(QRectF(2.5, 2.5, 13, 13))
        painter.drawPoint(QPointF(9, 6))
        painter.drawLine(QPointF(9, 9), QPointF(9, 13))
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
    elif name == "arrow-up":
        painter.drawLine(QPointF(9, 15), QPointF(9, 3))
        painter.drawLine(QPointF(9, 3), QPointF(4, 8))
        painter.drawLine(QPointF(9, 3), QPointF(14, 8))
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
    elif name == "more-vertical":
        painter.setBrush(QBrush(QColor(color)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QRectF(7, 2, 3, 3))
        painter.drawEllipse(QRectF(7, 7.5, 3, 3))
        painter.drawEllipse(QRectF(7, 13, 3, 3))
    elif name == "sliders-v":
        # Vertical stems deliberately: the horizontal slider variant shares a
        # stacked-horizontal-lines silhouette with "filter" at 16px.
        painter.drawLine(QPointF(6, 2.5), QPointF(6, 15.5))
        painter.drawLine(QPointF(12, 2.5), QPointF(12, 15.5))
        painter.setBrush(QBrush(QColor(color)))
        painter.drawEllipse(QPointF(6.0, 11.5), 2.3, 2.3)
        painter.drawEllipse(QPointF(12.0, 6.5), 2.3, 2.3)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif name == "sort":
        # Opposed arrows: distinct from the single-arrow move-up/move-down
        # glyphs and from "sliders-v", which is reserved for environment edit.
        painter.drawLine(QPointF(5.5, 15), QPointF(5.5, 3))
        painter.drawLine(QPointF(2.5, 6), QPointF(5.5, 3))
        painter.drawLine(QPointF(8.5, 6), QPointF(5.5, 3))
        painter.drawLine(QPointF(12.5, 3), QPointF(12.5, 15))
        painter.drawLine(QPointF(9.5, 12), QPointF(12.5, 15))
        painter.drawLine(QPointF(15.5, 12), QPointF(12.5, 15))
    elif name == "fields":
        # Two label + input rows: the classic form silhouette. Reads as "edit
        # as form fields", where the pencil glyph collapses into a diagonal
        # arrow at 16px.
        painter.drawLine(QPointF(2.5, 5.5), QPointF(4.5, 5.5))
        painter.drawRoundedRect(QRectF(7, 3, 9, 5), 1.4, 1.4)
        painter.drawLine(QPointF(2.5, 12.5), QPointF(4.5, 12.5))
        painter.drawRoundedRect(QRectF(7, 10, 9, 5), 1.4, 1.4)
    elif name == "sparkles":
        for cx, cy, radius in ((7.0, 10.0, 5.5), (14.0, 4.0, 2.5)):
            painter.drawPolygon(QPolygonF([
                QPointF(cx, cy - radius), QPointF(cx + radius * 0.28, cy - radius * 0.28),
                QPointF(cx + radius, cy), QPointF(cx + radius * 0.28, cy + radius * 0.28),
                QPointF(cx, cy + radius), QPointF(cx - radius * 0.28, cy + radius * 0.28),
                QPointF(cx - radius, cy), QPointF(cx - radius * 0.28, cy - radius * 0.28),
            ]))
    elif name == "settings":
        painter.drawPolygon(_cog_polygon())
        painter.drawEllipse(QPointF(9.0, 9.0), 2.5, 2.5)
    elif name == "bell":
        painter.drawPath(_bell_path())
    elif name == "moon":
        painter.drawPath(_crescent_path())
    elif name == "sun":
        painter.drawEllipse(QPointF(9.0, 9.0), 3.6, 3.6)
        for index in range(8):
            angle = math.radians(index * 45.0)
            dx, dy = math.cos(angle), math.sin(angle)
            painter.drawLine(
                QPointF(9.0 + dx * 5.9, 9.0 + dy * 5.9),
                QPointF(9.0 + dx * 7.8, 9.0 + dy * 7.8),
            )
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
    elif name == "remove":
        # Deliberately mirrors "add" minus the vertical stroke: these two are
        # always a pair, so they must read as opposites of the same action.
        painter.drawEllipse(QRectF(2, 2, 14, 14))
        painter.drawLine(QPointF(5, 9), QPointF(13, 9))
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
    elif name == "light-bulb":
        bulb = QPainterPath()
        bulb.moveTo(6, 12)
        bulb.lineTo(6, 10.5)
        bulb.cubicTo(2, 7.5, 3.5, 2, 9, 2)
        bulb.cubicTo(14.5, 2, 16, 7.5, 12, 10.5)
        bulb.lineTo(12, 12)
        bulb.closeSubpath()
        painter.drawPath(bulb)
        painter.drawLine(QPointF(6, 14), QPointF(12, 14))
        painter.drawLine(QPointF(7.5, 16), QPointF(10.5, 16))
        painter.drawLine(QPointF(9, 12), QPointF(9, 8))
        painter.drawLine(QPointF(7, 7), QPointF(9, 9))
        painter.drawLine(QPointF(9, 9), QPointF(11, 7))
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
    elif name == "close":
        painter.drawLine(QPointF(4.5, 4.5), QPointF(13.5, 13.5))
        painter.drawLine(QPointF(13.5, 4.5), QPointF(4.5, 13.5))
    else:
        painter.drawRoundedRect(QRectF(2, 2, 14, 14), 3, 3)
        painter.drawLine(5, 6, 13, 6)
        painter.drawLine(5, 9, 13, 9)
        painter.drawLine(5, 12, 10, 12)
    painter.end()
    return QIcon(pixmap)


def solid_icon(name: str, color: str, size: int = 18) -> QIcon:
    """An icon that keeps ``color`` in every state, including disabled.

    Qt normally derives a greyed disabled pixmap, which is unreadable on a
    filled accent button whose label stays white while disabled.
    """
    source = icon(name, color, size)
    supersample = max(1, int(math.ceil(_device_pixel_ratio())))
    pixmap = source.pixmap(size * supersample, size * supersample)
    pixmap.setDevicePixelRatio(float(supersample))
    result = QIcon()
    for mode in (QIcon.Mode.Normal, QIcon.Mode.Active,
                 QIcon.Mode.Selected, QIcon.Mode.Disabled):
        result.addPixmap(pixmap, mode)
    return result
