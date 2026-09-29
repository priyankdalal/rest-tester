"""A bounded, in-memory record of what authentication actually did.

Authentication is the part of a request a tester cannot see: a token is
acquired, reused, refreshed, or rejected somewhere between pressing Send
and the request leaving the process. When a call comes back 401 the useful
question is "which credential was used, where did it come from, and when
does it expire" - and until now nothing answered it.

The log deliberately holds **event metadata only**, never credential
values. Entries are copied into exports and bug reports, so treating them
as non-sensitive has to be an enforced property rather than a habit. Live
credential values are inspected separately and are never written here.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional


#: Events worth recording. Kept small so the log stays readable.
ACQUIRED = "acquired"
CACHED = "cached"
REFRESHED = "refreshed"
SIGNED_IN = "signed in"
SIGNED_OUT = "signed out"
FAILED = "failed"
APPLIED = "applied"
#: One outbound HTTP call made *by* authentication (token endpoint, device
#: authorization, JWKS). Distinct from the request under test.
HTTP = "http"
#: A named step inside a multi-leg flow (PKCE challenge, browser redirect,
#: device polling), so an interactive grant can be followed leg by leg.
STEP = "step"

#: Entries retained per process. Bounded so a long soak run cannot grow
#: the log without limit.
MAX_ENTRIES = 500


def _safe_url(url: str) -> str:
    """Strips any query string before a URL is stored.

    Authorization and token endpoints carry ``code``, ``client_secret``, and
    ``refresh_token`` as query parameters. The log records where a call went,
    never what was sent with it, so the query is removed here rather than
    relying on every caller to remember.
    """
    if not url:
        return ""
    base, sep, _ = str(url).partition("?")
    return base + (" (+params)" if sep else "")


@dataclass(frozen=True)
class AuthEvent:
    """One thing that happened to one profile."""

    at: float
    event: str
    profile: str
    method: str
    environment: str = ""
    service: str = ""
    detail: str = ""
    duration_ms: float = 0.0
    #: Names only - never the values behind them.
    header_names: tuple[str, ...] = ()
    query_names: tuple[str, ...] = ()
    ok: bool = True
    #: Set for :data:`HTTP` events. ``url`` has any query string stripped,
    #: because token endpoints accept credentials there.
    http_method: str = ""
    url: str = ""
    status: int = 0
    #: Names of the form fields posted - never their values.
    sent_fields: tuple[str, ...] = ()

    @property
    def when(self) -> str:
        stamp = time.localtime(self.at)
        return time.strftime("%H:%M:%S", stamp)

    def summary(self) -> str:
        parts = []
        if self.http_method and self.url:
            leg = f"{self.http_method} {self.url}"
            if self.status:
                leg += f" -> {self.status}"
            parts.append(leg)
        if self.detail:
            parts.append(self.detail)
        if self.sent_fields:
            parts.append("sent " + ", ".join(self.sent_fields))
        placed = list(self.header_names) + [f"?{n}" for n in self.query_names]
        if placed:
            parts.append("via " + ", ".join(placed))
        if self.duration_ms:
            parts.append(f"{self.duration_ms:.0f} ms")
        return " - ".join(parts)


class AuthActivityLog:
    """Thread-safe ring buffer of :class:`AuthEvent`.

    Suite runs and the Data Runner acquire credentials from worker threads,
    so appends are locked; readers get a snapshot list rather than a live
    view, which keeps the UI from iterating a mutating deque.
    """

    def __init__(self, capacity: int = MAX_ENTRIES) -> None:
        self._entries: deque[AuthEvent] = deque(maxlen=max(1, capacity))
        self._lock = threading.Lock()

    def record(self, event: AuthEvent) -> None:
        with self._lock:
            self._entries.append(event)

    def add(
        self,
        event: str,
        profile: str,
        method: str,
        *,
        environment: str = "",
        service: str = "",
        detail: str = "",
        duration_ms: float = 0.0,
        header_names: Iterable[str] = (),
        query_names: Iterable[str] = (),
        ok: bool = True,
        at: Optional[float] = None,
        http_method: str = "",
        url: str = "",
        status: int = 0,
        sent_fields: Iterable[str] = (),
    ) -> AuthEvent:
        entry = AuthEvent(
            at=time.time() if at is None else at,
            event=event,
            profile=profile,
            method=method,
            environment=environment,
            service=service,
            detail=detail,
            duration_ms=duration_ms,
            header_names=tuple(header_names),
            query_names=tuple(query_names),
            ok=ok,
            http_method=http_method,
            url=_safe_url(url),
            status=status,
            sent_fields=tuple(sent_fields),
        )
        self.record(entry)
        return entry

    def entries(self, *, profile: str = "", newest_first: bool = True) -> list[AuthEvent]:
        with self._lock:
            found = list(self._entries)
        if profile:
            found = [entry for entry in found if entry.profile == profile]
        return list(reversed(found)) if newest_first else found

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


#: The log the application and manager share.
activity_log = AuthActivityLog()


#: The profile whose flow is currently executing. ``oauth2_client`` and the
#: signing helpers are pure modules with no profile of their own, so the
#: manager publishes the context here for the duration of a flow rather than
#: threading a logger through every function signature. A ContextVar keeps
#: concurrent suite workers from attributing each other's legs.
_current: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar(
    "auth_log_current", default=None
)


@contextlib.contextmanager
def flow(profile: str, method: str, *, environment: str = "", log=None):
    """Attributes any :func:`trace` call inside the block to one profile."""
    token = _current.set((log if log is not None else activity_log,
                          profile, method, environment))
    try:
        yield
    finally:
        _current.reset(token)


def trace(event: str, **fields) -> Optional[AuthEvent]:
    """Records a step or HTTP leg against the enclosing :func:`flow`.

    A no-op outside a flow, so the pure helpers stay usable - and unit
    testable - without a logging context.
    """
    current = _current.get()
    if current is None:
        return None
    log, profile, method, environment = current
    return log.add(event, profile, method, environment=environment, **fields)
