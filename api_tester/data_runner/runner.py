"""Sequential Data Runner execution engine.

Wires the CSV/mapping plan (:mod:`api_tester.data_runner.planner`) to the
shared execution layer (transport, events, metrics, persistence,
cancellation). MVP execution is sequential per
``platform architecture guide: load-testing-and-data-runner.md``
section 10 — bounded parallel execution is a later, separate change once
sequential behavior is proven deterministic.
"""

from __future__ import annotations

import csv
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from api_tester.client import ApiResult, redact_headers
from api_tester.execution.cancellation import CancellationController
from api_tester.execution.errors import ClassifiedError
from api_tester.execution.events import EventBus
from api_tester.execution.metrics import MetricsAggregator
from api_tester.execution.models import ExecutionEvent, RequestSample, RequestTemplate, new_run_id
from api_tester.execution.persistence import RunStore
from api_tester.execution.transport import WorkerTransport

from .mapping import ResolvedRow, RowValidationIssue
from .planner import DataRunPlan

_OUTPUT_COLUMNS = (
    "_run_id",
    "_row_number",
    "_outcome",
    "_status_code",
    "_duration_ms",
    "_error_category",
    "_error",
    "_response_size",
    "_request_url",
    "_started_at",
    "_finished_at",
)

#: Response bodies are only retained in event payloads for non-passed rows, so a
#: 10,000-row run doesn't have to hold every successful JSON body in memory —
#: the live UI shows a placeholder for passed rows instead (see
#: ``platform architecture guide: load-testing-and-data-runner.md``).
_RETAINED_BODY_MAX_CHARS = 8000


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass
class DataRunnerOptions:
    """Tunables for one Data Runner execution.

    ``batch_size`` controls how many :class:`RequestSample` rows are written
    to the :class:`RunStore` in a single transaction; it is purely a
    persistence-efficiency knob, not a concurrency setting (execution itself
    is sequential in this version).
    """

    think_time_ms: int = 0
    batch_size: int = 50


@dataclass
class RowOutcome:
    """One row's execution result, kept for exports and the results table."""

    row_number: int
    correlation_key: str
    outcome: str  # "passed" | "failed" | "error" | "invalid" | "skipped" | "cancelled"
    status_code: int | None
    http_ms: float
    error_category: str | None
    error_message: str
    response_bytes: int
    started_at: str
    finished_at: str
    request_url: str
    issues: tuple[RowValidationIssue, ...] = field(default_factory=tuple)


@dataclass
class RunSummary:
    run_id: str
    outcome: str
    started_at: str
    finished_at: str
    total_rows: int
    executed: int
    passed: int
    failed: int
    errored: int
    invalid: int
    skipped: int
    stopped: bool


class DataRunner:
    """Executes a validated :class:`DataRunPlan` row by row."""

    def __init__(
        self,
        plan: DataRunPlan,
        *,
        run_id: str | None = None,
        store: RunStore | None = None,
        event_bus: EventBus | None = None,
        cancellation: CancellationController | None = None,
        metrics: MetricsAggregator | None = None,
        options: DataRunnerOptions | None = None,
    ) -> None:
        self.plan = plan
        self.run_id = run_id or new_run_id("DR")
        self.store = store
        self.event_bus = event_bus
        self.cancellation = cancellation or CancellationController()
        self.metrics = metrics or MetricsAggregator()
        self.options = options or DataRunnerOptions()
        self.row_outcomes: list[RowOutcome] = []

    def _publish(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_bus is not None:
            self.event_bus.publish(ExecutionEvent(kind=kind, run_id=self.run_id, payload=payload))

    @staticmethod
    def _build_executed_row_detail(
        row: dict[str, str],
        resolved: ResolvedRow,
        result: ApiResult | None,
        sample: RequestSample,
    ) -> dict[str, Any]:
        """Assembles the "Live results" master-detail payload for one executed
        row: the raw input row, the values actually resolved onto the
        request, and what was sent/received. Response bodies are only kept
        for non-passed outcomes to bound memory across large runs."""
        retained = sample.outcome != "passed"
        response_body = None
        if result is not None and retained:
            response_body = result.response_body[:_RETAINED_BODY_MAX_CHARS]
        return {
            "input_row": dict(row),
            "resolved_request": {
                "path": dict(resolved.path_values),
                "query": dict(resolved.query_values),
                "headers": redact_headers(dict(resolved.header_values)),
                "form": dict(resolved.form_values),
                "payload": resolved.payload,
            },
            "response": {
                "status_code": result.status_code if result is not None else None,
                "headers": redact_headers(dict(result.response_headers)) if result is not None else {},
                "body": response_body,
                "body_retained": retained,
                "content_type": result.content_type if result is not None else "",
            },
            "assertions": [
                {
                    "name": "Status code",
                    "expected": resolved.expected_status,
                    "actual": str(result.status_code) if result is not None else "(no response)",
                    "passed": sample.outcome == "passed",
                }
            ],
        }

    @staticmethod
    def _request_template_definition(plan: Any) -> dict[str, Any]:
        """Records that a request template seeded the run — keys only, since
        template values may hold header data that must not be persisted."""
        mapper = plan.mapper
        if mapper is None or (not mapper.template_values and mapper.template_payload is None):
            return {}
        return {
            "request_template": {
                "keys": sorted(mapper.template_values),
                "payload": mapper.template_payload is not None,
            }
        }

    def run(self) -> RunSummary:
        plan = self.plan
        started_at = _now_iso()

        if self.store is not None:
            self.store.start_run(
                self.run_id,
                kind="data_runner",
                name=plan.endpoint.id,
                environment_id=plan.environment.environment_id,
                environment_name=plan.environment.environment_name,
                started_at=started_at,
                definition={
                    "endpoint_id": plan.endpoint.id,
                    "csv_path": plan.csv_source.settings.path,
                    "mappings": [
                        {"column": m.column, "target_key": m.target_key} for m in plan.mappings
                    ],
                    **self._request_template_definition(plan),
                },
            )

        if not plan.is_executable:
            finished_at = _now_iso()
            for issue in plan.issues:
                self._publish("warning", {"severity": issue.severity, "message": issue.message})
            if self.store is not None:
                self.store.finish_run(self.run_id, finished_at, "ABORTED")
            self._publish("run_finished", {"outcome": "ABORTED", "total_rows": plan.total_row_count})
            return RunSummary(
                run_id=self.run_id,
                outcome="ABORTED",
                started_at=started_at,
                finished_at=finished_at,
                total_rows=plan.total_row_count,
                executed=0,
                passed=0,
                failed=0,
                errored=0,
                invalid=0,
                skipped=0,
                stopped=False,
            )

        self._publish("run_started", {"total_rows": plan.total_row_count})

        base_template = RequestTemplate(
            endpoint_id=plan.endpoint.id,
            service=plan.endpoint.service,
            method=plan.endpoint.method,
            path=plan.endpoint.path,
            expected_status=plan.default_expected_status,
        )
        request_url = f"{plan.environment.base_url(plan.endpoint.service)}{plan.endpoint.path}"

        passed = failed = errored = invalid = skipped = executed = 0
        pending_samples: list[RequestSample] = []
        pending_details: list[tuple[int, dict[str, Any]]] = []
        stopped = False

        with WorkerTransport(plan.environment, worker_id=0, run_id=self.run_id) as transport:
            for row_number, row in plan.csv_source.iter_rows():
                if self.cancellation.should_stop():
                    stopped = True
                    break
                self.cancellation.wait_if_paused()
                if self.cancellation.should_stop():
                    stopped = True
                    break

                row_started_at = _now_iso()
                assert plan.mapper is not None  # guarded by plan.is_executable above
                resolved = plan.mapper.resolve(row_number, row)
                self._publish(
                    "row_started",
                    {"row_number": row_number, "correlation_key": resolved.correlation_key},
                )

                if resolved.skip:
                    skipped += 1
                    self._record_non_executed_row(
                        row_number, row, resolved, "skipped", row_started_at, request_url
                    )
                    continue
                if not resolved.is_valid:
                    invalid += 1
                    self._record_non_executed_row(
                        row_number, row, resolved, "invalid", row_started_at, request_url
                    )
                    continue

                template = plan.mapper.build_request_template(base_template, resolved)
                self._publish(
                    "request_started",
                    {"row_number": row_number, "correlation_key": resolved.correlation_key},
                )
                result, sample, classified = transport.execute(plan.endpoint, template)
                sample = replace(
                    sample,
                    row_number=row_number,
                    correlation_key=resolved.correlation_key,
                )
                executed += 1
                if sample.outcome == "passed":
                    passed += 1
                elif sample.outcome == "failed":
                    failed += 1
                elif sample.outcome == "error":
                    errored += 1

                self.metrics.add_sample(sample)
                pending_samples.append(sample)
                if len(pending_samples) >= self.options.batch_size:
                    self._flush_samples(pending_samples)
                    pending_samples = []
                if classified is not None and self.store is not None:
                    self.store.record_error(self.run_id, classified, plan.endpoint.id, _now_iso())

                finished_row_at = _now_iso()
                self.row_outcomes.append(
                    RowOutcome(
                        row_number=row_number,
                        correlation_key=resolved.correlation_key,
                        outcome=sample.outcome,
                        status_code=sample.status_code,
                        http_ms=sample.http_ms,
                        error_category=sample.error_category,
                        error_message=sample.error_message,
                        response_bytes=sample.response_bytes,
                        started_at=row_started_at,
                        finished_at=finished_row_at,
                        request_url=request_url,
                    )
                )
                detail = self._build_executed_row_detail(row, resolved, result, sample)
                pending_details.append((row_number, detail))
                if len(pending_details) >= self.options.batch_size and self.store is not None:
                    self.store.record_row_details_batch(self.run_id, pending_details)
                    pending_details = []
                self._publish(
                    "request_completed" if sample.outcome == "passed" else "request_failed",
                    {
                        "row_number": row_number,
                        **sample.to_dict(),
                        "detail": detail,
                    },
                )

                if self.options.think_time_ms and not self.cancellation.should_stop():
                    time.sleep(self.options.think_time_ms / 1000.0)

        if pending_samples:
            self._flush_samples(pending_samples)
        if pending_details and self.store is not None:
            self.store.record_row_details_batch(self.run_id, pending_details)

        finished_at = _now_iso()
        if stopped:
            outcome = "STOPPED"
        elif executed == 0:
            outcome = "INCONCLUSIVE"
        elif failed or errored:
            outcome = "FAIL"
        else:
            outcome = "PASS"

        if self.store is not None:
            self.store.finish_run(self.run_id, finished_at, outcome)

        self._publish(
            "run_finished",
            {
                "outcome": outcome,
                "executed": executed,
                "passed": passed,
                "failed": failed,
                "errored": errored,
                "invalid": invalid,
                "skipped": skipped,
            },
        )

        return RunSummary(
            run_id=self.run_id,
            outcome=outcome,
            started_at=started_at,
            finished_at=finished_at,
            total_rows=plan.total_row_count,
            executed=executed,
            passed=passed,
            failed=failed,
            errored=errored,
            invalid=invalid,
            skipped=skipped,
            stopped=stopped,
        )

    def _record_non_executed_row(
        self,
        row_number: int,
        row: dict[str, str],
        resolved: ResolvedRow,
        outcome: str,
        started_at: str,
        request_url: str,
    ) -> None:
        finished_at = _now_iso()
        message = "; ".join(issue.message for issue in resolved.issues) if resolved.issues else ""
        self.row_outcomes.append(
            RowOutcome(
                row_number=row_number,
                correlation_key=resolved.correlation_key,
                outcome=outcome,
                status_code=None,
                http_ms=0.0,
                error_category="validation" if outcome == "invalid" else None,
                error_message=message,
                response_bytes=0,
                started_at=started_at,
                finished_at=finished_at,
                request_url=request_url,
                issues=tuple(resolved.issues),
            )
        )
        detail = {
            "input_row": dict(row),
            "resolved_request": {
                "path": dict(resolved.path_values),
                "query": dict(resolved.query_values),
                "headers": redact_headers(dict(resolved.header_values)),
                "form": dict(resolved.form_values),
                "payload": resolved.payload,
            },
            "response": None,
            "assertions": [
                {
                    "name": issue.message,
                    "expected": "",
                    "actual": "",
                    "passed": False,
                }
                for issue in resolved.issues
            ],
        }
        self._publish(
            "row_completed",
            {
                "row_number": row_number,
                "outcome": outcome,
                "correlation_key": resolved.correlation_key,
                "detail": detail,
            },
        )
        if self.store is not None:
            self.store.record_row_detail(self.run_id, row_number, detail)

    def _flush_samples(self, samples: list[RequestSample]) -> None:
        if self.store is not None:
            self.store.record_samples_batch(samples)

    def export_rows_csv(self, path: str) -> None:
        """Writes the reserved-column row export described in memory section 10.2.

        Configured response captures are not implemented yet (no capture
        support exists in the mapping layer); only the reserved diagnostic
        columns are written.
        """
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(_OUTPUT_COLUMNS)
            for row in self.row_outcomes:
                writer.writerow(
                    [
                        self.run_id,
                        row.row_number,
                        row.outcome,
                        row.status_code if row.status_code is not None else "",
                        row.http_ms,
                        row.error_category or "",
                        row.error_message,
                        row.response_bytes,
                        row.request_url,
                        row.started_at,
                        row.finished_at,
                    ]
                )
