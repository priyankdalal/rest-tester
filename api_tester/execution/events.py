from __future__ import annotations

from collections import deque
import threading
from typing import Callable

from api_tester.execution.models import ExecutionEvent


class EventBus:
    """Thread-safe, bounded event queue with oldest-item drop semantics."""

    def __init__(self, maxsize: int = 20000) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be greater than zero")
        self._maxsize = int(maxsize)
        self._queue: deque[ExecutionEvent] = deque()
        self._lock = threading.Lock()
        self._dropped_count = 0

    @property
    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped_count

    def publish(self, event: ExecutionEvent) -> None:
        with self._lock:
            if len(self._queue) >= self._maxsize:
                self._queue.popleft()
                self._dropped_count += 1
            self._queue.append(event)

    def drain(self, max_items: int | None = None) -> list[ExecutionEvent]:
        if max_items is not None and max_items <= 0:
            return []
        drained: list[ExecutionEvent] = []
        with self._lock:
            limit = len(self._queue) if max_items is None else min(max_items, len(self._queue))
            for _ in range(limit):
                drained.append(self._queue.popleft())
        return drained

    def close(self) -> None:
        """No-op compatibility hook.

        The bus has no background resources; callers may simply stop publishing
        and draining when a run ends.
        """


class BatchingCoordinator:
    """Periodically drains an :class:`EventBus` and forwards non-empty batches."""

    def __init__(
        self,
        event_bus: EventBus,
        on_batch: Callable[[list[ExecutionEvent]], None],
        interval_seconds: float = 0.2,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be greater than zero")
        self._event_bus = event_bus
        self._on_batch = on_batch
        self._interval_seconds = float(interval_seconds)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="execution-event-batcher",
                daemon=True,
            )
            self._thread.start()

    def stop(self, join_timeout: float | None = None) -> None:
        thread: threading.Thread | None
        with self._thread_lock:
            thread = self._thread
        if thread is None:
            self._flush_once()
            return
        self._stop_event.set()
        timeout = join_timeout
        if timeout is None:
            timeout = max(1.0, self._interval_seconds * 2.0)
        thread.join(timeout=timeout)
        with self._thread_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            self._flush_once()
        self._flush_once()

    def _flush_once(self) -> None:
        events = self._event_bus.drain()
        if not events:
            return
        try:
            self._on_batch(events)
        except Exception as exc:  # pragma: no cover - behavior validated by tests
            print(f"BatchingCoordinator callback failed: {exc}")
