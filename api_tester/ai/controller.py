"""Runs AI work off the UI thread.

Local models take tens of seconds per answer, so every call goes through a
``QThread``. Cancellation is cooperative: the planner checks the flag between
steps and the result of an in-flight HTTP call is discarded.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

from .catalog_index import CatalogIndex
from .config import AiSettings, build_provider
from .planner import PlanCancelled, RequestPlanner, SuitePlanner
from .provider import LlmError, probe_hosted_connection
from .providers.hosted import HostedProvider
from .usage_store import TrackedProvider


class _Worker(QObject):
    progress = pyqtSignal(str)
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, job: Callable[["_Worker"], Any]) -> None:
        super().__init__()
        self._job = job
        self.cancel_event = threading.Event()

    @pyqtSlot()
    def run(self) -> None:
        try:
            result = self._job(self)
        except PlanCancelled:
            self.failed.emit("Cancelled.")
        except LlmError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, never crash the UI
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            if self.cancel_event.is_set():
                self.failed.emit("Cancelled.")
            else:
                self.finished.emit(result)


class AiTask(QObject):
    """One background job. Keep a reference until ``done`` is emitted."""

    progress = pyqtSignal(str)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)
    done = pyqtSignal()

    def __init__(self, job: Callable[["_Worker"], Any], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread()
        self._worker = _Worker(job)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress)
        self._worker.finished.connect(self.succeeded)
        self._worker.failed.connect(self.failed)
        self._worker.finished.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self.done)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._worker.cancel_event.set()

    def is_running(self) -> bool:
        return self._thread.isRunning()

    def wait(self, milliseconds: int = 0) -> bool:
        return self._thread.wait(milliseconds) if milliseconds else self._thread.wait()


def plan_request_task(
    prompt: str,
    settings: AiSettings,
    index: CatalogIndex,
    secrets: tuple[str, ...],
    parent: QObject | None = None,
) -> AiTask:
    def job(worker: _Worker):
        provider = build_provider(settings)
        tracked = TrackedProvider(provider, settings, "request")
        planner = RequestPlanner(
            tracked,
            index,
            settings,
            secrets=(*secrets, getattr(provider, "api_key", "")),
            progress=worker.progress.emit,
            is_cancelled=worker.cancel_event.is_set,
        )
        outcome = planner.plan(prompt)
        if outcome.ok and outcome.plan.status == "plan":
            tracked.store.mark_plan_ready(tracked.generation_id)
        return outcome

    return AiTask(job, parent)


def plan_suite_task(
    prompt: str,
    settings: AiSettings,
    index: CatalogIndex,
    secrets: tuple[str, ...],
    parent: QObject | None = None,
) -> AiTask:
    def job(worker: _Worker):
        provider = build_provider(settings)
        tracked = TrackedProvider(provider, settings, "suite")
        planner = SuitePlanner(
            tracked,
            index,
            settings,
            secrets=(*secrets, getattr(provider, "api_key", "")),
            progress=worker.progress.emit,
            is_cancelled=worker.cancel_event.is_set,
        )
        outcome = planner.plan(prompt)
        if outcome.ok and outcome.plan.status == "plan":
            tracked.store.mark_plan_ready(tracked.generation_id)
        return outcome

    return AiTask(job, parent)

def plan_workflow_task(
    prompt: str,
    settings: AiSettings,
    index: CatalogIndex,
    secrets: tuple[str, ...],
    *,
    mode: str,
    columns: tuple[str, ...] = (),
    parent: QObject | None = None,
) -> AiTask:
    from .workflow_plan import WorkflowPlanner

    def job(worker: _Worker):
        provider = build_provider(settings)
        tracked = TrackedProvider(provider, settings, mode)
        planner = WorkflowPlanner(
            tracked, index, settings, mode=mode, columns=columns,
            secrets=(*secrets, getattr(provider, "api_key", "")),
            progress=worker.progress.emit,
            is_cancelled=worker.cancel_event.is_set,
        )
        outcome = planner.plan(prompt)
        if outcome.ok and outcome.plan.status == "plan":
            tracked.store.mark_plan_ready(tracked.generation_id)
        return outcome

    return AiTask(job, parent)


def connection_test_task(settings: AiSettings, parent: QObject | None = None, *, api_key: str = "") -> AiTask:
    def job(_worker: _Worker):
        provider = build_provider(settings, api_key=api_key)
        if settings.provider == "ollama":
            return provider.check_connection(settings.planner_model, timeout_seconds=15.0)
        if isinstance(provider, HostedProvider):
            provider = provider.for_connection_check()
        tracked = TrackedProvider(provider, settings, "connection")
        return probe_hosted_connection(tracked, settings.planner_model)

    return AiTask(job, parent)
