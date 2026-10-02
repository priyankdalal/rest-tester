"""Everything the exported Load Studio report needs, gathered into one object.

:class:`LoadReportData` is built from either the run that just finished or a
reopened saved run. It holds no Qt objects, so the diagnostics and PDF writer
can be tested with synthetic data. Request-sample aggregates are only present
when the run was persisted (:attr:`LoadReportData.samples_available`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from api_tester.execution.persistence import RunStore
from api_tester.load_testing.engine import LoadRunSummary, ThresholdResult
from api_tester.load_testing.run_stats import RunStatistics, StageStats, Stat, compute_run_statistics, stage_breakdown

#: Upper edges of the latency-distribution buckets; the last bucket is open-ended.
LATENCY_EDGES_MS = (100, 250, 500, 1000, 2000, 5000)
LATENCY_BUCKET_LABELS = ("< 100 ms", "100–250 ms", "250–500 ms", "0.5–1 s", "1–2 s", "2–5 s", "> 5 s")


@dataclass(frozen=True)
class StatusCount:
    status_code: int | None
    count: int
    first_seconds: float | None = None
    last_seconds: float | None = None


@dataclass(frozen=True)
class CategoryCount:
    category: str
    count: int
    first_seconds: float | None = None
    last_seconds: float | None = None


@dataclass(frozen=True)
class SampleAggregates:
    """Per-request facts that snapshots do not carry (persisted runs only)."""

    total: int
    latency_buckets: tuple[int, ...] = ()
    status_counts: tuple[StatusCount, ...] = ()
    categories: tuple[CategoryCount, ...] = ()
    queue_delay_ms: Stat = field(default_factory=Stat)
    response_bytes: Stat = field(default_factory=Stat)
    response_bytes_total: int = 0
    retries: int = 0
    error_samples: tuple[dict[str, Any], ...] = ()

    def status(self, predicate) -> list[StatusCount]:
        return [item for item in self.status_counts if item.status_code is not None and predicate(item.status_code)]

    def category(self, name: str) -> CategoryCount | None:
        return next((item for item in self.categories if item.category == name), None)

    @classmethod
    def from_store_dict(cls, raw: dict[str, Any]) -> "SampleAggregates":
        def stat(values: dict[str, Any]) -> Stat:
            if values.get("avg") is None:
                return Stat()
            return Stat(float(values["min"]), float(values["avg"]), float(values["max"]))

        return cls(
            total=int(raw.get("total", 0)),
            latency_buckets=tuple(int(value) for value in raw.get("latency_buckets") or ()),
            status_counts=tuple(StatusCount(**item) for item in raw.get("status_counts") or ()),
            categories=tuple(CategoryCount(**item) for item in raw.get("categories") or ()),
            queue_delay_ms=stat(raw.get("queue_delay_ms") or {}),
            response_bytes=stat(raw.get("response_bytes") or {}),
            response_bytes_total=int((raw.get("response_bytes") or {}).get("total") or 0),
            retries=int(raw.get("retries", 0)),
            error_samples=tuple(raw.get("error_samples") or ()),
        )


@dataclass
class LoadReportData:
    outcome: str
    summary: LoadRunSummary | None
    definition: dict[str, Any]
    snapshots: list[dict[str, Any]]
    stats: RunStatistics
    stages: list[StageStats]
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    environment_name: str = ""
    run_id: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    persisted_to: str = ""
    note: str = ""
    samples: SampleAggregates | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now().astimezone())

    @property
    def final(self) -> dict[str, Any]:
        return self.stats.final

    @property
    def samples_available(self) -> bool:
        return self.samples is not None and self.samples.total > 0

    @property
    def thresholds(self) -> list[ThresholdResult]:
        return list(self.summary.threshold_results) if self.summary is not None else []

    @property
    def method(self) -> str:
        return str(self.definition.get("method") or "").upper()

    @property
    def path(self) -> str:
        return str(self.definition.get("path") or self.definition.get("endpoint_id") or "")

    @property
    def service(self) -> str:
        return str(self.definition.get("service") or "")

    @property
    def base_url(self) -> str:
        return str(self.definition.get("base_url") or "")

    @property
    def title(self) -> str:
        name = self.definition.get("scenario_name") or self.definition.get("endpoint_id") or self.run_id or "Load run"
        return f"{name} — {self.environment_name}" if self.environment_name else str(name)

    @property
    def endpoint_text(self) -> str:
        return f"{self.method} {self.path}".strip() or "—"

    @property
    def total_requests(self) -> int:
        if self.summary is not None:
            return int(self.summary.total_requests)
        return int(self.final.get("completed", 0) or 0)

    @property
    def peak_users(self) -> int:
        if self.summary is not None and self.summary.peak_users:
            return int(self.summary.peak_users)
        return int(self.definition.get("peak_users", 0) or 0)

    @property
    def stop_reason(self) -> str:
        if self.summary is not None:
            return self.summary.stop_reason or "Scenario completed without a stop condition."
        return self.note or "No final summary was saved for this run."

    @property
    def elapsed_seconds(self) -> float:
        if self.snapshots:
            return float(self.snapshots[-1].get("elapsed_seconds", 0.0) or 0.0)
        return float(self.duration_seconds or 0.0)

    def users_at(self, elapsed_seconds: float) -> int:
        """Scheduled virtual users at ``elapsed_seconds`` according to the snapshots."""
        best: dict[str, Any] | None = None
        for snapshot in self.snapshots:
            if float(snapshot.get("elapsed_seconds", 0.0)) >= elapsed_seconds:
                best = snapshot
                break
        if best is None and self.snapshots:
            best = self.snapshots[-1]
        return int((best or {}).get("active_users", 0) or 0)

    def error_offset_seconds(self, timestamp: str | None) -> float | None:
        """Converts an error's ISO ``first_seen``/``last_seen`` into seconds after the run started."""
        if not timestamp or not self.started_at:
            return None
        try:
            return (datetime.fromisoformat(timestamp) - datetime.fromisoformat(self.started_at)).total_seconds()
        except (TypeError, ValueError):
            return None


def mask_base_url(url: str, mode: str = "full") -> str:
    """Base URL as printed in the report. User info and query strings are always removed."""
    if not url:
        return "—"
    if mode == "hidden":
        return "(hidden)"
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    if mode == "host":
        return urlunsplit((parts.scheme, host, "", "", "")) or url
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def load_sample_aggregates(path: str | Path | None, run_id: str) -> SampleAggregates | None:
    """Reads :meth:`RunStore.sample_aggregates` for ``run_id``; ``None`` when unavailable."""
    if not path or not run_id or not Path(path).is_file():
        return None
    try:
        store = RunStore(path)
    except sqlite3.DatabaseError:
        return None
    try:
        raw = store.sample_aggregates(run_id, LATENCY_EDGES_MS)
    except sqlite3.DatabaseError:
        return None
    finally:
        store.close()
    return SampleAggregates.from_store_dict(raw) if raw else None


def build_report_data(
    *,
    outcome: str,
    summary: LoadRunSummary | None,
    definition: dict[str, Any],
    snapshots: list[dict[str, Any]],
    final_snapshot: dict[str, Any] | None = None,
    errors: list[dict[str, Any]] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    environment_name: str = "",
    run_id: str = "",
    started_at: str | None = None,
    finished_at: str | None = None,
    duration_seconds: float | None = None,
    persisted_to: str = "",
    note: str = "",
    samples: SampleAggregates | None = None,
) -> LoadReportData:
    stats = compute_run_statistics(snapshots, final_snapshot)
    return LoadReportData(
        outcome=outcome,
        summary=summary,
        definition=dict(definition or {}),
        snapshots=list(snapshots),
        stats=stats,
        stages=stage_breakdown(list((definition or {}).get("stages") or []), snapshots),
        errors=list(errors or []),
        warnings=list(warnings or []),
        environment_name=environment_name,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        persisted_to=persisted_to,
        note=note,
        samples=samples,
    )
