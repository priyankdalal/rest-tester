"""Saved requests and ordered request collections.

Saved requests never contain credentials. They use the active environment's
access token and API key only when loaded or run.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, Qt, pyqtProperty, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .icons import icon
# Re-exported: this widget used to live here, and pages still import it
# from this module.
from .widgets import ElidingLabel, EmptyStateWidget
from .workspace_store import WorkspaceStore, as_store


@dataclass(frozen=True)
class _ToolbarAction:
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


def _build_page_toolbar(
    actions: tuple[_ToolbarAction, ...],
) -> tuple[QHBoxLayout, dict[str, QPushButton]]:
    """Builds the top action row shared by Saved Requests and Collections.

    Matches the Environments page: a left-aligned row of buttons followed by a
    stretch, so the group stays packed against the left edge instead of
    spreading across the full width.
    """
    toolbar = QHBoxLayout()
    buttons: dict[str, QPushButton] = {}
    for action in actions:
        button = QPushButton(action.caption)
        button.setToolTip(action.tooltip or action.caption)
        button.setAccessibleName(action.caption)
        if action.accent:
            button.setProperty("accent", True)
        if action.danger:
            button.setProperty("danger", True)
        button.clicked.connect(action.slot)
        toolbar.addWidget(button)
        buttons[action.caption] = button
    toolbar.addStretch()
    return toolbar, buttons


def _tint_toolbar(actions: tuple[_ToolbarAction, ...], buttons: dict[str, QPushButton]) -> None:
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


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def relative_time(stamp: str, *, now: "datetime | None" = None) -> str:
    """Formats an ISO timestamp as a short "2 min ago" phrase.

    Returns ``""`` for anything that cannot be stated as a fact -- no value,
    an unparsable value, or a value far enough in the future that a clock or
    timezone is wrong. Callers hide the field entirely on ``""`` rather than
    printing "never", so the UI never claims to know a time it does not.
    """
    if not stamp:
        return ""
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return ""
    # Rows written before timestamps carried an offset are UTC by convention.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    seconds = (current - moment).total_seconds()
    # A small negative drift is normal between a write and a read, so treat
    # it as "just now"; a large one means the stamp is not trustworthy.
    if seconds < -60:
        return ""
    if seconds < 45:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        count = int(round(minutes))
        return f"{count} min ago" if count != 1 else "1 min ago"
    hours = minutes / 60
    if hours < 24:
        count = int(round(hours))
        return f"{count} hours ago" if count != 1 else "1 hour ago"
    days = hours / 24
    if days < 7:
        count = int(days)
        return f"{count} days ago" if count != 1 else "1 day ago"
    if days < 365:
        return moment.astimezone().strftime("%d %b")
    return moment.astimezone().strftime("%d %b %Y")


def _safe_values(values: dict[str, Any]) -> dict[str, str]:
    secret_headers = {
        "authorization", "x-api-key", "api-key", "proxy-authorization",
        "x-functions-key", "ocp-apim-subscription-key", "cookie", "set-cookie",
    }
    return {
        str(name): str(item)
        for name, item in values.items()
        if not (
            str(name).lower().startswith("header:")
            and str(name).split(":", 1)[1].lower() in secret_headers
        )
    }


@dataclass
class SavedRequest:
    endpoint_id: str
    name: str
    values: dict[str, str] = field(default_factory=dict)
    payload: Any = None
    expected_status: str = "200-299"
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    authentication: str = "inherit"
    #: When this request was last run, or "" if it never has been. Empty is
    #: meaningful: the UI shows nothing rather than inventing a time.
    last_used_at: str = ""
    #: "pass", "fail", or "" when never run or the outcome is unknown.
    last_status: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SavedRequest":
        return cls(
            id=str(value.get("id") or uuid4().hex[:12]),
            endpoint_id=str(value["endpoint_id"]),
            name=str(value.get("name") or value["endpoint_id"]),
            values=_safe_values(value.get("values", {})),
            payload=value.get("payload"),
            expected_status=str(value.get("expected_status", "200-299")),
            created_at=str(value.get("created_at") or _timestamp()),
            updated_at=str(value.get("updated_at") or _timestamp()),
            authentication=str(value.get("authentication", "inherit")),
            last_used_at=str(value.get("last_used_at") or ""),
            last_status=str(value.get("last_status") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["values"] = _safe_values(self.values)
        # Defense in depth if a future caller passes environment-shaped values.
        for key in ("access_token", "api_key", "authorization", "x-api-key"):
            document.pop(key, None)
        return document


@dataclass
class RequestCollection:
    name: str
    request_ids: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    created_at: str = field(default_factory=_timestamp)
    updated_at: str = field(default_factory=_timestamp)
    #: When this collection was last run, or "" if it never has been.
    last_used_at: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RequestCollection":
        return cls(
            id=str(value.get("id") or uuid4().hex[:12]),
            name=str(value.get("name") or "Collection"),
            request_ids=[str(item) for item in value.get("request_ids", [])],
            created_at=str(value.get("created_at") or _timestamp()),
            updated_at=str(value.get("updated_at") or _timestamp()),
            last_used_at=str(value.get("last_used_at") or ""),
        )


class SavedRequestStore:
    """Saved requests and collections, kept in the workspace database.

    The in-memory dictionaries stay the read model the UI binds to, but a
    mutation now writes only the row it touched instead of rewriting the
    whole document, so a crash mid-save can no longer lose the library.
    """

    def __init__(self, source: "WorkspaceStore | Path | str") -> None:
        self.store = as_store(source)
        self.path = self.store.path
        self.requests: dict[str, SavedRequest] = {}
        self.collections: dict[str, RequestCollection] = {}
        self.load()

    def load(self) -> None:
        self.requests = {
            item.id: item
            for item in (
                SavedRequest.from_dict(value)
                for value in self.store.load_saved_requests()
            )
        }
        self.collections = {
            item.id: item
            for item in (
                RequestCollection.from_dict(value)
                for value in self.store.load_collections()
            )
        }

    def save(self) -> None:
        """Writes every in-memory record back.

        Mutators persist themselves, so this is only needed after editing
        the dictionaries directly.
        """
        for request in self.requests.values():
            self.store.upsert_saved_request(request.to_dict())
        for collection in self.collections.values():
            self.store.upsert_collection(asdict(collection))

    def upsert_request(self, request: SavedRequest) -> None:
        request.updated_at = _timestamp()
        self.requests[request.id] = request
        self.store.upsert_saved_request(request.to_dict())

    def delete_request(self, request_id: str) -> None:
        self.requests.pop(request_id, None)
        for collection in self.collections.values():
            collection.request_ids = [
                item for item in collection.request_ids if item != request_id
            ]
        self.store.delete_saved_request(request_id)

    def upsert_collection(self, collection: RequestCollection) -> None:
        collection.updated_at = _timestamp()
        self.collections[collection.id] = collection
        self.store.upsert_collection(asdict(collection))

    def delete_collection(self, collection_id: str) -> None:
        self.collections.pop(collection_id, None)
        self.store.delete_collection(collection_id)

    def add_to_collection(self, collection_id: str, request_id: str) -> None:
        collection = self.collections[collection_id]
        if request_id not in self.requests:
            raise KeyError(f"Saved request does not exist: {request_id}")
        collection.request_ids.append(request_id)
        self.upsert_collection(collection)

    def remove_from_collection(self, collection_id: str, index: int) -> None:
        collection = self.collections[collection_id]
        if 0 <= index < len(collection.request_ids):
            collection.request_ids.pop(index)
            self.upsert_collection(collection)

    def move_in_collection(self, collection_id: str, index: int, offset: int) -> None:
        collection = self.collections[collection_id]
        target = index + offset
        if not 0 <= index < len(collection.request_ids) or not 0 <= target < len(
            collection.request_ids
        ):
            return
        collection.request_ids[index], collection.request_ids[target] = (
            collection.request_ids[target],
            collection.request_ids[index],
        )
        self.upsert_collection(collection)

    def collection_requests(self, collection_id: str) -> list[SavedRequest]:
        collection = self.collections[collection_id]
        return [
            self.requests[request_id]
            for request_id in collection.request_ids
            if request_id in self.requests
        ]

    def mark_request_used(self, request_id: str, *, passed: bool | None = None) -> None:
        """Records that a saved request was just run.

        Deliberately does not touch ``updated_at``: running a request is not
        editing it, and conflating the two would make every row claim it was
        edited whenever it was merely executed.
        """
        request = self.requests.get(request_id)
        if request is None:
            return
        request.last_used_at = _timestamp()
        if passed is not None:
            request.last_status = "pass" if passed else "fail"
        self.store.touch_saved_request(
            request_id, timestamp=request.last_used_at, passed=passed
        )

    def mark_collection_used(self, collection_id: str) -> None:
        collection = self.collections.get(collection_id)
        if collection is None:
            return
        collection.last_used_at = _timestamp()
        self.store.touch_collection(collection_id, timestamp=collection.last_used_at)


class SavedRequestRow(QWidget):
    """One saved request rendered as a method pill plus three text lines.

    The pill reuses ``endpointMethodPill`` so the colour coding matches the
    API Explorer exactly; restyling a method there restyles it here too.

    A saved request stores only ``endpoint_id``, so the method and URL have to
    be resolved against the catalog. That lookup can fail in three distinct
    ways, and they are different facts that must not read the same:

    * the endpoint was dropped or renumbered by a catalog rebuild,
    * no catalog has been loaded yet (the empty-catalog stand-in),
    * the saved request itself was deleted but a collection still lists it.

    Each degrades to a neutral ``N/A`` pill with wording specific to the case
    rather than rendering a blank or a misleading green ``GET``.
    """

    #: Shown when the method cannot be resolved. Styled neutral grey so it is
    #: never mistaken for a real method.
    UNKNOWN_METHOD = "N/A"

    def __init__(
        self,
        request: "SavedRequest | None",
        endpoint: Any | None,
        parent: QWidget | None = None,
        *,
        catalog_loaded: bool = True,
        request_id: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("savedRequestRow")

        method = (getattr(endpoint, "method", "") or self.UNKNOWN_METHOD).upper()
        if request is None:
            # A collection outlived the saved request it points at.
            name = "Missing saved request"
            url = "This saved request has been deleted"
            identifiers = f"Request {request_id}"
        else:
            name = _display_name(request.name, method)
            identifiers = f"Endpoint {request.endpoint_id}  \u00b7  Request {request.id}"
            if endpoint is not None:
                url = getattr(endpoint, "path", "") or ""
            elif catalog_loaded:
                url = "Endpoint is no longer in the catalog"
            else:
                url = "Endpoint details unavailable until a catalog is loaded"
        unresolved = endpoint is None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(10)

        self.method = QLabel(method)
        self.method.setObjectName("endpointMethodPill")
        self.method.setProperty("method", method)
        self.method.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.method.setFixedSize(62, 26)
        # Centred against the full three-line text block, so the pill reads as
        # a label for the whole row rather than for the first line only.
        layout.addWidget(self.method, 0, Qt.AlignmentFlag.AlignVCenter)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(1)

        self.name = ElidingLabel(name)
        self.name.setObjectName("savedRequestName")
        self.name.setProperty("missing", request is None)
        self.url = ElidingLabel(url)
        self.url.setObjectName("savedRequestUrl")
        self.url.setProperty("missing", unresolved)
        # Both IDs are shown because they answer different questions: which
        # endpoint this points at, and which saved row this is.
        self.identifiers = ElidingLabel(identifiers)
        self.identifiers.setObjectName("savedRequestIds")

        for label in (self.name, self.url, self.identifiers):
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text_layout.addWidget(label)
        layout.addLayout(text_layout, 1)

        # Last run, shown only when there is a real timestamp to show. A
        # request that has never run renders nothing here rather than a
        # placeholder, and never a status dot that could be read as a pass.
        used = relative_time(getattr(request, "last_used_at", "") if request else "")
        status = (getattr(request, "last_status", "") if request else "") or ""
        self.last_used = QLabel(used)
        self.last_used.setObjectName("savedRequestUsed")
        self.last_used.setVisible(bool(used))
        self.status_dot = QLabel()
        self.status_dot.setObjectName("savedRequestDot")
        self.status_dot.setFixedSize(10, 10)
        self.status_dot.setProperty("state", status or "none")
        # Only a recorded outcome earns a dot.
        self.status_dot.setVisible(bool(used and status))

        meta_layout = QHBoxLayout()
        meta_layout.setContentsMargins(0, 0, 0, 0)
        meta_layout.setSpacing(6)
        meta_layout.addWidget(self.last_used)
        meta_layout.addWidget(self.status_dot)
        self.meta = QWidget()
        # Bare QWidgets inherit the window background, which would paint a
        # grey block over the row.
        self.meta.setProperty("transparentPane", True)
        self.meta.setLayout(meta_layout)
        self.meta.setVisible(bool(used))
        layout.addWidget(self.meta, 0, Qt.AlignmentFlag.AlignVCenter)

        # Clicks have to reach the QListWidget or selecting a row would stop
        # working the moment it gained a widget.
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


def _display_name(name: str, method: str) -> str:
    """Drop a method prefix that the pill already shows.

    Saved requests default to a ``"GET GetAllBrands"`` style name, which reads
    as a stutter once the method sits in a coloured pill beside it. Only an
    exact leading match for *this* row's method is removed, so a deliberate
    name is never truncated.
    """
    prefix = f"{method} "
    if method and name.upper().startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix):]
    return name


class CollectionRow(QWidget):
    """One collection: folder glyph, name, request count, and two timestamps.

    The timestamps are omitted entirely when unknown rather than rendered as
    "never", so the row only ever states facts the store actually holds.
    """

    def __init__(
        self,
        collection: "RequestCollection",
        *,
        missing_count: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("collectionRow")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(9)

        self.folder = QLabel()
        self.folder.setObjectName("collectionFolderIcon")
        self.folder.setFixedSize(18, 18)
        layout.addWidget(self.folder, 0, Qt.AlignmentFlag.AlignTop)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(1)

        self.name = ElidingLabel(collection.name)
        self.name.setObjectName("collectionName")

        total = len(collection.request_ids)
        caption = f"{total} request" if total == 1 else f"{total} requests"
        if missing_count:
            # Surfaced here because a collection can outlive the requests it
            # points at, and a bare count would hide that.
            caption += f"  \u00b7  {missing_count} missing"
        self.count = QLabel(caption)
        self.count.setObjectName("collectionCount")
        self.count.setProperty("warn", bool(missing_count))

        used = relative_time(collection.last_used_at)
        self.last_used = QLabel(f"Used {used}" if used else "")
        self.last_used.setObjectName("collectionMeta")
        self.last_used.setVisible(bool(used))

        updated = relative_time(collection.updated_at)
        self.last_updated = QLabel(f"Updated {updated}" if updated else "")
        self.last_updated.setObjectName("collectionMeta")
        self.last_updated.setVisible(bool(updated))

        for label in (self.name, self.count, self.last_used, self.last_updated):
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text_layout.addWidget(label)
        layout.addLayout(text_layout, 1)

        self.refresh_theme()
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def refresh_theme(self) -> None:
        self.folder.setPixmap(icon("folder", theme.ACCENT_SOFT_TEXT, 18).pixmap(18, 18))


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


class MiniRequestHandler(QWidget):
    """A compact send-and-inspect panel for one saved request.

    Deliberately does not execute anything itself. The owning page hands it a
    runner, so the request still goes through the application's single
    execution path and keeps the active environment, authentication profile,
    timeout, and TLS settings. Duplicating that here would quietly diverge.
    """

    send_requested = pyqtSignal(str, object)
    close_requested = pyqtSignal()

    #: Methods that conventionally carry no request body.
    BODYLESS = {"GET", "HEAD", "DELETE", "OPTIONS"}

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("miniHandler")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._request_id = ""
        self._busy = False

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget()
        header.setObjectName("listPaneHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        header_layout.setSpacing(8)
        self.title = ElidingLabel("Request")
        self.title.setObjectName("listPaneTitle")
        header_layout.addWidget(self.title, 1)
        self.close_button = QPushButton()
        self.close_button.setObjectName("miniHandlerClose")
        self.close_button.setFixedSize(22, 22)
        self.close_button.setToolTip("Close this panel")
        self.close_button.setAccessibleName("Close request panel")
        self.close_button.clicked.connect(self.close_requested.emit)
        header_layout.addWidget(self.close_button, 0)
        outer.addWidget(header)

        body = QWidget()
        body.setProperty("transparentPane", True)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        address = QHBoxLayout()
        address.setSpacing(8)
        self.method = QLabel("")
        self.method.setObjectName("endpointMethodPill")
        self.method.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.method.setFixedSize(62, 26)
        address.addWidget(self.method, 0)
        self.url = ElidingLabel("")
        self.url.setObjectName("miniHandlerUrl")
        address.addWidget(self.url, 1)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("miniHandlerSend")
        self.send_button.setProperty("accent", True)
        self.send_button.setAccessibleName("Send request")
        self.send_button.clicked.connect(self._emit_send)
        address.addWidget(self.send_button, 0)
        layout.addLayout(address)

        self.payload_caption = QLabel("Payload")
        self.payload_caption.setObjectName("miniHandlerCaption")
        layout.addWidget(self.payload_caption)
        self.payload = QPlainTextEdit()
        self.payload.setObjectName("miniHandlerEditor")
        self.payload.setPlaceholderText("No payload")
        self.payload.setMinimumHeight(90)
        self.payload.setMaximumHeight(170)
        layout.addWidget(self.payload)

        self.payload_error = QLabel("")
        self.payload_error.setObjectName("miniHandlerError")
        self.payload_error.setWordWrap(True)
        self.payload_error.setVisible(False)
        layout.addWidget(self.payload_error)

        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self.status_chip = QLabel("")
        self.status_chip.setObjectName("miniHandlerStatus")
        self.elapsed_chip = QLabel("")
        self.elapsed_chip.setObjectName("miniHandlerChip")
        self.size_chip = QLabel("")
        self.size_chip.setObjectName("miniHandlerChip")
        for chip in (self.status_chip, self.elapsed_chip, self.size_chip):
            chip.setVisible(False)
            status_row.addWidget(chip)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        self.response_caption = QLabel("Response")
        self.response_caption.setObjectName("miniHandlerCaption")
        layout.addWidget(self.response_caption)
        self.response = QPlainTextEdit()
        self.response.setObjectName("miniHandlerEditor")
        self.response.setReadOnly(True)
        self.response.setPlaceholderText("Send the request to see the response.")
        layout.addWidget(self.response, 1)

        outer.addWidget(body, 1)
        self.refresh_theme()

    # ------------------------------------------------------------ content

    def load(self, request: SavedRequest, endpoint: Any | None) -> None:
        """Binds one saved request, clearing any response from the last one."""
        self._request_id = request.id
        method = (getattr(endpoint, "method", "") or SavedRequestRow.UNKNOWN_METHOD).upper()
        self.title.setText(_display_name(request.name, method))
        self.method.setText(method)
        self.method.setProperty("method", method)
        self.url.setText(getattr(endpoint, "path", "") or "Endpoint unavailable")

        if request.payload is None:
            text = ""
        else:
            try:
                text = json.dumps(request.payload, indent=2)
            except (TypeError, ValueError):
                # A payload that will not re-serialise is still shown, because
                # hiding it would make the editor silently lose user data.
                text = str(request.payload)
        self.payload.setPlainText(text)
        bodyless = method in self.BODYLESS
        self.payload.setPlaceholderText(
            f"{method} requests usually have no payload" if bodyless else "No payload"
        )
        self.payload_error.setVisible(False)
        self.clear_response()
        # An unresolved endpoint cannot be sent, so do not offer it.
        self.send_button.setEnabled(endpoint is not None)
        self.send_button.setToolTip(
            "Send this request" if endpoint is not None
            else "This request's endpoint is not in the catalog"
        )
        self._restyle_pill()

    def request_id(self) -> str:
        return self._request_id

    def payload_text(self) -> str:
        return self.payload.toPlainText()

    def clear_response(self) -> None:
        self.response.setPlainText("")
        for chip in (self.status_chip, self.elapsed_chip, self.size_chip):
            chip.setVisible(False)

    # ------------------------------------------------------------- sending

    def _emit_send(self) -> None:
        if self._busy or not self._request_id:
            return
        text = self.payload.toPlainText().strip()
        payload = None
        if text:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                # Reported inline rather than in a modal: the editor is right
                # there, and a dialog would hide the text being corrected.
                self.payload_error.setText(f"Payload is not valid JSON: {exc}")
                self.payload_error.setVisible(True)
                return
        self.payload_error.setVisible(False)
        self.send_requested.emit(self._request_id, payload)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.send_button.setEnabled(not busy and bool(self._request_id))
        self.send_button.setText("Sending..." if busy else "Send")
        if busy:
            self.clear_response()

    def show_result(self, result: Any) -> None:
        self.set_busy(False)
        code = int(getattr(result, "status_code", 0) or 0)
        reason = (getattr(result, "reason", "") or "").strip()
        self.status_chip.setText(f"{code} {reason}".strip() if code else "No response")
        self.status_chip.setProperty(
            "state", "pass" if getattr(result, "passed", False) else "fail"
        )
        self.status_chip.setVisible(True)
        elapsed = getattr(result, "elapsed_ms", 0) or 0
        self.elapsed_chip.setText(f"{int(elapsed)} ms")
        self.elapsed_chip.setVisible(True)
        self.size_chip.setText(_format_size(int(getattr(result, "size", 0) or 0)))
        self.size_chip.setVisible(True)
        body = getattr(result, "response_body", "") or ""
        error = (getattr(result, "error", "") or "").strip()
        self.response.setPlainText(body or error or "(empty response)")
        self._repolish(self.status_chip)

    def show_error(self, message: str) -> None:
        self.set_busy(False)
        self.status_chip.setText("Error")
        self.status_chip.setProperty("state", "fail")
        self.status_chip.setVisible(True)
        self.response.setPlainText(message)
        self._repolish(self.status_chip)

    # -------------------------------------------------------------- theme

    def refresh_theme(self) -> None:
        self.close_button.setIcon(icon("close", theme.TEXT_MUTED, 12))
        self.send_button.setIcon(icon("send", theme.TEXT_INVERSE, 14))
        self._restyle_pill()
        self._repolish(self.status_chip)

    def _restyle_pill(self) -> None:
        self._repolish(self.method)

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        """Forces a restyle after a dynamic property changes.

        Qt does not re-evaluate property selectors on its own, so without
        this a pill keeps the colour of the previous method.
        """
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def _format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _fit_rows(view: QListWidget) -> None:
    """Re-measures row widgets so none is squeezed below its own minimum.

    A row's size hint depends on the application stylesheet, which supplies
    the smaller fonts for the secondary lines. Lists built before that
    stylesheet is installed measure their rows too short and, because a
    size hint is only read once, stay too short for the life of the list.
    Re-fitting when the page is first shown corrects that, and is a no-op
    for lists that were already measured correctly.
    """
    for row in range(view.count()):
        item = view.item(row)
        widget = view.itemWidget(item)
        if widget is None:
            continue
        widget.ensurePolished()
        wanted = max(widget.sizeHint().height(), widget.minimumSizeHint().height())
        if wanted and item.sizeHint().height() != wanted:
            size = item.sizeHint()
            size.setHeight(wanted)
            item.setSizeHint(size)


class SavedRequestsPage(QWidget):
    request_selected = pyqtSignal(str)

    def __init__(self, store: SavedRequestStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        #: Endpoint metadata by id, supplied by the window once a catalog is
        #: loaded. Empty until then, which rows render as unresolved rather
        #: than treating as an error.
        self._endpoints: dict[str, Any] = {}
        layout = QVBoxLayout(self)
        heading = QLabel("Saved Requests")
        heading.setObjectName("savedRequestsPageTitle")
        heading.setProperty("pageTitle", True)
        heading.setProperty("workspaceTitle", True)
        layout.addWidget(heading)
        description = QLabel("Reuse and manage requests you have saved from the API Explorer.")
        description.setObjectName("savedRequestsPageDescription")
        description.setProperty("pageDescription", True)
        description.setWordWrap(True)
        layout.addWidget(description)

        self._actions = (
            _ToolbarAction("Open", "open", self._open, accent=True, tooltip="Open the selected request in the API Explorer"),
            _ToolbarAction("Rename", "edit", self.rename, tooltip="Rename the selected request"),
            _ToolbarAction("Delete", "trash", self.delete, danger=True, tooltip="Delete the selected request"),
        )
        toolbar, self._buttons = _build_page_toolbar(self._actions)
        layout.addLayout(toolbar)
        self.open_button = self._buttons["Open"]
        self.rename_button = self._buttons["Rename"]
        self.delete_button = self._buttons["Delete"]

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search saved requests...")
        self.search.textChanged.connect(self.refresh)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("savedRequestList")
        self.list.setProperty("rowWidgetList", True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.itemDoubleClicked.connect(self._open)
        layout.addWidget(self.list, 1)
        self.empty_state = EmptyStateWidget()
        layout.addWidget(self.empty_state, 1)
        self.refresh_theme()
        self.refresh()

    def set_endpoints(self, endpoints: dict[str, Any]) -> None:
        """Supply catalog endpoints so rows can show their method and URL."""
        self._endpoints = dict(endpoints)
        self.refresh()

    def refresh_theme(self) -> None:
        _tint_toolbar(self._actions, self._buttons)
        self.empty_state.refresh_theme()
        # Rows cache no colours themselves, but the pill and muted text are
        # repainted from the new palette when they are rebuilt.
        self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _fit_rows(self.list)

    def refresh(self) -> None:
        query = self.search.text().strip().lower()
        selected = self.current_id()
        self.list.clear()
        for request in sorted(self.store.requests.values(), key=lambda item: item.name.lower()):
            endpoint = self._endpoints.get(request.endpoint_id)
            method = (getattr(endpoint, "method", "") or "").lower()
            path = (getattr(endpoint, "path", "") or "").lower()
            # Search spans everything the row displays, so a user can find a
            # row by the URL or method they can actually see.
            haystack = (
                f"{request.name} {request.endpoint_id} {request.id} {method} {path}".lower()
            )
            if query and query not in haystack:
                continue
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, request.id)
            row = SavedRequestRow(request, endpoint, catalog_loaded=bool(self._endpoints))
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            # The size hint is read only after the row is parented into the
            # list, because that is when the application stylesheet applies
            # and the identifier line takes its smaller font.
            item.setSizeHint(row.sizeHint())
            if request.id == selected:
                self.list.setCurrentItem(item)
        self._refresh_empty_state(has_query=bool(query))

    def _refresh_empty_state(self, *, has_query: bool) -> None:
        if self.list.count():
            self.list.setVisible(True)
            self.empty_state.setVisible(False)
            return
        self.list.setVisible(False)
        self.empty_state.setVisible(True)
        if has_query:
            self.empty_state.set_content(
                icon_name="search",
                title="No matches",
                guidance="No saved requests match your search.",
                action_text="Clear search",
                action_callback=self._clear_search,
            )
        else:
            self.empty_state.set_content(
                icon_name="bookmark",
                title="No saved requests yet",
                guidance="Save a request from the API Explorer to reuse it here.",
            )

    def _clear_search(self) -> None:
        self.search.clear()

    def current_id(self) -> str:
        item = self.list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _open(self, *_args) -> None:
        request_id = self.current_id()
        if request_id:
            self.request_selected.emit(request_id)

    def rename(self) -> None:
        request_id = self.current_id()
        if not request_id:
            return
        request = self.store.requests[request_id]
        name, accepted = QInputDialog.getText(
            self, "Rename saved request", "Name", text=request.name
        )
        if accepted and name.strip():
            request.name = name.strip()
            self.store.upsert_request(request)
            self.refresh()

    def delete(self) -> None:
        request_id = self.current_id()
        if request_id:
            self.store.delete_request(request_id)
            self.refresh()


class _SlidePanel(QWidget):
    """A clipping container whose width animates without reflowing content.

    The child is held at its final width and simply revealed. Animating the
    child's own width instead would re-run its layout on every frame, which
    visibly truncates button labels and re-wraps the JSON mid-animation.
    """

    def __init__(self, content_width: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("transparentPane", True)
        self._content_width = content_width
        self._content: QWidget | None = None
        self.setFixedWidth(0)

    def set_content(self, widget: QWidget) -> None:
        self._content = widget
        widget.setParent(self)
        widget.setGeometry(0, 0, self._content_width, max(self.height(), 0))
        widget.show()

    def content_width(self) -> int:
        return self._content_width

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self._content is not None:
            self._content.setGeometry(0, 0, self._content_width, self.height())

    def _get_panel_width(self) -> int:
        return self.width()

    def _set_panel_width(self, value: int) -> None:
        self.setFixedWidth(max(0, int(value)))

    panelWidth = pyqtProperty(int, _get_panel_width, _set_panel_width)


class CollectionsPage(QWidget):
    run_collection_requested = pyqtSignal(list)
    #: Emitted when the mini handler asks to send: (request_id, payload).
    request_send_requested = pyqtSignal(str, object)

    HANDLER_WIDTH = 380

    def __init__(self, store: SavedRequestStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        #: Endpoint metadata by id, supplied by the window once a catalog is
        #: loaded, so member rows can show their method and URL.
        self._endpoints: dict[str, Any] = {}
        self._selected_request_id = ""
        self._rebuilding = False
        layout = QVBoxLayout(self)
        heading = QLabel("Collections")
        heading.setObjectName("collectionsPageTitle")
        heading.setProperty("pageTitle", True)
        heading.setProperty("workspaceTitle", True)
        layout.addWidget(heading)
        description = QLabel("Group saved requests into ordered collections and run them together.")
        description.setObjectName("collectionsPageDescription")
        description.setProperty("pageDescription", True)
        description.setWordWrap(True)
        layout.addWidget(description)

        self._actions = (
            _ToolbarAction("New Collection", "new", self.create, accent=True, tooltip="Create an empty collection"),
            _ToolbarAction("Rename", "edit", self.rename, tooltip="Rename the selected collection"),
            _ToolbarAction("Delete", "trash", self.delete, danger=True, tooltip="Delete the selected collection"),
            _ToolbarAction("Add Saved Request", "add", self.add_request, tooltip="Add a saved request to this collection"),
            _ToolbarAction("Remove", "remove", self.remove_request, danger=True, tooltip="Remove the selected request from this collection"),
            _ToolbarAction("Up", "move-up", lambda: self.move_request(-1), tooltip="Move the selected request earlier"),
            _ToolbarAction("Down", "move-down", lambda: self.move_request(1), tooltip="Move the selected request later"),
            _ToolbarAction("Run Collection", "play", self.run, tooltip="Run every request in this collection in order"),
        )
        toolbar, self._buttons = _build_page_toolbar(self._actions)
        layout.addLayout(toolbar)

        content = QHBoxLayout()
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(10)

        splitter = QSplitter()
        splitter.setObjectName("collectionsSplitter")
        splitter.setChildrenCollapsible(False)

        self.collections_pane = Pane("Collections")
        self.collection_search = self._make_search("Search collections")
        self.collection_search.textChanged.connect(self.refresh)
        self.collections_pane.body.addWidget(self.collection_search)
        self.collections = QListWidget()
        self.collections.setProperty("rowWidgetList", True)
        self.collections.currentItemChanged.connect(self._collection_changed)
        self.collections_pane.body.addWidget(self.collections, 1)
        self.collections_empty_state = EmptyStateWidget()
        self.collections_pane.body.addWidget(self.collections_empty_state, 1)
        splitter.addWidget(self.collections_pane)

        self.members_pane = Pane("Requests")
        self.member_search = self._make_search("Search requests in this collection")
        self.member_search.textChanged.connect(self.refresh_members)
        self.members_pane.body.addWidget(self.member_search)
        self.members = QListWidget()
        self.members.setObjectName("collectionMembersList")
        self.members.setProperty("rowWidgetList", True)
        self.members.currentItemChanged.connect(self._member_changed)
        self.members_pane.body.addWidget(self.members, 1)
        self.members_empty_state = EmptyStateWidget()
        self.members_pane.body.addWidget(self.members_empty_state, 1)
        splitter.addWidget(self.members_pane)
        splitter.setSizes([320, 640])
        content.addWidget(splitter, 1)

        self.handler_panel = _SlidePanel(self.HANDLER_WIDTH)
        self.handler = MiniRequestHandler()
        self.handler.close_requested.connect(self.close_handler)
        self.handler.send_requested.connect(self.request_send_requested.emit)
        self.handler_panel.set_content(self.handler)
        content.addWidget(self.handler_panel, 0)

        self._animation = QPropertyAnimation(self.handler_panel, b"panelWidth", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        layout.addLayout(content, 1)
        self.refresh_theme()
        self.refresh()

    # --------------------------------------------------------------- setup

    def _make_search(self, placeholder: str) -> QLineEdit:
        field = QLineEdit()
        field.setPlaceholderText(placeholder)
        field.setClearButtonEnabled(True)
        field.setAccessibleName(placeholder)
        return field

    def set_endpoints(self, endpoints: dict[str, Any]) -> None:
        """Supply catalog endpoints so member rows can show method and URL."""
        self._endpoints = dict(endpoints)
        self.refresh_members()

    def refresh_theme(self) -> None:
        _tint_toolbar(self._actions, self._buttons)
        self.collections_empty_state.refresh_theme()
        self.members_empty_state.refresh_theme()
        for field in (self.collection_search, self.member_search):
            field.addAction(
                icon("search", theme.TEXT_MUTED, 14),
                QLineEdit.ActionPosition.LeadingPosition,
            )
            # Re-adding would stack duplicate glyphs, so drop any earlier one.
            actions = field.actions()
            for stale in actions[:-1]:
                field.removeAction(stale)
        self.handler.refresh_theme()
        # Row widgets have to be rebuilt to pick up the new palette for the
        # pill, the folder glyph, and the muted text lines.
        self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _fit_rows(self.collections)
        _fit_rows(self.members)

    def current_collection_id(self) -> str:
        item = self.collections.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    # ------------------------------------------------------------ listings

    def refresh(self, *_args) -> None:
        selected = self.current_collection_id()
        query = self.collection_search.text().strip().lower()
        was_rebuilding = self._rebuilding
        self._rebuilding = True
        self.collections.clear()
        collections = sorted(
            self.store.collections.values(), key=lambda item: item.name.lower()
        )
        matched = [item for item in collections if query in item.name.lower()]
        for collection in matched:
            missing = sum(
                1 for rid in collection.request_ids if rid not in self.store.requests
            )
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, collection.id)
            row = CollectionRow(collection, missing_count=missing)
            self.collections.addItem(item)
            self.collections.setItemWidget(item, row)
            item.setSizeHint(row.sizeHint())
            if collection.id == selected:
                self.collections.setCurrentItem(item)
        if self.collections.currentItem() is None and self.collections.count():
            self.collections.setCurrentRow(0)
        self.collections_pane.set_count(len(matched) if collections else None)

        has_rows = bool(self.collections.count())
        self.collections.setVisible(has_rows)
        self.collections_empty_state.setVisible(not has_rows)
        if not has_rows:
            if collections:
                # Filtered to nothing: offer a way back, not a create action.
                self.collections_empty_state.set_content(
                    icon_name="search",
                    title="No matching collections",
                    guidance="No collection name matches this search.",
                    action_text="Clear Search",
                    action_callback=self.collection_search.clear,
                )
            else:
                self.collections_empty_state.set_content(
                    icon_name="folder",
                    title="No collections yet",
                    guidance="Group saved requests into a collection to run them together.",
                    action_text="New Collection",
                    action_callback=self.create,
                )
        self._rebuilding = was_rebuilding
        self.refresh_members()

    def refresh_members(self, *_args) -> None:
        # Selection is tracked by request id rather than row, because the
        # member search can hide rows and shift every index.
        previous = self._current_member_id() or self._selected_request_id
        was_rebuilding = self._rebuilding
        self._rebuilding = True
        try:
            self._rebuild_members(previous)
        finally:
            self._rebuilding = was_rebuilding
        # Reconciled after the rebuild rather than from the selection signal,
        # so a refresh never collapses a panel that should stay open.
        if self._current_member_id() != self._selected_request_id:
            self.close_handler()
        self._update_actions()

    def _rebuild_members(self, previous: str) -> None:
        self.members.clear()
        collection_id = self.current_collection_id()
        if not collection_id or collection_id not in self.store.collections:
            self.members_pane.set_title("Requests")
            self.members_pane.set_count(None)
            self.member_search.setVisible(False)
            self.members.setVisible(False)
            self.members_empty_state.setVisible(True)
            self.members_empty_state.set_content(
                icon_name="bookmark",
                title="No collection selected",
                guidance="Select or create a collection to see its saved requests.",
            )
            return
        collection = self.store.collections[collection_id]
        self.members_pane.set_title(collection.name)
        self.member_search.setVisible(True)
        query = self.member_search.text().strip().lower()
        shown = 0
        for index, request_id in enumerate(collection.request_ids):
            request = self.store.requests.get(request_id)
            endpoint = (
                self._endpoints.get(request.endpoint_id) if request is not None else None
            )
            if query and not self._matches(request, endpoint, request_id, query):
                continue
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, request_id)
            # The position within the collection is carried explicitly so that
            # reorder and remove stay correct while the list is filtered.
            item.setData(Qt.ItemDataRole.UserRole + 1, index)
            row = SavedRequestRow(
                request,
                endpoint,
                catalog_loaded=bool(self._endpoints),
                request_id=request_id,
            )
            self.members.addItem(item)
            self.members.setItemWidget(item, row)
            # Read after parenting, so the stylesheet has applied and the
            # identifier line reports its smaller font.
            item.setSizeHint(row.sizeHint())
            if request_id == previous:
                self.members.setCurrentItem(item)
            shown += 1
        self.members_pane.set_count(shown)
        has_rows = bool(shown)
        self.members.setVisible(has_rows)
        self.members_empty_state.setVisible(not has_rows)
        if not has_rows:
            if query:
                self.members_empty_state.set_content(
                    icon_name="search",
                    title="No matching requests",
                    guidance="No request in this collection matches this search.",
                    action_text="Clear Search",
                    action_callback=self.member_search.clear,
                )
            else:
                self.members_empty_state.set_content(
                    icon_name="bookmark",
                    title="No requests in this collection",
                    guidance="Add a saved request to build this collection.",
                    action_text="Add Saved Request",
                    action_callback=self.add_request,
                )

    @staticmethod
    def _matches(
        request: SavedRequest | None, endpoint: Any | None, request_id: str, query: str
    ) -> bool:
        haystack = [request_id]
        if request is not None:
            haystack += [request.name, request.endpoint_id]
        if endpoint is not None:
            haystack += [
                str(getattr(endpoint, "method", "")),
                str(getattr(endpoint, "path", "")),
            ]
        return any(query in part.lower() for part in haystack if part)

    def _current_member_id(self) -> str:
        item = self.members.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else ""

    def _current_member_index(self) -> int:
        """The selected row's position in the collection, not in the view."""
        item = self.members.currentItem()
        if item is None:
            return -1
        value = item.data(Qt.ItemDataRole.UserRole + 1)
        return int(value) if value is not None else -1

    def _update_actions(self) -> None:
        filtered = bool(self.member_search.text().strip())
        has_member = self.members.currentItem() is not None
        for label in ("Up", "Down"):
            button = self._buttons.get(label)
            if button is None:
                continue
            # Reordering a filtered view would move a request relative to rows
            # the user cannot see, so it is withheld until the search clears.
            button.setEnabled(has_member and not filtered)
            button.setToolTip(
                "Clear the search to reorder requests"
                if filtered
                else next(a.tooltip for a in self._actions if a.caption == label)
            )

    # ------------------------------------------------------- mini handler

    def _collection_changed(self, *_args) -> None:
        if self._rebuilding:
            return
        self.close_handler()
        self.refresh_members()

    def _member_changed(self, *_args) -> None:
        if self._rebuilding:
            # List rebuilds emit a null selection then re-select. Acting on
            # that would collapse and reload the handler on every refresh.
            return
        self._update_actions()
        request_id = self._current_member_id()
        request = self.store.requests.get(request_id) if request_id else None
        if request is None:
            self.close_handler()
            return
        if request_id == self._selected_request_id:
            # The same request is still selected, so keep the response and
            # any payload edit the user had in progress.
            return
        endpoint = self._endpoints.get(request.endpoint_id)
        self._selected_request_id = request_id
        self.handler.load(request, endpoint)
        self._animate_panel(self.HANDLER_WIDTH)

    def close_handler(self) -> None:
        self._selected_request_id = ""
        self._animate_panel(0)

    def focus_request(self, request_id: str) -> bool:
        """Selects ``request_id`` and opens its handler. Returns success.

        Both searches are cleared first: the row the caller wants may be
        filtered out of the view, and silently selecting a hidden row would
        leave the page looking as though nothing happened.
        """
        owner = next(
            (
                collection
                for collection in self.store.collections.values()
                if request_id in collection.request_ids
            ),
            None,
        )
        if owner is None or request_id not in self.store.requests:
            return False
        self.collection_search.clear()
        self.member_search.clear()
        for row in range(self.collections.count()):
            item = self.collections.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole)) == owner.id:
                self.collections.setCurrentItem(item)
                break
        else:
            return False
        self.refresh_members()
        for row in range(self.members.count()):
            item = self.members.item(row)
            if str(item.data(Qt.ItemDataRole.UserRole)) == request_id:
                self.members.setCurrentItem(item)
                self.members.scrollToItem(item)
                return True
        return False

    def _animate_panel(self, target: int) -> None:
        if self._animation.state() == QPropertyAnimation.State.Running:
            self._animation.stop()
        if self.handler_panel.width() == target:
            return
        self._animation.setStartValue(self.handler_panel.width())
        self._animation.setEndValue(target)
        self._animation.start()

    def handler_busy(self, busy: bool) -> None:
        self.handler.set_busy(busy)

    def show_handler_result(self, result: Any) -> None:
        self.handler.show_result(result)
        # The run just happened, so the row's "used" line is now stale.
        self.refresh_members()

    def show_handler_error(self, message: str) -> None:
        self.handler.show_error(message)

    # -------------------------------------------------------------- edits

    def create(self) -> None:
        name, accepted = QInputDialog.getText(self, "New collection", "Name")
        if accepted and name.strip():
            self.store.upsert_collection(RequestCollection(name=name.strip()))
            self.refresh()

    def rename(self) -> None:
        collection_id = self.current_collection_id()
        if not collection_id:
            return
        collection = self.store.collections[collection_id]
        name, accepted = QInputDialog.getText(
            self, "Rename collection", "Name", text=collection.name
        )
        if accepted and name.strip():
            collection.name = name.strip()
            self.store.upsert_collection(collection)
            self.refresh()

    def delete(self) -> None:
        collection_id = self.current_collection_id()
        if collection_id:
            self.store.delete_collection(collection_id)
            self.close_handler()
            self.refresh()

    def add_request(self) -> None:
        collection_id = self.current_collection_id()
        requests = sorted(self.store.requests.values(), key=lambda item: item.name.lower())
        if not collection_id or not requests:
            QMessageBox.information(
                self, "No saved requests", "Save a request before adding it to a collection."
            )
            return
        labels = [f"{item.name} - {item.endpoint_id}" for item in requests]
        selected, accepted = QInputDialog.getItem(
            self, "Add saved request", "Request", labels, 0, False
        )
        if accepted and selected in labels:
            self.store.add_to_collection(
                collection_id, requests[labels.index(selected)].id
            )
            self.refresh()

    def remove_request(self) -> None:
        collection_id = self.current_collection_id()
        index = self._current_member_index()
        if collection_id and index >= 0:
            self.store.remove_from_collection(collection_id, index)
            self.close_handler()
            self.refresh()

    def move_request(self, offset: int) -> None:
        collection_id = self.current_collection_id()
        index = self._current_member_index()
        if not collection_id or index < 0:
            return
        self.store.move_in_collection(collection_id, index, offset)
        self.refresh_members()
        target = index + offset
        for row in range(self.members.count()):
            item = self.members.item(row)
            if int(item.data(Qt.ItemDataRole.UserRole + 1)) == target:
                self.members.setCurrentRow(row)
                break

    def run(self) -> None:
        collection_id = self.current_collection_id()
        if not collection_id:
            return
        request_ids = [
            item.id for item in self.store.collection_requests(collection_id)
        ]
        if request_ids:
            self.store.mark_collection_used(collection_id)
            self.refresh()
            self.run_collection_requested.emit(request_ids)
