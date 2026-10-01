"""Small reusable widgets shared by the tester and the catalog builder."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math

from PyQt6 import sip

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QIntValidator,
    QPainter,
    QPainterPath,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QBoxLayout,
    QComboBox,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .icons import icon


_ICON_SIZE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
_TEXT_SIZE_ROLE = int(Qt.ItemDataRole.UserRole) + 2
_TEXT_COLOR_ROLE = int(Qt.ItemDataRole.UserRole) + 3
_TEXT_PADDING_ROLE = int(Qt.ItemDataRole.UserRole) + 4
_HIDE_TEXT_ROLE = int(Qt.ItemDataRole.UserRole) + 5
_HIDE_ICON_ROLE = int(Qt.ItemDataRole.UserRole) + 6

GLYPH_PROPERTY = "textGlyph"


def set_text_glyph(button: QAbstractButton, glyph: str, size: int = 16) -> None:
    """Tints ``glyph`` with the body colour and remembers it for re-tinting.

    :func:`icon` rasterises the colour once, so without the recorded property a
    palette switch leaves the button carrying the previous theme's glyph.
    """
    button.setProperty(GLYPH_PROPERTY, glyph)
    button.setIcon(icon(glyph, theme.TEXT, size))


def retint_text_glyphs(root: QWidget, size: int = 16) -> int:
    """Re-rasterises every glyph registered by :func:`set_text_glyph`."""
    repainted = 0
    for button in root.findChildren(QAbstractButton):
        glyph = button.property(GLYPH_PROPERTY)
        if glyph:
            button.setIcon(icon(glyph, theme.TEXT, size))
            repainted += 1
    return repainted



class IconTextItemDelegate(QStyledItemDelegate):
    """Paints list item icons and labels with per-item sizing and spacing."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt signature
        size = super().sizeHint(option, index)
        icon_size = index.data(_ICON_SIZE_ROLE)
        if icon_size is not None:
            size.setHeight(max(size.height(), int(icon_size) + 4))
        return size

    def paint(self, painter, option, index) -> None:
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        styled.text = ""
        styled.icon = QIcon()
        style = (
            styled.widget.style()
            if styled.widget is not None
            else QApplication.style()
        )
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem, styled, painter, styled.widget
        )

        rect = option.rect
        icon_size = index.data(_ICON_SIZE_ROLE)
        icon_size = int(icon_size) if icon_size is not None else 18
        text_padding = index.data(_TEXT_PADDING_ROLE)
        text_padding = int(text_padding) if text_padding is not None else 8
        item_icon = index.data(Qt.ItemDataRole.DecorationRole)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        hide_icon = bool(index.data(_HIDE_ICON_ROLE))
        hide_text = bool(index.data(_HIDE_TEXT_ROLE))
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        icon_x = rect.left() + 9
        if hide_text and not hide_icon:
            icon_x = rect.center().x() - icon_size // 2
        icon_y = rect.center().y() - icon_size // 2
        text_x = (
            icon_x
            if hide_icon
            else icon_x + icon_size + text_padding
        )
        text_rect = rect.adjusted(text_x - rect.left(), 0, -8, 0)

        painter.save()
        if not hide_icon and isinstance(item_icon, QIcon):
            painter.drawPixmap(
                icon_x,
                icon_y,
                item_icon.pixmap(icon_size, icon_size),
            )

        if not hide_text:
            font = QFont(option.font)
            text_size = index.data(_TEXT_SIZE_ROLE)
            if text_size is not None:
                font.setPointSizeF(float(text_size))
            painter.setFont(font)
            color = (
                QColor(theme.TEXT_INVERSE)
                if selected
                else QColor(index.data(_TEXT_COLOR_ROLE) or theme.NAV_TEXT)
            )
            painter.setPen(color)
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter
                | Qt.AlignmentFlag.AlignLeft
                | Qt.TextFlag.TextSingleLine,
                QFontMetrics(font).elidedText(
                    text, Qt.TextElideMode.ElideRight, text_rect.width()
                ),
            )
        painter.restore()


def make_icon_text_item(
    text: str,
    icon_name: str,
    *,
    icon_size: int = 18,
    text_size: float | None = None,
    icon_color: str = "#DCE9F8",
    text_color: str = theme.NAV_TEXT,
    text_padding: int = 8,
    hide_text: bool = False,
    hide_icon: bool = False,
) -> QListWidgetItem:
    """Creates a list item with independently configurable icon and label.

    ``text_padding`` is the gap, in pixels, between the icon and its text.
    ``hide_text`` and ``hide_icon`` independently hide either visual element.
    Existing standalone icons continue to use :func:`icons.icon` unchanged.
    """
    item = QListWidgetItem(icon(icon_name, icon_color, icon_size), text)
    item.setData(_ICON_SIZE_ROLE, icon_size)
    item.setData(_TEXT_SIZE_ROLE, text_size)
    item.setData(_TEXT_COLOR_ROLE, text_color)
    item.setData(_TEXT_PADDING_ROLE, text_padding)
    item.setData(_HIDE_TEXT_ROLE, hide_text)
    item.setData(_HIDE_ICON_ROLE, hide_icon)
    return item


def set_icon_text_items_collapsed(
    widget: QListWidget, collapsed: bool
) -> None:
    """Shows list items as icons only or as icon-and-text rows."""
    for row in range(widget.count()):
        item = widget.item(row)
        item.setData(_HIDE_TEXT_ROLE, collapsed)
    widget.viewport().update()


class EmptyStateWidget(QWidget):
    """A compact placeholder shown instead of an empty list.

    Exposes ``objectName``/``property`` hooks (``emptyState``, ``emptyStateIcon``,
    ``emptyStateTitle``, ``emptyStateGuidance``, ``emptyStateAction``) so the
    theme stylesheet can style every occurrence centrally.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        self._icon_name = ""
        self._palette_version = theme.PALETTE_VERSION
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(6)
        # Stretches rather than AlignCenter: a centred layout sizes every child
        # to its size hint, which makes the word-wrapped guidance break far
        # earlier than the available width.
        layout.addStretch()
        self.icon_label = QLabel()
        self.icon_label.setObjectName("emptyStateIcon")
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon_label)
        self.title_label = QLabel()
        self.title_label.setProperty("emptyStateTitle", True)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_label)
        self.guidance_label = QLabel()
        self.guidance_label.setProperty("emptyStateGuidance", True)
        self.guidance_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.guidance_label.setWordWrap(True)
        layout.addWidget(self.guidance_label)
        self.action_button = QPushButton()
        self.action_button.setProperty("emptyStateAction", True)
        self.action_button.setVisible(False)
        layout.addWidget(self.action_button, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()

    def set_content(
        self,
        *,
        icon_name: str,
        title: str,
        guidance: str,
        action_text: str | None = None,
        action_callback: Callable[[], None] | None = None,
    ) -> None:
        self._icon_name = icon_name
        self.refresh_theme()
        self.title_label.setText(title)
        self.guidance_label.setText(guidance)
        try:
            self.action_button.clicked.disconnect()
        except TypeError:
            pass
        if action_text and action_callback is not None:
            self.action_button.setText(action_text)
            self.action_button.clicked.connect(action_callback)
            self.action_button.setVisible(True)
        else:
            self.action_button.setVisible(False)

    def refresh_theme(self) -> None:
        """Repaints the glyph in the active palette's muted text colour."""
        self._palette_version = theme.PALETTE_VERSION
        if self._icon_name:
            self.icon_label.setPixmap(
                icon(self._icon_name, theme.TEXT_MUTED, 32).pixmap(32, 32)
            )

    def changeEvent(self, event) -> None:
        # Re-tints without the owning page needing a refresh_theme hook: a theme
        # switch restyles the whole app, which delivers StyleChange here.
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            if self._palette_version != theme.PALETTE_VERSION:
                self.refresh_theme()
        super().changeEvent(event)


class TableEmptyState(EmptyStateWidget):
    """An empty state overlaid on an item view, shown whenever it has no rows.

    While the view is empty its headers are hidden, so every empty table in
    the app reads as one centred placeholder card instead of a column strip
    above a blank area. The headers come back as soon as rows arrive. Works
    for any ``QAbstractItemView`` - tables and trees alike - because it only
    needs the viewport and the model's top-level row count.
    """

    def __init__(self, table: QAbstractItemView) -> None:
        super().__init__(table.viewport())
        self._table = table
        self._headers = [
            header
            for header in (
                getattr(table, "horizontalHeader", lambda: None)(),
                getattr(table, "verticalHeader", lambda: None)(),
                getattr(table, "header", lambda: None)(),
            )
            if header is not None
        ]
        # Only headers the owner already shows may be restored, so hiding the
        # placeholder never reveals a header a page deliberately turned off.
        # ``isHidden`` rather than ``isVisible``: the view is usually still
        # unshown while being built, so ``isVisible`` is False for every
        # header at this point and nothing would ever be restored.
        self._restorable = [header for header in self._headers if not header.isHidden()]
        # Styled flat: the view already draws the frame, and a rounded card
        # inside a square viewport leaves notched corners under the header.
        self.setProperty("tableOverlay", True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        table.viewport().installEventFilter(self)
        model = table.model()
        for signal in (
            model.rowsInserted,
            model.rowsRemoved,
            model.modelReset,
            model.layoutChanged,
        ):
            signal.connect(self._sync)
        self._sync()

    def eventFilter(self, source, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(self._table.viewport().rect())
        return super().eventFilter(source, event)

    def set_placeholder_visible(self, visible: bool) -> None:
        """Shows or hides the placeholder, hiding the headers along with it.

        For views whose emptiness the row count cannot express - a filtered
        tree still holds its rows - the owner drives visibility through here
        so the headers follow the same rule as a genuinely empty view.
        """
        for header in self._restorable:
            if not sip.isdeleted(header):
                header.setVisible(not visible)
        if visible:
            self.setGeometry(self._table.viewport().rect())
            self.raise_()
        self.setVisible(visible)

    def _sync(self) -> None:
        if sip.isdeleted(self) or sip.isdeleted(self._table):
            return
        self.setGeometry(self._table.viewport().rect())
        self.set_placeholder_visible(self._table.model().rowCount() == 0)


class OverlayEmptyState(EmptyStateWidget):
    """An empty state covering an arbitrary widget, toggled by the caller.

    ``TableEmptyState`` follows a model's row count; a pane has no such
    signal, so visibility here is driven explicitly via :meth:`set_active`.
    """

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self.setObjectName("emptyStateOverlay")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAutoFillBackground(True)
        host.installEventFilter(self)
        self.setVisible(False)

    def eventFilter(self, source, event) -> bool:
        if source is self._host and event.type() == QEvent.Type.Resize:
            self.setGeometry(self._host.rect())
        return super().eventFilter(source, event)

    def set_active(self, active: bool) -> None:
        """Covers the host when ``active``, otherwise reveals it again."""
        if active:
            self.setGeometry(self._host.rect())
            self.raise_()
        self.setVisible(active)


def attach_table_empty_state(
    table: QAbstractItemView,
    *,
    icon_name: str,
    title: str,
    guidance: str,
    action_text: str | None = None,
    action_callback: Callable[[], None] | None = None,
) -> TableEmptyState:
    """Gives ``table`` a themed placeholder for when it holds no rows."""
    overlay = TableEmptyState(table)
    overlay.set_content(
        icon_name=icon_name,
        title=title,
        guidance=guidance,
        action_text=action_text,
        action_callback=action_callback,
    )
    return overlay


class NumericTableItem(QTableWidgetItem):
    """Cell that displays formatted text but sorts on its numeric value.

    A plain ``QTableWidgetItem`` compares its display string, so a duration
    column sorts 100 before 25 before 9. Keeping the number in ``UserRole``
    lets the cell stay human-readable ("1.2 s", "84%") while still ordering
    correctly.
    """

    def __init__(self, value: float, text: str | None = None) -> None:
        super().__init__(str(value) if text is None else text)
        self.setData(Qt.ItemDataRole.UserRole, float(value))

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine = self.data(Qt.ItemDataRole.UserRole)
        theirs = other.data(Qt.ItemDataRole.UserRole)
        if mine is not None and theirs is not None:
            return float(mine) < float(theirs)
        return super().__lt__(other)


def enable_result_sorting(table: QTableWidget, *, default_column: int | None = None) -> None:
    """Lets the user sort a read-only result table by clicking its header."""
    table.setSortingEnabled(True)
    table.horizontalHeader().setSortIndicatorShown(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    if default_column is not None:
        table.sortItems(default_column, Qt.SortOrder.DescendingOrder)


class _AccordionHeader(QToolButton):
    """Toggle button with an optional compact summary anchored to the right.

    It can also carry an outcome: a coloured pill (shown only while the
    section is collapsed) and a short pulsing tint across the whole ribbon.
    """

    FLASH_DURATION_MS = 3000
    FLASH_PULSES = 3
    FLASH_PEAK_ALPHA = 0.30

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.summary_label = QLabel(self)
        self.summary_label.setObjectName("accordionSummary")
        self.summary_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.summary_label.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred
        )
        self.status_pill = QLabel(self)
        self.status_pill.setObjectName("accordionStatusPill")
        self.status_pill.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.status_pill.hide()
        self._status_color: str | None = None
        self._flash_color = QColor()
        self._flash_level = 0.0
        self._flash = QVariantAnimation(self)
        self._flash.setStartValue(0.0)
        self._flash.setEndValue(1.0)
        self._flash.setDuration(self.FLASH_DURATION_MS)
        self._flash.valueChanged.connect(self._on_flash_step)
        self._flash.finished.connect(self._on_flash_finished)
        self.set_summary("")

    # -- outcome pill and flash -------------------------------------------
    def set_status(self, text: str | None, color: str | None) -> None:
        """Sets (or clears, with ``None``) the collapsed-state outcome pill."""
        self._status_color = color if text else None
        self.status_pill.setText(text or "")
        self._style_status_pill()
        self._sync_status_visibility()

    def status_text(self) -> str:
        return self.status_pill.text() if self._status_color else ""

    def flash(self, color: str) -> None:
        self._flash.stop()
        self._flash_color = QColor(color)
        self._flash.start()

    def is_flashing(self) -> bool:
        return self._flash.state() == QAbstractAnimation.State.Running

    def stop_flash(self) -> None:
        self._flash.stop()
        self._on_flash_finished()

    def refresh_status_theme(self) -> None:
        self._style_status_pill()

    def _style_status_pill(self) -> None:
        if not self._status_color:
            self.status_pill.setStyleSheet("")
            return
        color = QColor(self._status_color)
        fill = QColor(color)
        fill.setAlphaF(0.16)
        border = QColor(color)
        border.setAlphaF(0.55)
        self.status_pill.setStyleSheet(
            "QLabel#accordionStatusPill {"
            f" background-color: {fill.name(QColor.NameFormat.HexArgb)};"
            f" color: {color.name()};"
            f" border: 1px solid {border.name(QColor.NameFormat.HexArgb)};"
            " border-radius: 9px; padding: 2px 9px;"
            " font-size: 8pt; font-weight: 700; }"
        )

    def _sync_status_visibility(self) -> None:
        self.status_pill.setVisible(bool(self._status_color) and not self.isChecked())
        self._position_summary()

    def nextCheckState(self) -> None:  # noqa: N802 - Qt signature
        super().nextCheckState()
        self._sync_status_visibility()

    def checkStateSet(self) -> None:  # noqa: N802 - Qt signature
        super().checkStateSet()
        self._sync_status_visibility()

    def _on_flash_step(self, value) -> None:
        # Three soft pulses that start and end at zero, so the ribbon
        # returns to its normal colour without a jump.
        progress = float(value)
        pulse = 0.5 - 0.5 * math.cos(2 * math.pi * self.FLASH_PULSES * progress)
        self._flash_level = pulse * self.FLASH_PEAK_ALPHA
        self.update()

    def _on_flash_finished(self) -> None:
        self._flash_level = 0.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().paintEvent(event)
        if self._flash_level <= 0 or not self._flash_color.isValid():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        radius = 7.0
        path = QPainterPath()
        if self.isChecked():
            # Mirrors the stylesheet: square bottom corners while expanded.
            path.moveTo(rect.left(), rect.bottom())
            path.lineTo(rect.left(), rect.top() + radius)
            path.quadTo(rect.left(), rect.top(), rect.left() + radius, rect.top())
            path.lineTo(rect.right() - radius, rect.top())
            path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
            path.lineTo(rect.right(), rect.bottom())
            path.closeSubpath()
        else:
            path.addRoundedRect(rect, radius, radius)
        fill = QColor(self._flash_color)
        fill.setAlphaF(self._flash_level)
        painter.fillPath(path, fill)
        edge = QColor(self._flash_color)
        edge.setAlphaF(min(1.0, self._flash_level * 3))
        painter.fillRect(QRectF(rect.left(), rect.top() + 4, 3, rect.height() - 8), edge)
        painter.end()

    def set_summary(self, summary: str | None) -> None:
        text = summary or ""
        self.summary_label.setText(text)
        self.summary_label.setVisible(bool(text))
        self.summary_label.setProperty("hasSummary", bool(text))
        self.setProperty("hasSummary", bool(text))
        self.style().unpolish(self)
        self.style().polish(self)
        self._position_summary()

    def summary(self) -> str:
        return self.summary_label.text()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().resizeEvent(event)
        self._position_summary()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().showEvent(event)
        self._position_summary()

    def _position_summary(self) -> None:
        horizontal_margin = 12
        vertical_margin = 6
        right = self.width() - horizontal_margin
        for label in (self.summary_label, self.status_pill):
            if label.isHidden():
                continue
            hint = label.sizeHint()
            width = min(
                hint.width(),
                max(0, self.width() // 3),
            )
            height = min(
                hint.height(),
                max(0, self.height() - (vertical_margin * 2)),
            )
            label.setGeometry(
                max(0, right - width),
                max(vertical_margin, (self.height() - height) // 2),
                width,
                height,
            )
            right -= width + 8


class AccordionSection(QFrame):
    """A compact collapsible card for vertically scrollable forms."""

    expandedChanged = pyqtSignal(bool)

    def __init__(
        self,
        title: str,
        content: QWidget,
        *,
        expanded: bool = False,
        summary: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("accordionSection")
        self._content = content
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(14)
        self._shadow.setOffset(QPointF(0, 4))
        self.setGraphicsEffect(self._shadow)
        self.refresh_theme()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = _AccordionHeader()
        self.header.setObjectName("accordionHeader")
        self.header.setText(title)
        self.header.set_summary(summary)
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self.header.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.header.toggled.connect(self.set_expanded)
        layout.addWidget(self.header)
        self.body = QWidget()
        self.body.setObjectName("accordionBody")
        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(12, 10, 12, 12)
        # A plain layout container inherits the global QWidget background and
        # would paint a grey block over the card. Only bare QWidgets are tagged:
        # real widgets (tables, editors) must keep their own background.
        if type(content) is QWidget:
            content.setProperty("transparentPane", True)
        body_layout.addWidget(content)
        layout.addWidget(self.body)
        self.set_expanded(expanded)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def summary(self) -> str:
        return self.header.summary()

    def set_summary(self, summary: str | None) -> None:
        self.header.set_summary(summary)

    def show_outcome(self, text: str, color: str, *, flash: bool = True) -> None:
        """Flags a finished action: pulses the ribbon and, while collapsed,
        keeps a coloured pill with ``text`` on its right edge."""
        self.header.set_status(text, color)
        if flash:
            self.header.flash(color)

    def clear_outcome(self) -> None:
        self.header.stop_flash()
        self.header.set_status(None, None)

    def outcome(self) -> str:
        return self.header.status_text()

    def refresh_theme(self) -> None:
        alpha = 42 if theme.ACTIVE_TOKENS["BACKGROUND"] == theme.LIGHT_TOKENS["BACKGROUND"] else 92
        self._shadow.setColor(QColor(4, 18, 38, alpha))
        if hasattr(self, "header"):
            self.header.refresh_status_theme()
        self.update()

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange and hasattr(self, "_shadow"):
            self.refresh_theme()

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        changed = self.is_expanded() != expanded
        self.header.blockSignals(True)
        self.header.setChecked(expanded)
        self.header.blockSignals(False)
        self.header.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.body.setVisible(expanded)
        self.setProperty("expanded", expanded)
        self.setProperty("collapsed", not expanded)
        self.header.setProperty("expanded", expanded)
        self.header.setProperty("collapsed", not expanded)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred if expanded else QSizePolicy.Policy.Fixed,
        )
        self.style().unpolish(self)
        self.style().polish(self)
        self.header.style().unpolish(self.header)
        self.header.style().polish(self.header)
        self.header._sync_status_visibility()
        self.updateGeometry()
        if changed:
            self.expandedChanged.emit(expanded)


def expected_status_combo() -> QComboBox:
    """Editable expected-status picker shared by the explorer and suite editor."""
    from .client import EXPECTED_STATUS_CHOICES

    combo = QComboBox()
    combo.setEditable(True)
    combo.addItems(EXPECTED_STATUS_CHOICES)
    # The theme's QComboBox padding (10px left, 36px right) and the 30px
    # drop-down, which sits inside that padding, both come off the edit
    # field. Qt's own size hint ignores that, so "200-299" used to scroll.
    widest = max(
        combo.fontMetrics().horizontalAdvance(choice)
        for choice in EXPECTED_STATUS_CHOICES
    )
    combo.setMinimumWidth(widest + 10 + 36 + 30 + 2 + 12)
    return combo


class EditorDialog(QDialog):
    """A modal host for an editor that is edited off to the side of a row.

    The editor widget is owned by the dialog for the dialog's whole life, so
    callers seed it before ``exec()`` and read it back only when the result is
    ``Accepted``. Cancel therefore needs no undo: the caller re-seeds from its
    own source of truth on the next open.
    """

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        editor: QWidget,
        accept_text: str,
        *,
        leading: QHBoxLayout | None = None,
        size: tuple[int, int] = (960, 520),
    ) -> None:
        super().__init__(parent)
        self.setObjectName("editorDialog")
        self.setModal(True)
        self.editor = editor
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        self.title_label = QLabel()
        self.title_label.setProperty("sectionTitle", True)
        layout.addWidget(self.title_label)
        layout.addWidget(editor, 1)
        footer = QHBoxLayout()
        footer.setSpacing(8)
        if leading is not None:
            footer.addLayout(leading)
        footer.addStretch()
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.reject)
        footer.addWidget(self.cancel_button)
        self.accept_button = QPushButton(accept_text)
        self.accept_button.setProperty("accent", True)
        self.accept_button.setDefault(True)
        self.accept_button.clicked.connect(self.accept)
        footer.addWidget(self.accept_button)
        layout.addLayout(footer)
        self.set_title(title)
        self.resize(*size)

    def set_title(self, title: str) -> None:
        self.setWindowTitle(title)
        self.title_label.setText(title)


class ResponsiveTwoColumn(QWidget):
    """Lays two panes side by side with an even split, stacking them vertically
    once the available width can no longer hold both.

    Unlike :class:`_ResponsiveSplitter` this is for peers of equal importance:
    both panes get stretch ``1`` so the split stays 50/50 at every width, and
    there is no draggable handle to knock it out of balance.

    ``min_pane_width`` is the narrowest a pane may become before stacking. When
    it is not supplied the threshold is derived from the panes' own
    ``minimumSizeHint`` so the breakpoint tracks the real content rather than a
    guessed pixel count.
    """

    def __init__(
        self,
        first: QWidget,
        second: QWidget,
        *,
        min_pane_width: int | None = None,
        spacing: int = 12,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("responsiveTwoColumn")
        # A bare QWidget would inherit the global window background and paint a
        # block over whatever surface it is placed on.
        self.setProperty("transparentPane", True)
        self.first = first
        self.second = second
        self._min_pane_width = min_pane_width
        self._applied: QBoxLayout.Direction | None = None
        self._layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(spacing)
        self._layout.addWidget(first, 1)
        self._layout.addWidget(second, 1)
        self._apply_direction()

    def threshold(self) -> int:
        """The width at or above which the panes sit side by side."""
        if self._min_pane_width is not None:
            return self._min_pane_width * 2 + self._layout.spacing()
        widest = max(
            self.first.minimumSizeHint().width(),
            self.second.minimumSizeHint().width(),
        )
        return widest * 2 + self._layout.spacing()

    def is_stacked(self) -> bool:
        return self._layout.direction() == QBoxLayout.Direction.TopToBottom

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override signature
        super().resizeEvent(event)
        self._apply_direction()

    def _apply_direction(self) -> None:
        wanted = (
            QBoxLayout.Direction.LeftToRight
            if self.width() >= self.threshold()
            else QBoxLayout.Direction.TopToBottom
        )
        if wanted == self._applied:
            return
        self._applied = wanted
        self._layout.setDirection(wanted)
        side_by_side = wanted == QBoxLayout.Direction.LeftToRight
        for pane in (self.first, self.second):
            # Side by side the horizontal policy is Ignored on purpose: a
            # QBoxLayout hands out surplus space in proportion to stretch only
            # *after* satisfying each pane's sizeHint, so two panes with equal
            # stretch but different hints end up different widths. Ignoring the
            # hint makes the equal stretch the only input, which is what keeps
            # the split at exactly 50/50. minimumSizeHint is still honoured.
            pane.setSizePolicy(
                QSizePolicy.Policy.Ignored if side_by_side else QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Preferred if side_by_side else QSizePolicy.Policy.Maximum,
            )


class AccordionScrollArea(QScrollArea):
    """A consistent scrollable stack of accordion sections."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        content_margins: tuple[int, int, int, int] = (12, 12, 12, 12),
    ) -> None:
        super().__init__(parent)
        self.setObjectName("accordionScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.content = QWidget()
        self.content.setObjectName("accordionContent")
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(*content_margins)
        self.content_layout.setSpacing(14)
        self.content_layout.addStretch()
        self.setWidget(self.content)

    def add_section(
        self,
        title: str,
        content: QWidget,
        *,
        expanded: bool = False,
        summary: str | None = None,
    ) -> AccordionSection:
        section = AccordionSection(
            title,
            content,
            expanded=expanded,
            summary=summary,
        )
        self.content_layout.insertWidget(self.content_layout.count() - 1, section)
        return section


def inset_shadow_detail_pane(
    content: QWidget,
    *,
    shell_name: str,
) -> tuple[QWidget, QWidget, QWidget]:
    """Wrap a detail pane with persistent inset shadows on its left and top edges."""

    shell = QWidget()
    shell.setObjectName(shell_name)
    layout = QGridLayout(shell)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    corner_shadow = QWidget()
    corner_shadow.setObjectName("cornerInsetShadow")
    corner_shadow.setFixedSize(12, 12)
    left_shadow = QWidget()
    left_shadow.setObjectName("leftInsetShadow")
    left_shadow.setFixedWidth(12)
    top_shadow = QWidget()
    top_shadow.setObjectName("topInsetShadow")
    top_shadow.setFixedHeight(12)
    for shadow in (corner_shadow, left_shadow, top_shadow):
        shadow.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    layout.addWidget(corner_shadow, 0, 0)
    layout.addWidget(top_shadow, 0, 1)
    layout.addWidget(left_shadow, 1, 0)
    layout.addWidget(content, 1, 1)
    return shell, left_shadow, top_shadow


def fit_table_rows(table: QTableWidget) -> bool:
    """Grows rows by however much their cell widgets are actually short.

    A fixed row height is guesswork: the shared ``::item`` padding shrinks the
    rectangle a cell widget receives, and the button inside it is sized by the
    font rather than by the ``min-height`` the stylesheet asks for. The
    padding is not a constant either -- it grows with the row -- so this
    measures the real deficit instead of modelling it, and reports whether
    anything changed so the caller can let it settle.

    ``visualRect`` is deliberately not used: it reports the padded row, not
    the smaller rectangle Qt hands the widget.
    """
    changed = False
    for row in range(table.rowCount()):
        deficit = 0
        for column in range(table.columnCount()):
            widget = table.cellWidget(row, column)
            if widget is None or widget.height() <= 0:
                continue
            widget.ensurePolished()
            hint = max(widget.sizeHint().height(), widget.minimumSizeHint().height())
            deficit = max(deficit, hint - widget.height())
        if deficit > 0:
            table.setRowHeight(row, table.rowHeight(row) + deficit)
            changed = True
    return changed


def settle_table_rows(table: QTableWidget, passes: int = 5) -> None:
    """Repeats :func:`fit_table_rows` until the rows stop changing.

    One pass is not enough: growing a row also grows the padding Qt takes out
    of it, so the widget ends up a little short again. Re-measuring after the
    next layout pass converges in two or three rounds; the bound just stops a
    pathological layout from looping forever.
    """
    if passes <= 0:
        return
    if fit_table_rows(table):
        QTimer.singleShot(0, lambda: settle_table_rows(table, passes - 1))


def fit_last_column(table: QTableWidget, minimum: int = 80) -> None:
    """Gives the last column whatever width is left in the viewport.

    ``QHeaderView``'s ``Stretch`` mode is not usable here: it sizes the section
    once against the width the header had during the first layout pass, and
    never recomputes it, so a table built inside a hidden ``QStackedWidget``
    page keeps a stale width and shows a horizontal scrollbar for the few
    pixels of difference. Measuring the viewport at the moment the table is
    actually on screen is deterministic and holds up when the window resizes.
    """
    last = table.columnCount() - 1
    if last < 0:
        return
    header = table.horizontalHeader()
    if header.sectionResizeMode(last) != QHeaderView.ResizeMode.Interactive:
        header.setSectionResizeMode(last, QHeaderView.ResizeMode.Interactive)
    used = sum(table.columnWidth(i) for i in range(last))
    table.setColumnWidth(last, max(minimum, table.viewport().width() - used))


class RowButton(QPushButton):
    """A table-cell button that elides its caption instead of demanding width.

    Captions here are generated ("Values, constraints...", "3 field(s)"), so a
    plain button reports whatever width its longest caption needs. A column set
    to ``Stretch`` will not shrink a section below that hint, which forces a
    horizontal scrollbar onto the table. Reporting an elided caption instead
    lets the column shrink, and the full text stays available as the tooltip.
    """

    _WIDTH_HINT = 76

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("rowButton")
        self._full_text = ""
        self._explicit_tooltip = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full_text = text
        self._apply_elided()

    def full_text(self) -> str:
        return self._full_text

    def set_hover_text(self, tooltip: str) -> None:
        """Sets a tooltip that survives re-eliding."""
        self._explicit_tooltip = tooltip
        self._apply_elided()

    def _apply_elided(self) -> None:
        available = self.width() - 20  # the rowButton style's horizontal padding
        metrics = QFontMetrics(self.font())
        if available > 0 and metrics.horizontalAdvance(self._full_text) > available:
            shown = metrics.elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, available
            )
        else:
            shown = self._full_text
        super().setText(shown)
        if self._explicit_tooltip and shown == self._full_text:
            self.setToolTip(self._explicit_tooltip)
        elif shown != self._full_text:
            self.setToolTip(
                f"{self._full_text}\n{self._explicit_tooltip}".strip()
                if self._explicit_tooltip
                else self._full_text
            )
        else:
            self.setToolTip("")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elided()

    def sizeHint(self) -> QSize:
        """Reports a modest width so a ``Stretch`` column can shrink.

        The height still comes from the real hint, because ``fit_table_rows``
        relies on it to grow the row enough that the button is not clipped.
        """
        hint = super().sizeHint()
        return QSize(min(hint.width(), self._WIDTH_HINT), hint.height())

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), self._WIDTH_HINT), hint.height())


def cell_button(text: str = "", tooltip: str = "") -> tuple[QWidget, RowButton]:
    """A button sized to sit inside a table row, with its holder.

    A default ``QPushButton`` is 34px tall - exactly the row height of the
    builder tables - so dropping one straight into a cell fills it edge to edge
    and collides with the grid lines. The ``rowButton`` style keeps it shorter,
    and the holder margins keep it clear of the row borders.

    The holder is marked ``transparentPane`` so it shows the row underneath:
    a bare ``QWidget`` otherwise picks up the global window background and
    paints a grey block across the cell.

    Returns ``(holder, button)``: add the holder to the cell, connect the button.
    """
    button = RowButton(text)
    if tooltip:
        button.set_hover_text(tooltip)
    holder = QWidget()
    holder.setProperty("transparentPane", True)
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(4, 3, 4, 3)
    layout.setSpacing(0)
    layout.addWidget(button)
    return holder, button


def center_in_cell(widget: QWidget) -> QWidget:
    """Wraps ``widget`` so it sits centred in a table cell.

    Checkboxes and other fixed-size controls carry no text, so left-aligning
    them leaves them floating away from the column they belong to. The holder
    is transparent for the same reason as :func:`cell_button`'s.
    """
    holder = QWidget()
    holder.setProperty("transparentPane", True)
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    layout.addStretch()
    layout.addWidget(widget)
    layout.addStretch()
    return holder


def button_in_cell(table: QTableWidget, row: int, column: int) -> QPushButton | None:
    """Returns the button inside a :func:`cell_button` holder, if there is one."""
    holder = table.cellWidget(row, column)
    if holder is None:
        return None
    if isinstance(holder, QPushButton):
        return holder
    return holder.findChild(QPushButton)


class ElidingLabel(QLabel):
    """A label that shrinks instead of widening its container.

    A plain ``QLabel`` reports the full width of its text as its *minimum*
    size, so a long caption in a shared layout forces the whole window wider.
    This one keeps a small minimum and elides what will not fit, holding the
    untruncated text in the tooltip.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._apply_elision()

    def full_text(self) -> str:
        return self._full_text

    def minimumSizeHint(self):  # noqa: N802 - Qt naming
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), 80))
        return hint

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._apply_elision()

    def _apply_elision(self) -> None:
        metrics = QFontMetrics(self.font())
        available = max(0, self.width())
        if available <= 0:
            super().setText(self._full_text)
            return
        super().setText(
            metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, available)
        )


class _PulseDot(QWidget):
    """A small status dot whose opacity can breathe in and out.

    The animation loops seamlessly because the key values start and end at the
    same opacity, so the dot never snaps back at the loop boundary.

    In ``halo`` mode the dot is wrapped in a soft ring of the same colour. The
    ring holds a steady alpha while the core breathes, which keeps the control
    legible at the 32px header size where a bare 8px dot is easy to miss.
    """

    _DIAMETER = 8
    _HALO_DIAMETER = 18
    _HALO_ALPHA = 0.22

    def __init__(self, parent: QWidget | None = None, *, halo: bool = False) -> None:
        super().__init__(parent)
        self._color = QColor(theme.PASS)
        self._opacity = 1.0
        self._halo = halo
        self._halo_visible = False
        size = self._HALO_DIAMETER if halo else self._DIAMETER
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(2200)
        self._animation.setLoopCount(-1)
        self._animation.setKeyValueAt(0.0, 1.0)
        self._animation.setKeyValueAt(0.5, 0.25)
        self._animation.setKeyValueAt(1.0, 1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._animation.valueChanged.connect(self._set_opacity)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def set_breathing(self, breathing: bool) -> None:
        # The ring is the resting state's cue, so it follows the animation:
        # present while live, absent once the dot is static.
        self._halo_visible = bool(breathing)
        if breathing:
            if self._animation.state() != QAbstractAnimation.State.Running:
                self._animation.start()
            return
        self._animation.stop()
        self._set_opacity(1.0)

    def _set_opacity(self, value) -> None:
        try:
            opacity = float(value)
        except (TypeError, ValueError):
            opacity = 1.0
        self._opacity = max(0.0, min(1.0, opacity))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        core = self.rect()
        if self._halo:
            if self._halo_visible:
                ring = QColor(self._color)
                ring.setAlphaF(self._HALO_ALPHA)
                painter.setBrush(ring)
                painter.drawEllipse(self.rect())
            inset = (self._HALO_DIAMETER - self._DIAMETER) // 2
            core = self.rect().adjusted(inset, inset, -inset, -inset)
        color = QColor(self._color)
        color.setAlphaF(self._opacity)
        painter.setBrush(color)
        painter.drawEllipse(core)
        painter.end()


class HeaderConnectionPill(QFrame):
    """A header capsule pairing a connection status dot with an edit button.

    The pill lives in the application header rather than the request workspace,
    so it carries no label: the state is read from the dot colour and the
    surrounding tint, and the full explanation is in the tooltip. The dot
    breathes and wears a soft halo only while connected, which distinguishes a
    live credential from a stale one without relying on the red/green hue pair
    alone.

    The edit control is the capsule's right half: square on the left where it
    meets the dot area, and rounded on the right to follow the capsule. It
    fills the border box exactly rather than overflowing it -- a child sized to
    the full capsule height would paint its hover state across the 1px border
    and square off the rounded edge.
    """

    edit_requested = pyqtSignal()

    _HEIGHT = 32
    _BORDER = 1
    _KEY_WIDTH = 36
    _KEY_HEIGHT = _HEIGHT - (_BORDER * 2)
    _KEY_RADIUS = _KEY_HEIGHT // 2

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("headerStatusPill")
        self.setProperty("state", "disconnected")
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self._HEIGHT)

        layout = QHBoxLayout(self)
        # Zero margins: the stylesheet border is excluded from the contents
        # rect, so the key lands flush against the inside of the capsule edge.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._dot_host = QWidget(self)
        self._dot_host.setObjectName("headerStatusDotHost")
        dot_layout = QHBoxLayout(self._dot_host)
        dot_layout.setContentsMargins(10, 0, 6, 0)
        dot_layout.setSpacing(0)
        self._dot = _PulseDot(self._dot_host, halo=True)
        dot_layout.addWidget(self._dot)
        layout.addWidget(self._dot_host)

        self.edit_button = QToolButton(self)
        self.edit_button.setObjectName("headerStatusEdit")
        self.edit_button.setIconSize(QSize(16, 16))
        self.edit_button.setFixedSize(self._KEY_WIDTH, self._KEY_HEIGHT)
        self.edit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_button.setToolTip(
            "Edit base URLs, headers, and authentication for the active environment"
        )
        self.edit_button.setAccessibleName("Edit environment")
        self.edit_button.clicked.connect(self.edit_requested.emit)
        # No drop shadow here. The key now sits flush against the capsule
        # border, so an outward shadow would blur straight past the rounded
        # edge. The raise is carried by the fill contrast and the left border.
        layout.addWidget(self.edit_button)

        self._connected = False
        self._status_text = "Disconnected"
        self._detail = ""
        self.refresh_theme()

    def is_connected(self) -> bool:
        return self._connected

    def status_text(self) -> str:
        """The state wording that the pill no longer renders as a label."""
        return self._status_text

    def set_state(self, connected: bool, text: str = "", tooltip: str = "") -> None:
        self._connected = bool(connected)
        self._status_text = text or ("Connected" if connected else "Disconnected")
        self._detail = tooltip
        self.setProperty("state", "connected" if connected else "disconnected")
        # Without a label the tooltip is the only place the wording survives,
        # so the status leads it and the detail explains it.
        summary = self._status_text
        self._dot_host.setToolTip(f"{summary}\n{tooltip}" if tooltip else summary)
        self._dot.set_breathing(self._connected)
        self.refresh_theme()

    def refresh_theme(self) -> None:
        key = "CONNECTED_DOT" if self._connected else "DISCONNECTED_DOT"
        fallback = theme.PASS if self._connected else theme.FAIL
        self._dot.set_color(theme.ACTIVE_TOKENS.get(key, fallback))
        # "sliders-v", not "settings": the cog is the nav rail's Settings icon,
        # and reusing it here would imply application preferences rather than
        # the active environment's URLs, headers and auth.
        self.edit_button.setIcon(icon("sliders-v", theme.TEXT_MUTED))
        # Every rule lives in the global sheet. Splitting the button's geometry
        # across a per-widget sheet and the global one let the native style win
        # the hover paint, which came back square-cornered.
        for widget in (self, self._dot_host, self.edit_button):
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class KeyValueTable(QWidget):
    """An editable two-column table of string pairs with add/delete rows."""

    changed = pyqtSignal()

    def __init__(
        self,
        key_label: str = "Name",
        value_label: str = "Value",
        key_placeholder: str = "",
        value_placeholder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.key_placeholder = key_placeholder
        self.value_placeholder = value_placeholder
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([key_label, value_label])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.AnyKeyPressed
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 200)
        self.table.itemChanged.connect(lambda _: self.changed.emit())
        self.empty_state = attach_table_empty_state(
            self.table,
            icon_name="fields",
            title=f"No {key_label.lower()}s yet",
            guidance=f"Use Add row to create the first {key_label.lower()}.",
        )
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.add_button = QPushButton("Add row")
        self.add_button.setIcon(icon("save", "#0878F9"))
        self.add_button.clicked.connect(lambda: self.add_row("", "", focus=True))
        row.addWidget(self.add_button)
        self.delete_button = QPushButton("Delete row")
        self.delete_button.setProperty("danger", True)
        self.delete_button.setIcon(icon("trash", "#E5484D"))
        self.delete_button.clicked.connect(self.delete_selected)
        row.addWidget(self.delete_button)
        row.addStretch()
        self.hint = QLabel()
        self.hint.setProperty("fieldCaption", True)
        row.addWidget(self.hint)
        layout.addLayout(row)

    def set_hint(self, text: str) -> None:
        self.hint.setText(text)

    def add_row(self, key: str = "", value: str = "", focus: bool = False) -> int:
        index = self.table.rowCount()
        self.table.insertRow(index)
        key_item = QTableWidgetItem(key)
        if not key:
            key_item.setToolTip(self.key_placeholder)
        value_item = QTableWidgetItem(value)
        if not value:
            value_item.setToolTip(self.value_placeholder)
        self.table.setItem(index, 0, key_item)
        self.table.setItem(index, 1, value_item)
        if focus:
            self.table.setCurrentCell(index, 0)
            self.table.editItem(key_item)
        self.changed.emit()
        return index

    def delete_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            self.table.removeRow(row)
        if rows:
            self.changed.emit()

    def set_pairs(self, pairs: dict[str, str]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for key, value in pairs.items():
            self.add_row(str(key), str(value))
        self.table.blockSignals(False)
        self.changed.emit()

    def pairs(self) -> dict[str, str]:
        """Returns the non-empty rows; the last written name wins."""
        result: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            key_item = self.table.item(row, 0)
            value_item = self.table.item(row, 1)
            key = key_item.text().strip() if key_item else ""
            if not key:
                continue
            result[key] = value_item.text().strip() if value_item else ""
        return result

    def keys(self) -> list[str]:
        return list(self.pairs())


class Pager(QWidget):
    """The app-wide pagination control: a split pill with a primary Next.

    Owns the page arithmetic as well as the chrome, so every pager in the app
    enforces the same rules — Prev disabled on the first page, Next disabled on
    the last, typed input clamped into range, and the whole strip hidden when
    the data fits on a single page. Callers supply a total and react to
    :attr:`page_changed`; they never compute page counts themselves.

    Styling lives in ``theme.py`` under the ``pager*`` object names, and the
    chevrons re-tint on a palette switch without the owning page needing a
    ``refresh_theme`` hook.
    """

    page_changed = pyqtSignal(int)

    #: Inner height of the pill. The theme stylesheet repeats this as the
    #: pager button min/max-height to override the global 22px button cap.
    _CONTROL_HEIGHT = 30

    #: Width of the chevron buttons, also repeated in the theme stylesheet.
    _BUTTON_WIDTH = 34

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        page_size: int = 100,
        hide_when_single_page: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("pager")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._page_size = max(1, page_size)
        self._hide_when_single_page = hide_when_single_page
        self._page = 0
        self._total_items = 0
        self._total_is_exact = True
        self._palette_version = theme.PALETTE_VERSION

        row = QHBoxLayout(self)
        # 1px inset so the children sit inside the pill's border instead of
        # painting over it, which would square off the rounded corners.
        row.setContentsMargins(1, 1, 1, 1)
        row.setSpacing(0)

        self.prev_button = QPushButton()
        self.prev_button.setObjectName("pagerPrev")
        self.prev_button.setToolTip("Previous page")
        self.prev_button.setAccessibleName("Previous page")
        self.prev_button.clicked.connect(lambda: self.set_page(self._page - 1))
        row.addWidget(self.prev_button)

        self.page_edit = QLineEdit("1")
        self.page_edit.setObjectName("pagerPage")
        self.page_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_edit.setValidator(QIntValidator(1, 1, self.page_edit))
        self.page_edit.editingFinished.connect(self._commit_typed_page)
        row.addWidget(self.page_edit)

        self.total_label = QLabel("of 1")
        self.total_label.setObjectName("pagerTotal")
        self.total_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.total_label)

        self.next_button = QPushButton()
        self.next_button.setObjectName("pagerNext")
        self.next_button.setToolTip("Next page")
        self.next_button.setAccessibleName("Next page")
        self.next_button.clicked.connect(lambda: self.set_page(self._page + 1))
        row.addWidget(self.next_button)

        for button in (self.prev_button, self.next_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIconSize(QSize(16, 16))
            button.setFixedSize(self._BUTTON_WIDTH, self._CONTROL_HEIGHT)
        self.page_edit.setFixedHeight(self._CONTROL_HEIGHT)
        self.total_label.setFixedHeight(self._CONTROL_HEIGHT)

        self.refresh_theme()
        self._sync()

    # ------------------------------------------------------------------ state

    @property
    def page(self) -> int:
        """The current page, zero-based."""
        return self._page

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def page_count(self) -> int:
        return max(1, -(-max(self._total_items, 1) // self._page_size))

    def offset(self) -> int:
        """The index of the first item on the current page."""
        return self._page * self._page_size

    def set_total(
        self, total_items: int, *, page_size: int | None = None, exact: bool = True
    ) -> None:
        """Re-points the pager at a new result set and returns to page 1.

        ``exact=False`` marks the total as a lower bound — the page count then
        renders with a trailing ``+`` — which is what a capped row scan needs.
        """
        if page_size is not None:
            self._page_size = max(1, page_size)
        self._total_items = max(0, total_items)
        self._total_is_exact = exact
        self._page = 0
        self._sync()

    def set_page(self, page: int, *, emit: bool = True) -> None:
        """Clamps ``page`` into range and emits :attr:`page_changed` if it moved."""
        target = max(0, min(int(page), self.page_count - 1))
        changed = target != self._page
        self._page = target
        self._sync()
        if changed and emit:
            self.page_changed.emit(target)

    def reset(self) -> None:
        self.set_total(0)

    # ------------------------------------------------------------------ internals

    def _commit_typed_page(self) -> None:
        text = self.page_edit.text().strip()
        if not text.isdigit():
            # Restores the displayed page rather than guessing at the intent.
            self._sync()
            return
        self.set_page(int(text) - 1)

    def _sync(self) -> None:
        count = self.page_count
        self.page_edit.setText(str(self._page + 1))
        self.total_label.setText(f"of {count}" + ("" if self._total_is_exact else "+"))
        self.prev_button.setEnabled(self._page > 0)
        self.next_button.setEnabled(self._page < count - 1)
        self._tint_chevrons()
        validator = self.page_edit.validator()
        if isinstance(validator, QIntValidator):
            validator.setTop(count)
        self.page_edit.setToolTip(f"Jump to a page (1-{count})")
        width = QFontMetrics(self.page_edit.font()).horizontalAdvance("0" * len(str(count)))
        self.page_edit.setFixedWidth(max(34, width + 20))
        if self._hide_when_single_page:
            self.setVisible(count > 1)

    def _tint_chevrons(self) -> None:
        # Both ends are primary-filled while usable, so their glyphs are
        # inverse; a disabled end falls back to the neutral pill colour, where
        # an inverse glyph would be invisible.
        for button, name in (
            (self.prev_button, "chevron-left"),
            (self.next_button, "chevron-right"),
        ):
            colour = theme.TEXT_INVERSE if button.isEnabled() else theme.TEXT_MUTED
            button.setIcon(icon(name, colour, 16))

    def refresh_theme(self) -> None:
        self._palette_version = theme.PALETTE_VERSION
        self._tint_chevrons()

    def changeEvent(self, event) -> None:
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            if self._palette_version != theme.PALETTE_VERSION:
                self.refresh_theme()
        super().changeEvent(event)


def form_caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("fieldCaption", True)
    return label


@dataclass(frozen=True)
class ToolbarAction:
    """One button in a page toolbar.

    ``icon_name`` is required rather than optional: every toolbar button in
    these pages carries a glyph, and making it mandatory stops a new action
    from silently shipping without one.
    """

    caption: str
    icon_name: str
    slot: Callable[[], None]
    accent: bool = False
    danger: bool = False
    tooltip: str = ""
    #: Buttons sharing a group number stay together; a divider is drawn
    #: wherever the group changes.
    group: int = 0


def build_page_toolbar(
    actions: tuple[ToolbarAction, ...],
) -> tuple[QHBoxLayout, dict[str, QPushButton]]:
    """Builds the top action row shared by the workspace pages.

    Matches the Environments page: a left-aligned row of buttons followed by a
    stretch, so the group stays packed against the left edge instead of
    spreading across the full width. Actions carrying different ``group``
    numbers are separated by a thin divider.
    """
    toolbar = QHBoxLayout()
    buttons: dict[str, QPushButton] = {}
    previous_group: int | None = None
    for action in actions:
        if previous_group is not None and action.group != previous_group:
            toolbar.addSpacing(4)
            divider = QFrame()
            divider.setObjectName("toolbarDivider")
            divider.setFixedWidth(1)
            toolbar.addWidget(divider)
            toolbar.addSpacing(4)
        previous_group = action.group
        button = QPushButton(action.caption)
        button.setToolTip(action.tooltip or action.caption)
        button.setAccessibleName(action.caption)
        if action.accent:
            button.setProperty("accent", True)
        if action.danger:
            button.setProperty("danger", True)
        # ``clicked`` carries a bool. Connecting a slot directly would feed
        # that bool into its first optional parameter -- which for actions
        # like ``new_catalog(confirm=True)`` silently turns the confirmation
        # off. The shim guarantees the slot is always called with no args.
        button.clicked.connect(lambda _checked=False, slot=action.slot: slot())
        toolbar.addWidget(button)
        buttons[action.caption] = button
    toolbar.addStretch()
    return toolbar, buttons


def tint_toolbar(
    actions: tuple[ToolbarAction, ...], buttons: dict[str, QPushButton]
) -> None:
    """Re-tints toolbar glyphs for the active palette.

    Icons are rasterised with a fixed colour, so they have to be rebuilt on
    every theme change or an accent button keeps a dark glyph on a blue fill.
    """
    for action in actions:
        button = buttons.get(action.caption)
        if button is None:
            continue
        if action.accent:
            colour = theme.TEXT_INVERSE
        elif action.danger:
            colour = theme.FAIL
        else:
            colour = theme.TEXT
        button.setIcon(icon(action.icon_name, colour, 16))


class Pane(QWidget):
    """A bordered column with a titled header and a swappable body.

    The border lives on this container rather than on the list inside it.
    That is the fix for panes visually dissolving when empty: the page used
    to hide the list and show a sibling empty state, which removed the only
    bordered widget and left the columns with no boundary at all.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("listPane")
        # A plain QWidget ignores stylesheet borders unless it is told to draw
        # a styled background, which is exactly what the pane boundary needs.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setObjectName("listPaneHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(8)
        self.title = ElidingLabel(title)
        self.title.setObjectName("listPaneTitle")
        header_layout.addWidget(self.title, 1)
        self.count = QLabel("")
        self.count.setObjectName("listPaneCount")
        header_layout.addWidget(self.count, 0)
        outer.addWidget(header)

        self.body = QVBoxLayout()
        self.body.setContentsMargins(8, 8, 8, 8)
        self.body.setSpacing(8)
        outer.addLayout(self.body, 1)

    def set_title(self, title: str) -> None:
        self.title.setText(title)

    def set_count(self, count: int | None) -> None:
        self.count.setText("" if count is None else str(count))
        self.count.setVisible(count is not None)

