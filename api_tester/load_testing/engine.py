"""Load Testing Studio headless execution engine (Phase 5 MVP).

Implements the closed-model virtual-user scheduler described in sections
6.5-6.7 of the architecture memory using a bounded, fixed-size thread pool
(one thread per peak virtual user) rather than spawning/retiring a thread
per ramp step. See :class:`LoadEngine` for the scheduling rationale.

This module has no PyQt dependency; Phase 6 wires a live dashboard on top
of the ``metric_snapshot``/``request_completed``/``run_finished`` events it
publishes through the shared :class:`~api_tester.execution.events.EventBus`.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from api_tester.execution.cancellation import CancellationController
from api_tester.execution.errors import ClassifiedError
from api_tester.execution.events import EventBus
from api_tester.execution.metrics import LatencySketch, MetricsAggregator
from api_tester.execution.models import ExecutionEvent, RequestSample, new_run_id
from api_tester.execution.persistence import RunStore
from api_tester.execution.transport import WorkerTransport

from .planner import LoadPlan, stage_at, target_active_users
from .scenario import ThresholdDefinition


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class LoadRunOptions:
    """Tunables for one load-test execution.

    ``batch_size`` is a persistence-efficiency knob only (see
    ``DataRunnerOptions.batch_size`` for the equivalent Data Runner
    setting). ``metric_snapshot_interval_seconds`` governs how often the
    controller thread publishes a ``metric_snapshot`` event and evaluates
    threshold-based stop conditions; section 7.2 recommends repainting
    charts "at most 4-10 times per second", well below the default here,
    which the UI layer can further coalesce.
    """

    batch_size: int = 100
    metric_snapshot_interval_seconds: float = 1.0
    drain_timeout_seconds: float = 10.0
    active_user_poll_seconds: float = 0.05


@dataclass
class ThresholdResult:
    definition: ThresholdDefinition
    observed_value: float
    passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "definition": self.definition.to_dict(),
            "observed_value": self.observed_value,
            "passed": self.passed,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ThresholdResult":
        return cls(
            definition=ThresholdDefinition.from_dict(value["definition"]),
            observed_value=float(value.get("observed_value", 0.0)),
            passed=bool(value.get("passed", False)),
        )


@dataclass
class LoadRunSummary:
    run_id: str
    outcome: str
    started_at: str
    finished_at: str
    total_requests: int
    passed: int
    failed: int
    errored: int
    peak_users: int
    stop_reason: str | None
    threshold_results: list[ThresholdResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_requests": self.total_requests,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "peak_users": self.peak_users,
            "stop_reason": self.stop_reason,
            "threshold_results": [result.to_dict() for result in self.threshold_results],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LoadRunSummary":
        return cls(
            run_id=str(value.get("run_id", "")),
            outcome=str(value.get("outcome", "")),
            started_at=str(value.get("started_at", "")),
            finished_at=str(value.get("finished_at", "")),
            total_requests=int(value.get("total_requests", 0)),
            passed=int(value.get("passed", 0)),
            failed=int(value.get("failed", 0)),
            errored=int(value.get("errored", 0)),
            peak_users=int(value.get("peak_users", 0)),
            stop_reason=value.get("stop_reason"),
            threshold_results=[
                ThresholdResult.from_dict(item) for item in value.get("threshold_results", [])
            ],
        )


def evaluate_thresholds(
    thresholds: tuple[ThresholdDefinition, ...],
    snapshot: dict[str, Any],
    auth_failures: int,
    http_500_count: int,
) -> list[ThresholdResult]:
    """Evaluates every configured threshold against one metrics snapshot.

    Kept as a free function (rather than only an engine method) so both the
    live loop and any offline/CI report tooling (Phase 8) can reuse it
    against a persisted snapshot without constructing a whole engine.
    """
    metric_values: dict[str, float] = {
        "p50_ms": float(snapshot.get("p50_ms", 0.0)),
        "p95_ms": float(snapshot.get("p95_ms", 0.0)),
        "p99_ms": float(snapshot.get("p99_ms", 0.0)),
        "mean_ms": float(snapshot.get("mean_ms", 0.0)),
        "max_ms": float(snapshot.get("max_ms", 0.0)),
        "error_rate": float(snapshot.get("error_rate", 0.0)),
        "throughput_per_second": float(snapshot.get("throughput_per_second", 0.0)),
        "auth_failures": float(auth_failures),
        "http_500_count": float(http_500_count),
    }
    results: list[ThresholdResult] = []
    for definition in thresholds:
        observed = metric_values.get(definition.metric, 0.0)
        results.append(
            ThresholdResult(definition=definition, observed_value=observed, passed=definition.evaluate(observed))
        )
    return results


class LoadEngine:
    """Executes a validated :class:`LoadPlan` with a bounded, fixed-size
    thread pool of virtual users (section 6.6's closed-workload scheduler).

    Rather than spawning/retiring one OS thread per second of ramp (which
    would create unbounded thread churn under long scenarios), this starts
    exactly ``plan.peak_users`` worker threads up front and gates each one
    on whether its index is below the *currently* scheduled active-user
    count (see
    :func:`api_tester.load_testing.planner.target_active_users`). Thread
    index ``i`` only executes iterations while ``i < active_count``;
    otherwise it waits (polled every ``active_user_poll_seconds``) without
    ever being interrupted mid-request, matching the section 4.5 stop
    contract: an in-flight request always finishes before a worker observes
    Stop.
    """

    def __init__(
        self,
        plan: LoadPlan,
        *,
        run_id: str | None = None,
        store: RunStore | None = None,
        event_bus: EventBus | None = None,
        cancellation: CancellationController | None = None,
        metrics: MetricsAggregator | None = None,
        options: LoadRunOptions | None = None,
    ) -> None:
        self.plan = plan
        self.run_id = run_id or new_run_id("LT")
        self.store = store
        self.event_bus = event_bus
        self.options = options or LoadRunOptions()
        self.cancellation = cancellation or CancellationController(drain_timeout=self.options.drain_timeout_seconds)
        self.metrics = metrics or MetricsAggregator()

        self._lock = threading.Lock()
        self._pending_samples: list[RequestSample] = []
        self._passed = 0
        self._failed = 0
        self._errored = 0
        self._auth_failures = 0
        self._http_500_count = 0
        self._total_requests = 0
        self._stop_reason: str | None = None
        # Per-interval latency/throughput: reset at every metric snapshot so
        # run statistics (min/avg/max of P99 etc.) describe each interval
        # rather than the slowly-moving cumulative percentiles.
        self._interval_latency = LatencySketch()
        self._interval_completed = 0
        self._interval_errors = 0
        self._interval_started = 0.0
        self._snapshot_seq = 0
        self._warnings: list[dict[str, Any]] = []
        #: Cumulative metrics at the end of the run (set once :meth:`run` returns).
        self.final_snapshot: dict[str, Any] = {}

    def _publish(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "warning":
            self._warnings.append(dict(payload))
        if self.event_bus is not None:
            self.event_bus.publish(ExecutionEvent(kind=kind, run_id=self.run_id, payload=payload))

    def _sleep_cancellable(self, seconds: float) -> None:
        """Sleeps up to ``seconds``, waking early (and often) enough to
        observe Stop between polls instead of oversleeping past it."""
        if seconds <= 0:
            return
        deadline = time.monotonic() + seconds
        poll = min(self.options.active_user_poll_seconds, seconds)
        while not self.cancellation.should_stop():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(poll, remaining))

    def _record_result(self, sample: RequestSample, classified: ClassifiedError | None) -> None:
        flush: list[RequestSample] | None = None
        with self._lock:
            self._total_requests += 1
            if sample.outcome == "passed":
                self._passed += 1
            elif sample.outcome == "failed":
                self._failed += 1
                if sample.status_code is not None and 500 <= sample.status_code < 600:
                    self._http_500_count += 1
            elif sample.outcome == "error":
                self._errored += 1
            if classified is not None and classified.category == "authentication":
                self._auth_failures += 1
            self.metrics.add_sample(sample)
            self._interval_latency.add(sample.http_ms)
            self._interval_completed += 1
            if sample.outcome in ("failed", "error"):
                self._interval_errors += 1
            self._pending_samples.append(sample)
            if len(self._pending_samples) >= self.options.batch_size:
                flush = self._pending_samples
                self._pending_samples = []
        if flush and self.store is not None:
            self.store.record_samples_batch(flush)
        if classified is not None and self.store is not None:
            self.store.record_error(self.run_id, classified, self.plan.scenario.endpoint.id, _now_iso())

    def _flush_remaining_samples(self) -> None:
        with self._lock:
            flush, self._pending_samples = self._pending_samples, []
        if flush and self.store is not None:
            self.store.record_samples_batch(flush)

    def _request_stop(self, reason: str) -> None:
        with self._lock:
            if self._stop_reason is None:
                self._stop_reason = reason
        self.cancellation.stop()

    def _vu_loop(self, vu_index: int, run_started: float) -> None:
        scenario = self.plan.scenario
        with WorkerTransport(scenario.environment, worker_id=vu_index, run_id=self.run_id) as transport:
            while not self.cancellation.should_stop():
                elapsed = time.monotonic() - run_started
                if elapsed >= scenario.total_duration_seconds:
                    return
                active = target_active_users(scenario, elapsed)
                if vu_index >= active:
                    time.sleep(self.options.active_user_poll_seconds)
                    continue

                stage = stage_at(scenario, elapsed)
                self._publish("request_started", {"worker_id": vu_index})
                result, sample, classified = transport.execute(scenario.endpoint, scenario.template)
                offset_ms = (time.monotonic() - run_started) * 1000.0
                sample = replace(sample, offset_ms=offset_ms, worker_id=vu_index)
                self._record_result(sample, classified)
                self._publish(
                    "request_completed" if sample.outcome == "passed" else "request_failed",
                    {"worker_id": vu_index, "status_code": sample.status_code, "outcome": sample.outcome},
                )

                think_time_ms = stage.think_time_ms if stage is not None else 0
                if think_time_ms > 0:
                    self._sleep_cancellable(think_time_ms / 1000.0)

    @property
    def warnings(self) -> list[dict[str, Any]]:
        """Warnings published so far (advisory plan issues, persistence failures)."""
        with self._lock:
            return list(self._warnings)

    def run_definition(self) -> dict[str, Any]:
        """The scenario as persisted with the run; enough to reopen or reuse it."""
        scenario = self.plan.scenario
        return {
            "endpoint_id": scenario.endpoint.id,
            "service": scenario.endpoint.service,
            "method": scenario.endpoint.method,
            "path": scenario.endpoint.path,
            "base_url": scenario.environment.base_url(scenario.endpoint.service),
            "scenario_name": scenario.name,
            "peak_users": self.plan.peak_users,
            "total_duration_seconds": scenario.total_duration_seconds,
            # Header values may carry credentials, so they are never persisted.
            "values": {
                key: value
                for key, value in scenario.template.values.items()
                if not key.startswith("header:")
                and not (key.startswith("file:") and ":header:" in key)
            },
            "payload": scenario.template.payload,
            "expected_status": scenario.template.expected_status,
            "workload_model": scenario.workload_model,
            "stages": [stage.to_dict() for stage in scenario.stages],
            "limits": scenario.limits.to_dict(),
            "thresholds": [t.to_dict() for t in scenario.thresholds],
        }

    def run(self) -> LoadRunSummary:
        scenario = self.plan.scenario
        started_at = _now_iso()

        if self.store is not None:
            self.store.start_run(
                self.run_id,
                kind="load_test",
                name=scenario.name or scenario.endpoint.id,
                environment_id=scenario.environment.environment_id,
                environment_name=scenario.environment.environment_name,
                started_at=started_at,
                definition=self.run_definition(),
            )

        if not self.plan.is_executable:
            return self._finish_without_running(started_at)

        for issue in self.plan.issues:  # advisory warnings only; blocking issues returned above
            self._publish("warning", {"severity": issue.severity, "message": issue.message})

        self._publish(
            "run_started",
            {"peak_users": self.plan.peak_users, "total_duration_seconds": scenario.total_duration_seconds},
        )

        run_started = time.monotonic()
        self._interval_started = run_started
        threads = [
            threading.Thread(
                target=self._vu_loop,
                args=(vu_index, run_started),
                name=f"load-vu-{vu_index}",
                daemon=True,
            )
            for vu_index in range(self.plan.peak_users)
        ]
        for thread in threads:
            thread.start()

        completed_naturally = False
        try:
            completed_naturally = self._drive_controller_loop(scenario, run_started)
        finally:
            # Always signal Stop here so every virtual-user thread observes it
            # and exits its loop promptly, whether the schedule finished
            # naturally, a safety limit tripped, or the caller cancelled.
            self.cancellation.stop()
            self._publish("run_stopping", {})
            for thread in threads:
                thread.join(timeout=self.options.drain_timeout_seconds)

        return self._finish_after_running(started_at, run_started, completed_naturally)

    def _drive_controller_loop(self, scenario, run_started: float) -> bool:
        """Runs the metric-snapshot/stop-condition loop until the scenario's
        total duration elapses or a stop is requested.

        Returns ``True`` only when the loop exited because the schedule
        finished naturally, so :meth:`_finish_after_running` can tell a
        clean completion apart from an internal safety-limit stop or an
        externally requested cancellation (both of which already leave
        :attr:`cancellation` in the stopped state before this returns).
        """
        while not self.cancellation.should_stop():
            elapsed = time.monotonic() - run_started
            if elapsed >= scenario.total_duration_seconds:
                return True
            self._sleep_cancellable(
                min(self.options.metric_snapshot_interval_seconds, scenario.total_duration_seconds - elapsed)
            )
            if self.cancellation.should_stop():
                return False
            snapshot = self.metrics.snapshot((time.monotonic() - run_started) * 1000.0)
            payload = {
                "elapsed_seconds": time.monotonic() - run_started,
                "active_users": target_active_users(scenario, time.monotonic() - run_started),
                **snapshot,
                **self._take_interval_metrics(),
            }
            self._publish("metric_snapshot", payload)
            self._persist_snapshot(payload)
            self._evaluate_stop_conditions(snapshot)
            if self.cancellation.should_stop():
                return False
        return False

    def _take_interval_metrics(self) -> dict[str, Any]:
        """Returns metrics for requests completed since the previous snapshot, then resets them."""
        now = time.monotonic()
        with self._lock:
            sketch, self._interval_latency = self._interval_latency, LatencySketch()
            completed, self._interval_completed = self._interval_completed, 0
            errors, self._interval_errors = self._interval_errors, 0
            seconds = max(now - self._interval_started, 1e-6)
            self._interval_started = now
        return {
            "interval_seconds": seconds,
            "interval_completed": completed,
            "interval_errors": errors,
            "interval_throughput": completed / seconds,
            "interval_mean_ms": sketch.mean,
            "interval_p50_ms": sketch.percentile(50.0),
            "interval_p95_ms": sketch.percentile(95.0),
            "interval_p99_ms": sketch.percentile(99.0),
        }

    def _persist_snapshot(self, payload: dict[str, Any]) -> None:
        if self.store is None:
            return
        stored = {key: value for key, value in payload.items() if key != "endpoints"}
        try:
            self.store.record_snapshot(self.run_id, self._snapshot_seq, float(payload["elapsed_seconds"]), stored)
        except Exception as exc:  # persistence must never abort a running load test
            self._publish("warning", {"severity": "warning", "message": f"Could not persist metrics snapshot: {exc}"})
        self._snapshot_seq += 1

    def _save_report(self, summary: "LoadRunSummary", final_snapshot: dict[str, Any]) -> None:
        if self.store is None:
            return
        report = {
            "summary": summary.to_dict(),
            "final_snapshot": {key: value for key, value in final_snapshot.items() if key != "endpoints"},
            "warnings": list(self._warnings),
            "auth_failures": self._auth_failures,
            "http_500_count": self._http_500_count,
        }
        try:
            self.store.save_report(self.run_id, report)
        except Exception:  # the run result is still returned to the caller
            pass

    def _evaluate_stop_conditions(self, snapshot: dict[str, Any]) -> None:
        limits = self.plan.scenario.limits
        if (
            limits.error_rate_stop_threshold is not None
            and snapshot["completed"] > 0
            and snapshot["error_rate"] > limits.error_rate_stop_threshold
        ):
            self._request_stop(
                f"Error rate {snapshot['error_rate']:.1%} exceeded the "
                f"{limits.error_rate_stop_threshold:.1%} stop threshold."
            )
            return
        if limits.latency_stop_ms is not None and snapshot["p95_ms"] > limits.latency_stop_ms:
            self._request_stop(
                f"P95 latency {snapshot['p95_ms']:.0f}ms exceeded the {limits.latency_stop_ms:.0f}ms stop threshold."
            )
            return
        if limits.auth_failure_stop and self._auth_failures > 0:
            self._request_stop("An authentication failure was observed and auth_failure_stop is enabled.")
            return
        if limits.max_total_requests is not None and self._total_requests >= limits.max_total_requests:
            self._request_stop(f"Reached the configured max_total_requests ceiling ({limits.max_total_requests}).")

    def _finish_without_running(self, started_at: str) -> LoadRunSummary:
        finished_at = _now_iso()
        for issue in self.plan.issues:
            self._publish("warning", {"severity": issue.severity, "message": issue.message})
        if self.store is not None:
            self.store.finish_run(self.run_id, finished_at, "ABORTED")
        self._publish("run_finished", {"outcome": "ABORTED"})
        summary = LoadRunSummary(
            run_id=self.run_id,
            outcome="ABORTED",
            started_at=started_at,
            finished_at=finished_at,
            total_requests=0,
            passed=0,
            failed=0,
            errored=0,
            peak_users=self.plan.peak_users,
            stop_reason="Scenario failed validation.",
            threshold_results=[],
        )
        self._save_report(summary, {})
        return summary

    def _finish_after_running(
        self, started_at: str, run_started: float, completed_naturally: bool
    ) -> LoadRunSummary:
        self._flush_remaining_samples()
        finished_at = _now_iso()

        with self._lock:
            total_requests = self._total_requests
            passed = self._passed
            failed = self._failed
            errored = self._errored
            stop_reason = self._stop_reason
            if stop_reason is None and not completed_naturally:
                # Cancellation was requested externally (e.g. a user pressed
                # Stop) rather than by one of our own safety-limit checks,
                # which always set a specific reason via _request_stop.
                stop_reason = "Run stopped by request."
                self._stop_reason = stop_reason

        final_snapshot = self.metrics.snapshot((time.monotonic() - run_started) * 1000.0)
        threshold_results = evaluate_thresholds(
            self.plan.scenario.thresholds, final_snapshot, self._auth_failures, self._http_500_count
        )

        outcome = self._final_outcome(total_requests, stop_reason, threshold_results)
        summary = LoadRunSummary(
            run_id=self.run_id,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
            total_requests=total_requests,
            passed=passed,
            failed=failed,
            errored=errored,
            peak_users=self.plan.peak_users,
            stop_reason=stop_reason,
            threshold_results=threshold_results,
        )
        self.final_snapshot = {key: value for key, value in final_snapshot.items() if key != "endpoints"}
        self._save_report(summary, final_snapshot)
        if self.store is not None:
            self.store.finish_run(self.run_id, finished_at, outcome)
        self._publish("run_finished", {"outcome": outcome, "stop_reason": stop_reason})
        return summary

    @staticmethod
    def _final_outcome(total_requests: int, stop_reason: str | None, threshold_results: list[ThresholdResult]) -> str:
        if stop_reason is not None:
            return "STOPPED"
        if total_requests == 0:
            return "INCONCLUSIVE"
        if any(not result.passed for result in threshold_results):
            return "FAIL"
        return "PASS"
