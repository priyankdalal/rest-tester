"""Small reusable widgets shared by the tester and the catalog builder."""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QPointF,
    QSize,
    Qt,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QIcon, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
        if self._icon_name:
            self.icon_label.setPixmap(
                icon(self._icon_name, theme.TEXT_MUTED, 32).pixmap(32, 32)
            )


class TableEmptyState(EmptyStateWidget):
    """An empty state overlaid on an item view, shown whenever it has no rows.

    Attaching to the viewport rather than replacing the view in its layout
    keeps the header row visible, so the user can still see what the columns
    will be, and avoids restructuring every page that owns a table. Works for
    any ``QAbstractItemView`` - tables and trees alike - because it only needs
    the viewport and the model's top-level row count.
    """

    def __init__(self, table: QAbstractItemView) -> None:
        super().__init__(table.viewport())
        self._table = table
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

    def _sync(self) -> None:
        self.setGeometry(self._table.viewport().rect())
        self.setVisible(self._table.model().rowCount() == 0)
        self.raise_()


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
    """Toggle button with an optional compact summary anchored to the right."""

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
        self.set_summary("")

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
        if not self.summary_label.isVisible():
            return
        horizontal_margin = 12
        vertical_margin = 6
        hint = self.summary_label.sizeHint()
        width = min(
            hint.width(),
            max(0, self.width() // 3),
        )
        height = min(
            hint.height(),
            max(0, self.height() - (vertical_margin * 2)),
        )
        self.summary_label.setGeometry(
            max(0, self.width() - width - horizontal_margin),
            max(vertical_margin, (self.height() - height) // 2),
            width,
            height,
        )


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
        body_layout.addWidget(content)
        layout.addWidget(self.body)
        self.set_expanded(expanded)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def summary(self) -> str:
        return self.header.summary()

    def set_summary(self, summary: str | None) -> None:
        self.header.set_summary(summary)

    def refresh_theme(self) -> None:
        alpha = 42 if theme.ACTIVE_TOKENS["BACKGROUND"] == theme.LIGHT_TOKENS["BACKGROUND"] else 92
        self._shadow.setColor(QColor(4, 18, 38, alpha))
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
        self.updateGeometry()
        if changed:
            self.expandedChanged.emit(expanded)


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


def cell_button(text: str = "", tooltip: str = "") -> tuple[QWidget, QPushButton]:
    """A button sized to sit inside a table row, with its holder.

    A default ``QPushButton`` is 34px tall - exactly the row height of the
    builder tables - so dropping one straight into a cell fills it edge to edge
    and collides with the grid lines. The ``rowButton`` style is 22px, and the
    holder margins keep it clear of the row borders.

    Returns ``(holder, button)``: add the holder to the cell, connect the button.
    """
    button = QPushButton(text)
    button.setObjectName("rowButton")
    if tooltip:
        button.setToolTip(tooltip)
    holder = QWidget()
    layout = QHBoxLayout(holder)
    layout.setContentsMargins(4, 3, 4, 3)
    layout.setSpacing(0)
    layout.addWidget(button)
    return holder, button


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


def form_caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty("fieldCaption", True)
    return label
