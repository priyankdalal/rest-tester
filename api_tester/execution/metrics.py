from __future__ import annotations

from dataclasses import dataclass, field
import random
import statistics

from api_tester.execution.models import RequestSample


class LatencySketch:
    """Bounded-memory latency summary using reservoir sampling.

    Percentiles use ``statistics.quantiles(..., n=1000, method="inclusive")``
    over the retained reservoir and return the nearest 0.1-percent cut point.
    """

    def __init__(self, max_samples: int = 5000) -> None:
        if max_samples <= 0:
            raise ValueError("max_samples must be greater than zero")
        self._max_samples = int(max_samples)
        self._samples: list[float] = []
        self._count = 0
        self._total = 0.0
        self._min: float | None = None
        self._max: float | None = None

    @property
    def count(self) -> int:
        return self._count

    @property
    def retained_count(self) -> int:
        return len(self._samples)

    @property
    def min(self) -> float:
        return 0.0 if self._min is None else self._min

    @property
    def max(self) -> float:
        return 0.0 if self._max is None else self._max

    @property
    def mean(self) -> float:
        if self._count == 0:
            return 0.0
        return self._total / self._count

    def add(self, value_ms: float) -> None:
        value = float(value_ms)
        self._count += 1
        self._total += value
        if self._min is None or value < self._min:
            self._min = value
        if self._max is None or value > self._max:
            self._max = value
        if len(self._samples) < self._max_samples:
            self._samples.append(value)
            return
        replacement_index = random.randrange(self._count)
        if replacement_index < self._max_samples:
            self._samples[replacement_index] = value

    def percentile(self, p: float) -> float:
        if not 0.0 <= p <= 100.0:
            raise ValueError("percentile must be between 0 and 100")
        if self._count == 0:
            return 0.0
        if self._count == 1:
            return self._samples[0]
        if p == 0:
            return self.min
        if p == 100:
            return self.max
        quantiles = statistics.quantiles(self._samples, n=1000, method="inclusive")
        index = max(0, min(len(quantiles) - 1, round(p * 10) - 1))
        return float(quantiles[index])


@dataclass
class _Bucket:
    second: int | None = None
    completed: int = 0
    passed: int = 0
    failed: int = 0

    def reset(self, second: int) -> None:
        self.second = second
        self.completed = 0
        self.passed = 0
        self.failed = 0


@dataclass
class _EndpointMetrics:
    completed: int = 0
    passed: int = 0
    failed: int = 0
    errored: int = 0
    cancelled: int = 0
    latency: LatencySketch = field(default_factory=LatencySketch)

    def snapshot(self) -> dict[str, float | int]:
        return {
            "completed": self.completed,
            "passed": self.passed,
            "failed": self.failed,
            "errored": self.errored,
            "cancelled": self.cancelled,
            "p95_ms": self.latency.percentile(95.0),
            "error_rate": ((self.failed + self.errored) / self.completed) if self.completed else 0.0,
        }


class MetricsAggregator:
    """O(1)-ish rolling execution metrics over individual request samples."""

    _SERIES_FIELDS = {"completed", "passed", "failed"}

    def __init__(self, bucket_count: int = 120, sketch_max_samples: int = 5000) -> None:
        if bucket_count <= 0:
            raise ValueError("bucket_count must be greater than zero")
        self._bucket_count = int(bucket_count)
        self._sketch_max_samples = int(sketch_max_samples)
        self._latency = LatencySketch(max_samples=sketch_max_samples)
        self._completed = 0
        self._passed = 0
        self._failed = 0
        self._errored = 0
        self._cancelled = 0
        self._endpoints: dict[str, _EndpointMetrics] = {}
        self._buckets = [_Bucket() for _ in range(self._bucket_count)]
        self._current_second: int | None = None

    def add_sample(self, sample: RequestSample) -> None:
        bucket_second = int(sample.offset_ms // 1000)
        bucket = self._ensure_bucket(bucket_second)
        bucket.completed += 1
        self._completed += 1
        self._latency.add(sample.http_ms)

        endpoint_metrics = self._endpoints.get(sample.endpoint_id)
        if endpoint_metrics is None:
            endpoint_metrics = _EndpointMetrics(latency=LatencySketch(max_samples=self._sketch_max_samples))
            self._endpoints[sample.endpoint_id] = endpoint_metrics
        endpoint_metrics.completed += 1
        endpoint_metrics.latency.add(sample.http_ms)

        if sample.outcome == "passed":
            self._passed += 1
            bucket.passed += 1
            endpoint_metrics.passed += 1
        elif sample.outcome == "failed":
            self._failed += 1
            bucket.failed += 1
            endpoint_metrics.failed += 1
        elif sample.outcome == "error":
            self._errored += 1
            endpoint_metrics.errored += 1
        elif sample.outcome == "cancelled":
            self._cancelled += 1
            endpoint_metrics.cancelled += 1

    def snapshot(self, now_offset_ms: float) -> dict[str, object]:
        self._advance_to(int(now_offset_ms // 1000))
        return {
            "completed": self._completed,
            "passed": self._passed,
            "failed": self._failed,
            "errored": self._errored,
            "cancelled": self._cancelled,
            "p50_ms": self._latency.percentile(50.0),
            "p95_ms": self._latency.percentile(95.0),
            "p99_ms": self._latency.percentile(99.0),
            "min_ms": self._latency.min,
            "max_ms": self._latency.max,
            "mean_ms": self._latency.mean,
            "throughput_per_second": self._bucket_value(
                None if self._current_second is None else self._current_second - 1,
                "completed",
            ),
            "error_rate": ((self._failed + self._errored) / self._completed) if self._completed else 0.0,
            "endpoints": {
                endpoint_id: metrics.snapshot()
                for endpoint_id, metrics in sorted(self._endpoints.items())
            },
        }

    def series(self, field: str) -> list[tuple[int, int]]:
        if field not in self._SERIES_FIELDS:
            raise ValueError(f"Unknown series field: {field!r}")
        if self._current_second is None:
            return []
        start_second = max(0, self._current_second - self._bucket_count + 1)
        return [
            (second, self._bucket_value(second, field))
            for second in range(start_second, self._current_second + 1)
        ]

    def per_endpoint(self, endpoint_id: str) -> dict[str, float | int]:
        metrics = self._endpoints.get(endpoint_id)
        if metrics is None:
            return {
                "completed": 0,
                "passed": 0,
                "failed": 0,
                "errored": 0,
                "cancelled": 0,
                "p95_ms": 0.0,
                "error_rate": 0.0,
            }
        return metrics.snapshot()

    def _advance_to(self, second: int) -> None:
        if self._current_second is None:
            self._current_second = second
            self._buckets[second % self._bucket_count].reset(second)
            return
        if second <= self._current_second:
            return
        for next_second in range(self._current_second + 1, second + 1):
            self._buckets[next_second % self._bucket_count].reset(next_second)
        self._current_second = second

    def _ensure_bucket(self, second: int) -> _Bucket:
        self._advance_to(second)
        bucket = self._buckets[second % self._bucket_count]
        if bucket.second != second:
            bucket.reset(second)
        return bucket

    def _bucket_value(self, second: int | None, field: str) -> int:
        if second is None:
            return 0
        bucket = self._buckets[second % self._bucket_count]
        if bucket.second != second:
            return 0
        return getattr(bucket, field)
