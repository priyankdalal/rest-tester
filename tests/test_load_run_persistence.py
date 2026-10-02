"""Load Studio run statistics, snapshot/report persistence and reopening saved runs."""

from __future__ import annotations

import sqlite3

import pytest

from api_tester.execution.events import EventBus
from api_tester.execution.persistence import RunStore, store_file_size
from api_tester.load_testing.engine import LoadEngine, LoadRunSummary
from api_tester.load_testing.planner import build_plan
from api_tester.load_testing.run_stats import (
    compute_run_statistics,
    downsample,
    overall_throughput,
    snapshots_from_samples,
    stage_breakdown,
)
from api_tester.load_testing.saved_runs import (
    SavedRunError,
    format_bytes,
    list_saved_runs,
    load_saved_run,
)
from api_tester.load_testing.scenario import LoadStage, ThresholdDefinition

from tests.test_load_testing_engine import _fast_options, _install_fake_request, _scenario


def _interval(elapsed, *, users, completed, errors=0, seconds=1.0, p99=100.0, p95=80.0, p50=40.0):
    return {
        "elapsed_seconds": elapsed,
        "active_users": users,
        "interval_seconds": seconds,
        "interval_completed": completed,
        "interval_errors": errors,
        "interval_throughput": completed / seconds,
        "interval_mean_ms": p50,
        "interval_p50_ms": p50,
        "interval_p95_ms": p95,
        "interval_p99_ms": p99,
        "throughput_per_second": completed / seconds,
    }


# ------------------------------------------------------------------ run_stats


def test_statistics_use_intervals_with_active_users_for_throughput_and_users() -> None:
    snapshots = [
        _interval(1.0, users=2, completed=10, p99=120.0),
        _interval(2.0, users=6, completed=40, errors=4, p99=300.0),
        _interval(3.0, users=4, completed=20, p99=180.0),
        # Ramp-down reached zero users: excluded so the minimum is not 0.
        _interval(4.0, users=0, completed=1, p99=50.0),
    ]
    final = {"completed": 71, "mean_ms": 55.0, "min_ms": 3.0, "max_ms": 900.0, "error_rate": 4 / 71}

    stats = compute_run_statistics(snapshots, final)

    assert (stats.throughput.minimum, stats.throughput.maximum) == (10.0, 40.0)
    assert stats.throughput.average == pytest.approx(70 / 3)
    assert (stats.active_users.minimum, stats.active_users.maximum) == (2, 6)
    assert stats.active_users.average == pytest.approx(4.0)
    # Latency uses every interval that completed requests, including the last.
    assert (stats.p99.minimum, stats.p99.maximum) == (50.0, 300.0)
    assert stats.p99.average == pytest.approx((120 + 300 + 180 + 50) / 4)
    assert (stats.latency_mean.minimum, stats.latency_mean.average, stats.latency_mean.maximum) == (3.0, 55.0, 900.0)
    assert stats.error_rate.maximum == pytest.approx(0.1)
    assert stats.interval_count == 4


def test_statistics_without_snapshots_are_unavailable() -> None:
    stats = compute_run_statistics([], {})
    assert not stats.available
    assert not stats.throughput.available
    assert not stats.latency_mean.available


def test_stage_breakdown_assigns_intervals_by_midpoint() -> None:
    stages = [
        {"kind": "ramp_up", "duration_seconds": 2.0, "start_users": 1, "end_users": 4, "label": "Ramp"},
        {"kind": "steady", "duration_seconds": 2.0, "start_users": 4, "end_users": 4},
    ]
    snapshots = [
        _interval(1.0, users=2, completed=10),
        _interval(2.0, users=4, completed=20, errors=2),
        _interval(3.0, users=4, completed=30, p95=200.0),
        _interval(4.4, users=4, completed=5, p95=100.0),  # tail past the end lands in the last stage
    ]

    ramp, steady = stage_breakdown(stages, snapshots)

    assert (ramp.label, ramp.requests, ramp.errors) == ("Ramp", 30, 2)
    assert ramp.average_throughput == pytest.approx(15.0)
    assert steady.requests == 35
    assert steady.p95_ms == pytest.approx(150.0)


def test_snapshots_from_samples_rebuilds_per_second_metrics() -> None:
    samples = [
        {"offset_ms": 100, "http_ms": 10.0, "outcome": "passed", "worker_id": 0},
        {"offset_ms": 900, "http_ms": 30.0, "outcome": "failed", "worker_id": 1},
        {"offset_ms": 2100, "http_ms": 20.0, "outcome": "passed", "worker_id": 0},
    ]

    snapshots, final = snapshots_from_samples(samples)

    assert [s["interval_completed"] for s in snapshots] == [2, 0, 1]
    assert snapshots[0]["active_users"] == 2
    assert snapshots[0]["interval_errors"] == 1
    assert final["completed"] == 3
    assert final["error_rate"] == pytest.approx(1 / 3)


def test_downsample_keeps_the_last_snapshot_within_the_limit() -> None:
    items = [{"elapsed_seconds": float(index)} for index in range(500)]
    thinned = downsample(items, 120)
    assert len(thinned) == 120
    assert thinned[0] is items[0]
    assert thinned[-1] is items[-1]
    assert downsample(items[:5], 120) == items[:5]


def test_overall_throughput_covers_the_whole_run() -> None:
    assert overall_throughput([{"elapsed_seconds": 4.0}], {"completed": 10}) == pytest.approx(2.5)
    assert overall_throughput([], {"completed": 10}) is None


# ------------------------------------------------------------------ persistence


def test_snapshots_and_reports_round_trip(tmp_path) -> None:
    store = RunStore(tmp_path / "runs.db")
    store.start_run("LT-1", kind="load_test", name="n", environment_id="e", environment_name="QA", started_at="2025-01-01T00:00:00+00:00", definition={})
    store.record_snapshot("LT-1", 1, 2.0, {"elapsed_seconds": 2.0, "completed": 4})
    store.record_snapshot("LT-1", 0, 1.0, {"elapsed_seconds": 1.0, "completed": 2})
    store.save_report("LT-1", {"summary": {"outcome": "PASS"}})

    assert [s["completed"] for s in store.list_snapshots("LT-1")] == [2, 4]
    assert store.get_report("LT-1") == {"summary": {"outcome": "PASS"}}
    assert store.get_report("missing") is None
    assert store.size_bytes() == store_file_size(tmp_path / "runs.db") > 0
    store.close()


def test_store_file_size_is_zero_for_missing_files(tmp_path) -> None:
    assert store_file_size(tmp_path / "nope.db") == 0


# ------------------------------------------------------------------ engine persistence


def _run_persisted(monkeypatch, tmp_path, **scenario_kwargs):
    _install_fake_request(monkeypatch)
    scenario = _scenario(
        stages=(LoadStage(kind="steady", duration_seconds=0.4, start_users=2, end_users=2, think_time_ms=10),),
        **scenario_kwargs,
    )
    store = RunStore(tmp_path / "engine.db")
    engine = LoadEngine(build_plan(scenario), store=store, event_bus=EventBus(), options=_fast_options())
    summary = engine.run()
    store.close()
    return engine, summary


def test_engine_persists_interval_snapshots_report_and_definition(monkeypatch, tmp_path) -> None:
    engine, summary = _run_persisted(
        monkeypatch, tmp_path, thresholds=(ThresholdDefinition(metric="error_rate", operator="<=", target=0.5),)
    )

    loaded = load_saved_run(tmp_path / "engine.db", engine.run_id)

    assert loaded.summary is not None
    assert loaded.summary.outcome == summary.outcome == "PASS"
    assert loaded.summary.threshold_results[0].passed
    assert loaded.snapshots, "metric snapshots should be persisted"
    assert {"interval_throughput", "interval_p99_ms", "interval_completed"} <= set(loaded.snapshots[0])
    assert "endpoints" not in loaded.snapshots[0]
    assert loaded.final_snapshot["completed"] == summary.total_requests
    assert engine.final_snapshot["completed"] == summary.total_requests
    definition = loaded.definition
    assert definition["endpoint_id"] == "brands.get"
    assert definition["stages"][0]["duration_seconds"] == 0.4
    assert definition["thresholds"][0]["metric"] == "error_rate"
    assert not loaded.reconstructed


def test_engine_never_persists_header_values(monkeypatch, tmp_path) -> None:
    from dataclasses import replace

    _install_fake_request(monkeypatch)
    scenario = _scenario(
        stages=(LoadStage(kind="steady", duration_seconds=0.2, start_users=1, end_users=1, think_time_ms=10),),
    )
    template = replace(scenario.template, values={**scenario.template.values, "header:Authorization": "Bearer secret"})
    scenario = replace(scenario, template=template)
    engine = LoadEngine(build_plan(scenario), options=_fast_options())

    definition = engine.run_definition()

    assert "header:Authorization" not in definition["values"]
    assert definition["values"]["path:Id"] == "1"


def test_summary_round_trips_through_dict() -> None:
    summary = LoadRunSummary(
        run_id="LT-1", outcome="FAIL", started_at="a", finished_at="b", total_requests=5,
        passed=3, failed=1, errored=1, peak_users=2, stop_reason="x",
    )
    assert LoadRunSummary.from_dict(summary.to_dict()) == summary


# ------------------------------------------------------------------ saved runs


def test_load_saved_run_falls_back_to_samples_for_older_files(monkeypatch, tmp_path) -> None:
    engine, summary = _run_persisted(monkeypatch, tmp_path)
    # Simulate a file written before snapshots and reports were persisted.
    connection = sqlite3.connect(tmp_path / "engine.db")
    connection.execute("DELETE FROM run_snapshots")
    connection.execute("DELETE FROM run_reports")
    connection.commit()
    connection.close()

    loaded = load_saved_run(tmp_path / "engine.db", engine.run_id)

    assert loaded.reconstructed
    assert loaded.summary is None
    assert loaded.outcome == summary.outcome
    assert loaded.final_snapshot["completed"] == summary.total_requests
    assert sum(s["interval_completed"] for s in loaded.snapshots) == summary.total_requests


def test_list_saved_runs_rejects_missing_and_invalid_files(tmp_path) -> None:
    with pytest.raises(SavedRunError):
        list_saved_runs(tmp_path / "missing.db")
    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(b"this is not a sqlite database" * 100)
    with pytest.raises(SavedRunError):
        list_saved_runs(bogus)


def test_list_saved_runs_only_returns_load_tests(tmp_path) -> None:
    store = RunStore(tmp_path / "mixed.db")
    store.start_run("DR-1", kind="data_runner", name="d", environment_id="e", environment_name="QA", started_at="2025-01-01T00:00:00", definition={})
    store.start_run("LT-1", kind="load_test", name="l", environment_id="e", environment_name="QA", started_at="2025-01-02T00:00:00", definition={})
    store.close()

    assert [run["run_id"] for run in list_saved_runs(tmp_path / "mixed.db")] == ["LT-1"]
    with pytest.raises(SavedRunError):
        load_saved_run(tmp_path / "mixed.db", "DR-1")


def test_format_bytes() -> None:
    assert format_bytes(512) == "512 B"
    assert format_bytes(2048) == "2.0 KB"
    assert format_bytes(3 * 1024 * 1024) == "3.0 MB"
