"""Thread-safe cooperative cancellation shared by every execution engine.

Both Load Testing Studio and the Data Runner run work across multiple
worker threads driven from a single controller thread. Every worker must
poll :meth:`CancellationController.should_stop` between units of work
instead of being killed, so in-flight HTTP requests are never aborted mid
socket-read and cleanup/report generation can still run deterministically.
"""

from __future__ import annotations

import threading
import time


class CancellationController:
    """One instance per run, shared by the scheduler and every worker."""

    def __init__(self, drain_timeout: float = 10.0) -> None:
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set == "not paused" (Data Runner only)
        self._pause_event.set()
        self._stop_requested_at: float | None = None
        self._drain_timeout = drain_timeout
        self._lock = threading.Lock()

    # -- stop -----------------------------------------------------------
    def stop(self) -> None:
        with self._lock:
            if self._stop_requested_at is None:
                self._stop_requested_at = time.monotonic()
        self._stop_event.set()
        self._pause_event.set()  # never leave paused workers unable to observe Stop

    def should_stop(self) -> bool:
        return self._stop_event.is_set()

    @property
    def stop_requested_at(self) -> float | None:
        return self._stop_requested_at

    def drain_deadline_elapsed(self) -> bool:
        """True once in-flight work has had ``drain_timeout`` seconds to finish."""
        if self._stop_requested_at is None:
            return False
        return (time.monotonic() - self._stop_requested_at) >= self._drain_timeout

    def wait_for_drain(self, poll_interval: float = 0.05) -> None:
        """Blocks the calling (controller) thread until the drain window elapses."""
        while not self.drain_deadline_elapsed():
            time.sleep(poll_interval)

    # -- pause/resume (Data Runner: never interrupts an in-flight request) ----
    def pause(self) -> None:
        self._pause_event.clear()

    def resume(self) -> None:
        self._pause_event.set()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def wait_if_paused(self, poll_interval: float = 0.1) -> None:
        """Blocks a worker between iterations/rows while paused, unless stopped."""
        while not self._pause_event.is_set() and not self._stop_event.is_set():
            self._pause_event.wait(timeout=poll_interval)
