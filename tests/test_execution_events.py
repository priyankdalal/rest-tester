from __future__ import annotations

import threading
import time

from api_tester.execution.events import BatchingCoordinator, EventBus
from api_tester.execution.models import ExecutionEvent


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_event_bus_publish_and_drain_preserves_order() -> None:
    bus = EventBus()
    events = [
        ExecutionEvent(kind="run_started", run_id="DR-1", payload={"index": 1}),
        ExecutionEvent(kind="row_started", run_id="DR-1", payload={"index": 2}),
        ExecutionEvent(kind="row_completed", run_id="DR-1", payload={"index": 3}),
    ]
    for event in events:
        bus.publish(event)

    assert bus.drain() == events
    assert bus.drain() == []


def test_event_bus_drops_oldest_when_full() -> None:
    bus = EventBus(maxsize=3)
    for index in range(5):
        bus.publish(ExecutionEvent(kind="metric_snapshot", run_id="DR-1", payload={"index": index}))

    drained = bus.drain()
    assert [event.payload["index"] for event in drained] == [2, 3, 4]
    assert len(drained) == 3
    assert bus.dropped_count == 2


def test_batching_coordinator_batches_multithreaded_publishes_and_flushes_on_stop() -> None:
    bus = EventBus()
    received: list[ExecutionEvent] = []
    lock = threading.Lock()

    def on_batch(events: list[ExecutionEvent]) -> None:
        with lock:
            received.extend(events)

    coordinator = BatchingCoordinator(bus, on_batch, interval_seconds=0.05)
    coordinator.start()

    total_events = 12

    def publisher(offset: int) -> None:
        for value in range(offset, offset + 4):
            bus.publish(ExecutionEvent(kind="row_completed", run_id="DR-1", payload={"index": value}))

    threads = [threading.Thread(target=publisher, args=(start,)) for start in (0, 4, 8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert _wait_until(lambda: len(received) >= total_events, timeout=2.0)

    bus.publish(ExecutionEvent(kind="run_finished", run_id="DR-1", payload={"index": 999}))
    coordinator.stop()

    with lock:
        indexes = [event.payload["index"] for event in received]
    assert len(received) >= total_events + 1
    assert 999 in indexes


def test_batching_coordinator_keeps_running_after_callback_error() -> None:
    bus = EventBus()
    delivered: list[ExecutionEvent] = []
    callback_count = 0
    lock = threading.Lock()

    def on_batch(events: list[ExecutionEvent]) -> None:
        nonlocal callback_count
        with lock:
            callback_count += 1
            delivered.extend(events)
            current_count = callback_count
        if current_count == 1:
            raise RuntimeError("boom")

    coordinator = BatchingCoordinator(bus, on_batch, interval_seconds=0.05)
    coordinator.start()
    bus.publish(ExecutionEvent(kind="request_completed", run_id="DR-1", payload={"index": 1}))
    assert _wait_until(lambda: callback_count >= 1, timeout=2.0)

    bus.publish(ExecutionEvent(kind="request_completed", run_id="DR-1", payload={"index": 2}))
    assert _wait_until(lambda: callback_count >= 2, timeout=2.0)
    coordinator.stop()

    with lock:
        indexes = [event.payload["index"] for event in delivered]
    assert indexes == [1, 2]
