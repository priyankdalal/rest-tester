"""In-app notification centre behind the header bell.

The bell reports work that finished while the user was looking somewhere
else: a request that came back after they moved to another page, a Data
Runner or Load Studio job that ran to completion, and a catalog reload.

Each notification carries a *route* -- plain data describing where clicking
it should take the user -- rather than a callable. A stored callable would
capture widgets that may be rebuilt (the endpoint tree and the collection
member list both are), so the window resolves the route at click time
against whatever is on screen then.

Authentication activity keeps its own feed in :mod:`api_tester.auth_log`
and stays visible in the Auth Inspector; this module does not replace it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from itertools import count

from PyQt6.QtCore import QObject, pyqtSignal

#: Longest history the bell keeps. Older entries fall off the back.
MAX_ENTRIES = 50

# Notification kinds. These double as the icon lookup and the route tag.
REQUEST = "request"
DATA_RUNNER = "data-runner"
LOAD_TEST = "load-test"
CATALOG = "catalog"

#: Glyph drawn beside each entry, keyed by kind.
ICONS = {
    REQUEST: "send",
    DATA_RUNNER: "data-runner",
    LOAD_TEST: "load-testing",
    CATALOG: "renew",
}


def relative_time(at: float, *, now: float | None = None) -> str:
    """Formats an epoch timestamp as a short "2 min ago" phrase.

    Returns ``""`` when the value cannot be stated as a fact, so callers can
    hide the field rather than print a time they cannot prove.
    """
    if not at:
        return ""
    seconds = (time.time() if now is None else now) - at
    # A small negative drift between a write and a read is normal; a large
    # one means the clock moved and the stamp is not trustworthy.
    if seconds < -60:
        return ""
    if seconds < 45:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(round(minutes))} min ago"
    hours = minutes / 60
    if hours < 24:
        count_ = int(round(hours))
        return "1 hour ago" if count_ == 1 else f"{count_} hours ago"
    days = int(round(hours / 24))
    return "1 day ago" if days == 1 else f"{days} days ago"


_ids = count(1)


@dataclass(frozen=True)
class Notification:
    """One completed piece of background work."""

    kind: str
    title: str
    detail: str = ""
    ok: bool = True
    #: Route arguments resolved by the window when the entry is clicked.
    route: dict[str, object] = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    id: int = field(default_factory=lambda: next(_ids))

    @property
    def when(self) -> str:
        return relative_time(self.at)

    @property
    def icon_name(self) -> str:
        return ICONS.get(self.kind, "bell")

    def label(self) -> str:
        """Single-line menu text: title, then detail, then how long ago."""
        parts = [self.title]
        if self.detail:
            parts.append(self.detail)
        when = self.when
        if when:
            parts.append(when)
        return "  ·  ".join(parts)


class NotificationCenter(QObject):
    """Thread-safe store of :class:`Notification` with an unread count.

    Background threads may post, so the list is guarded by a lock. ``changed``
    is emitted after every mutation; Qt delivers it to the GUI thread.
    """

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._entries: list[Notification] = []
        self._unread: set[int] = set()

    def notify(
        self,
        kind: str,
        title: str,
        detail: str = "",
        *,
        ok: bool = True,
        route: dict[str, object] | None = None,
    ) -> Notification:
        entry = Notification(
            kind=kind, title=title, detail=detail, ok=ok, route=dict(route or {})
        )
        with self._lock:
            self._entries.append(entry)
            self._unread.add(entry.id)
            if len(self._entries) > MAX_ENTRIES:
                dropped = self._entries[:-MAX_ENTRIES]
                del self._entries[:-MAX_ENTRIES]
                self._unread.difference_update(item.id for item in dropped)
        self.changed.emit()
        return entry

    def entries(self, limit: int | None = None) -> list[Notification]:
        """Newest first."""
        with self._lock:
            items = list(reversed(self._entries))
        return items if limit is None else items[:limit]

    def is_unread(self, entry: Notification) -> bool:
        with self._lock:
            return entry.id in self._unread

    def unread_count(self) -> int:
        with self._lock:
            return len(self._unread)

    def mark_all_read(self) -> None:
        with self._lock:
            if not self._unread:
                return
            self._unread.clear()
        self.changed.emit()

    def clear(self) -> None:
        with self._lock:
            if not self._entries and not self._unread:
                return
            self._entries.clear()
            self._unread.clear()
        self.changed.emit()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


#: Shared centre used by the window and the pages that report completions.
notification_center = NotificationCenter()
