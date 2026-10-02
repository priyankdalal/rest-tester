"""Run statistics for Load Studio: min/avg/max per metric and per-stage
breakdowns, computed from the periodic ``metric_snapshot`` payloads.

Pure functions only (no Qt), so the live dashboard, a run reopened from a
SQLite store, and tests all share one implementation.

Snapshots carry two kinds of values:

* cumulative ones (``p99_ms``, ``mean_ms``, ``error_rate`` ...) describing the
  whole run so far, used for the report's *Final* column; and
* per-interval ones (``interval_p99_ms``, ``interval_throughput`` ...) covering
  only the requests completed since the previous snapshot. Min/avg/max are
  taken over these, because extremes of a cumulative percentile say little.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from api_tester.execution.metrics import LatencySketch


@dataclass(frozen=True)
class Stat:
    minimum: float | None = None
    average: float | None = None
    maximum: float | None = None

    @property
    def available(self) -> bool:
        return self.average is not None


def _stat(values: Iterable[float], average: float | None = None) -> Stat:
    items = [float(value) for value in values]
    if not items:
        return Stat()
    return Stat(min(items), average if average is not None else sum(items) / len(items), max(items))


@dataclass(frozen=True)
class StageStats:
    index: int
    label: str
    kind: str
    duration_seconds: float
    start_users: int
    end_users: int
    requests: int
    average_throughput: float | None
    p95_ms: float | None
    p99_ms: float | None
    errors: int


@dataclass(frozen=True)
class RunStatistics:
    throughput: Stat = field(default_factory=Stat)
    latency_mean: Stat = field(default_factory=Stat)
    p50: Stat = field(default_factory=Stat)
    p95: Stat = field(default_factory=Stat)
    p99: Stat = field(default_factory=Stat)
    active_users: Stat = field(default_factory=Stat)
    error_rate: Stat = field(default_factory=Stat)
    final: dict[str, Any] = field(default_factory=dict)
    interval_count: int = 0

    @property
    def available(self) -> bool:
        return self.interval_count > 0


def _interval_throughput(snapshot: dict[str, Any]) -> float:
    if "interval_throughput" in snapshot:
        return float(snapshot["interval_throughput"])
    return float(snapshot.get("throughput_per_second", 0.0))


def compute_run_statistics(
    snapshots: list[dict[str, Any]], final_snapshot: dict[str, Any] | None = None
) -> RunStatistics:
    """Aggregates snapshots into card/report statistics.

    Intervals with no scheduled users (after ramp-down reaches zero) are left
    out of throughput and user statistics, otherwise every minimum would be 0.
    Latency statistics only use intervals that completed at least one request.
    """
    final = dict(final_snapshot or (snapshots[-1] if snapshots else {}))
    active = [s for s in snapshots if int(s.get("active_users", 0)) > 0]
    with_requests = [s for s in snapshots if int(s.get("interval_completed", 0)) > 0]

    seconds = sum(float(s.get("interval_seconds", 0.0)) for s in active)
    completed = sum(int(s.get("interval_completed", 0)) for s in active)
    throughput_average = completed / seconds if seconds > 0 else None
    throughput = _stat((_interval_throughput(s) for s in active), throughput_average)

    # Per request: the fastest request, the mean of every request, and the slowest.
    mean_value = final.get("mean_ms")
    if mean_value is not None and final.get("completed"):
        latency_mean = Stat(
            float(final.get("min_ms", mean_value)), float(mean_value), float(final.get("max_ms", mean_value))
        )
    else:
        latency_mean = Stat()

    def latency(key: str) -> Stat:
        return _stat(float(s.get(f"interval_{key}", 0.0)) for s in with_requests)

    error_rate = _stat(
        int(s.get("interval_errors", 0)) / int(s["interval_completed"]) for s in with_requests
    )
    users = _stat(int(s.get("active_users", 0)) for s in active)
    return RunStatistics(
        throughput=throughput,
        latency_mean=latency_mean,
        p50=latency("p50_ms"),
        p95=latency("p95_ms"),
        p99=latency("p99_ms"),
        active_users=users,
        error_rate=error_rate,
        final=final,
        interval_count=len(snapshots),
    )


def stage_breakdown(stages: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> list[StageStats]:
    """Splits snapshots into the scenario's stages by each interval's midpoint."""
    boundaries: list[tuple[float, float]] = []
    start = 0.0
    for stage in stages:
        end = start + float(stage.get("duration_seconds", 0.0))
        boundaries.append((start, end))
        start = end

    buckets: list[list[dict[str, Any]]] = [[] for _ in stages]
    for snapshot in snapshots:
        midpoint = float(snapshot.get("elapsed_seconds", 0.0)) - float(snapshot.get("interval_seconds", 0.0)) / 2
        for index, (low, high) in enumerate(boundaries):
            if low <= midpoint < high or (index == len(boundaries) - 1 and midpoint >= high):
                buckets[index].append(snapshot)
                break

    result: list[StageStats] = []
    for index, (stage, items) in enumerate(zip(stages, buckets)):
        requests = sum(int(s.get("interval_completed", 0)) for s in items)
        seconds = sum(float(s.get("interval_seconds", 0.0)) for s in items)
        with_requests = [s for s in items if int(s.get("interval_completed", 0)) > 0]
        p95 = _stat(float(s.get("interval_p95_ms", 0.0)) for s in with_requests)
        p99 = _stat(float(s.get("interval_p99_ms", 0.0)) for s in with_requests)
        result.append(
            StageStats(
                index=index + 1,
                label=str(stage.get("label") or ""),
                kind=str(stage.get("kind", "")),
                duration_seconds=float(stage.get("duration_seconds", 0.0)),
                start_users=int(stage.get("start_users", 0)),
                end_users=int(stage.get("end_users", 0)),
                requests=requests,
                average_throughput=requests / seconds if seconds > 0 else None,
                p95_ms=p95.average,
                p99_ms=p99.average,
                errors=sum(int(s.get("interval_errors", 0)) for s in items),
            )
        )
    return result


def downsample(snapshots: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Evenly thins ``snapshots`` to at most ``limit`` items, keeping the last one."""
    if limit <= 0 or len(snapshots) <= limit:
        return list(snapshots)
    step = (len(snapshots) - 1) / (limit - 1) if limit > 1 else len(snapshots)
    return [snapshots[min(len(snapshots) - 1, round(index * step))] for index in range(limit)]


def overall_throughput(snapshots: list[dict[str, Any]], final: dict[str, Any]) -> float | None:
    """Completed requests divided by the whole elapsed run time."""
    if not snapshots:
        return None
    elapsed = float(snapshots[-1].get("elapsed_seconds", 0.0))
    completed = int(final.get("completed", 0) or 0)
    return completed / elapsed if elapsed > 0 else None


def snapshots_from_samples(samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rebuilds one-second snapshots, plus a final cumulative snapshot, from
    persisted request samples.

    Used for stores written before snapshots were persisted. Active users are
    approximated by the number of distinct workers seen in each second.
    """
    by_second: dict[int, list[dict[str, Any]]] = {}
    overall = LatencySketch()
    counts = {"passed": 0, "failed": 0, "error": 0, "cancelled": 0}
    for sample in samples:
        second = int(float(sample.get("offset_ms") or 0.0) // 1000)
        by_second.setdefault(second, []).append(sample)
        overall.add(float(sample.get("http_ms") or 0.0))
        outcome = str(sample.get("outcome", ""))
        if outcome in counts:
            counts[outcome] += 1

    snapshots: list[dict[str, Any]] = []
    if by_second:
        for second in range(min(by_second), max(by_second) + 1):
            items = by_second.get(second, [])
            sketch = LatencySketch()
            for item in items:
                sketch.add(float(item.get("http_ms") or 0.0))
            errors = sum(1 for item in items if item.get("outcome") in ("failed", "error"))
            workers = {item.get("worker_id") for item in items if item.get("worker_id") is not None}
            snapshots.append(
                {
                    "elapsed_seconds": float(second + 1),
                    "active_users": len(workers),
                    "interval_seconds": 1.0,
                    "interval_completed": len(items),
                    "interval_errors": errors,
                    "interval_throughput": float(len(items)),
                    "interval_mean_ms": sketch.mean,
                    "interval_p50_ms": sketch.percentile(50.0),
                    "interval_p95_ms": sketch.percentile(95.0),
                    "interval_p99_ms": sketch.percentile(99.0),
                }
            )

    total = len(samples)
    final = {
        "completed": total,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errored": counts["error"],
        "cancelled": counts["cancelled"],
        "p50_ms": overall.percentile(50.0),
        "p95_ms": overall.percentile(95.0),
        "p99_ms": overall.percentile(99.0),
        "min_ms": overall.min,
        "max_ms": overall.max,
        "mean_ms": overall.mean,
        "error_rate": (counts["failed"] + counts["error"]) / total if total else 0.0,
        "throughput_per_second": snapshots[-1]["interval_throughput"] if snapshots else 0.0,
        "active_users": snapshots[-1]["active_users"] if snapshots else 0,
    }
    return snapshots, final
