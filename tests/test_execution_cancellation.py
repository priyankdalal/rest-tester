from __future__ import annotations

import threading
import time

from api_tester.execution.cancellation import CancellationController


def test_should_stop_false_until_stop_called() -> None:
    controller = CancellationController()
    assert controller.should_stop() is False
    controller.stop()
    assert controller.should_stop() is True


def test_drain_deadline_elapsed_respects_timeout() -> None:
    controller = CancellationController(drain_timeout=0.05)
    controller.stop()
    assert controller.drain_deadline_elapsed() is False
    time.sleep(0.08)
    assert controller.drain_deadline_elapsed() is True


def test_drain_deadline_elapsed_false_before_stop() -> None:
    controller = CancellationController(drain_timeout=0.01)
    assert controller.drain_deadline_elapsed() is False


def test_pause_blocks_worker_until_resume() -> None:
    controller = CancellationController()
    controller.pause()
    assert controller.is_paused is True
    progressed = []

    def worker() -> None:
        controller.wait_if_paused(poll_interval=0.02)
        progressed.append(True)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.05)
    assert progressed == []
    controller.resume()
    thread.join(timeout=2)
    assert progressed == [True]


def test_stop_releases_a_paused_worker() -> None:
    controller = CancellationController()
    controller.pause()
    progressed = []

    def worker() -> None:
        controller.wait_if_paused(poll_interval=0.02)
        progressed.append(controller.should_stop())

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.05)
    controller.stop()
    thread.join(timeout=2)
    assert progressed == [True]


def test_stop_is_idempotent_and_keeps_first_timestamp() -> None:
    controller = CancellationController()
    controller.stop()
    first = controller.stop_requested_at
    time.sleep(0.02)
    controller.stop()
    assert controller.stop_requested_at == first
