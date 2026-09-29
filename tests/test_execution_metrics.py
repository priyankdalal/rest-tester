from __future__ import annotations

import pytest

from api_tester.execution.metrics import LatencySketch, MetricsAggregator
from api_tester.execution.models import RequestSample


def _sample(
    *,
    endpoint_id: str,
    outcome: str,
    offset_ms: float,
    http_ms: float,
    status_code: int | None = 200,
) -> RequestSample:
    return RequestSample(
        run_id="DR-1",
        endpoint_id=endpoint_id,
        service="TrialAuth",
        method="POST",
        outcome=outcome,
        started_at="2026-09-25T00:00:00Z",
        offset_ms=offset_ms,
        queue_delay_ms=0.0,
        http_ms=http_ms,
        status_code=status_code,
    )


def test_latency_sketch_reports_reasonable_percentiles_for_known_values() -> None:
    sketch = LatencySketch(max_samples=200)
    for value in range(1, 101):
        sketch.add(value)

    assert sketch.count == 100
    assert sketch.min == 1.0
    assert sketch.max == 100.0
    assert sketch.mean == pytest.approx(50.5)
    assert sketch.percentile(50) == pytest.approx(50.5, abs=1.0)
    assert sketch.percentile(95) == pytest.approx(95.0, abs=1.0)
    assert sketch.percentile(99) == pytest.approx(99.0, abs=1.0)


def test_latency_sketch_caps_retained_samples_but_keeps_total_count() -> None:
    sketch = LatencySketch(max_samples=10)
    for value in range(100):
        sketch.add(value)

    assert sketch.count == 100
    assert sketch.retained_count == 10


def test_metrics_aggregator_reports_counts_rates_and_endpoint_breakdown() -> None:
    aggregator = MetricsAggregator(bucket_count=10, sketch_max_samples=50)
    aggregator.add_sample(_sample(endpoint_id="users.create", outcome="passed", offset_ms=1000, http_ms=100))
    aggregator.add_sample(_sample(endpoint_id="users.create", outcome="failed", offset_ms=1400, http_ms=200, status_code=400))
    aggregator.add_sample(_sample(endpoint_id="teams.create", outcome="error", offset_ms=2200, http_ms=300, status_code=None))
    aggregator.add_sample(_sample(endpoint_id="teams.create", outcome="cancelled", offset_ms=2500, http_ms=400, status_code=None))

    snapshot = aggregator.snapshot(now_offset_ms=4000)

    assert snapshot["completed"] == 4
    assert snapshot["passed"] == 1
    assert snapshot["failed"] == 1
    assert snapshot["errored"] == 1
    assert snapshot["cancelled"] == 1
    assert snapshot["error_rate"] == pytest.approx(0.5)
    assert snapshot["min_ms"] == 100.0
    assert snapshot["max_ms"] == 400.0
    assert snapshot["mean_ms"] == pytest.approx(250.0)

    endpoints = snapshot["endpoints"]
    assert endpoints["users.create"]["completed"] == 2
    assert endpoints["users.create"]["failed"] == 1
    assert endpoints["users.create"]["error_rate"] == pytest.approx(0.5)
    assert endpoints["teams.create"]["completed"] == 2
    assert endpoints["teams.create"]["errored"] == 1
    assert endpoints["teams.create"]["cancelled"] == 1

    users = aggregator.per_endpoint("users.create")
    assert users["p95_ms"] == pytest.approx(195.0, abs=10.0)


def test_metrics_aggregator_tracks_throughput_series_and_evicts_old_buckets() -> None:
    aggregator = MetricsAggregator(bucket_count=3, sketch_max_samples=20)
    for sample in [
        _sample(endpoint_id="users.create", outcome="passed", offset_ms=100, http_ms=10),
        _sample(endpoint_id="users.create", outcome="passed", offset_ms=900, http_ms=20),
        _sample(endpoint_id="users.create", outcome="failed", offset_ms=1100, http_ms=30, status_code=400),
        _sample(endpoint_id="users.create", outcome="passed", offset_ms=2100, http_ms=40),
        _sample(endpoint_id="users.create", outcome="passed", offset_ms=2200, http_ms=50),
        _sample(endpoint_id="users.create", outcome="error", offset_ms=2300, http_ms=60, status_code=None),
        _sample(endpoint_id="users.create", outcome="passed", offset_ms=3100, http_ms=70),
    ]:
        aggregator.add_sample(sample)

    snapshot = aggregator.snapshot(now_offset_ms=4500)
    series = aggregator.series("completed")

    assert snapshot["throughput_per_second"] == 1
    assert [second for second, _ in series] == [2, 3, 4]
    assert dict(series) == {2: 3, 3: 1, 4: 0}


def test_metrics_aggregator_series_rejects_unknown_field() -> None:
    aggregator = MetricsAggregator()
    with pytest.raises(ValueError):
        aggregator.series("errored")
