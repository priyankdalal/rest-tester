"""Serializable Load Testing Studio scenario definitions (Definition layer).

Per section 4.1 of the architecture memory, definitions are plain,
JSON-serializable dataclasses with no live tokens or mutable runtime state.
Nothing here performs network I/O or scheduling -- see ``planner.py`` for
validation/scheduling math and ``engine.py`` for execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from api_tester.catalog import Endpoint
from api_tester.execution.models import ExecutionEnvironmentSnapshot, RequestTemplate, new_run_id

#: All stage kinds from section 6.3. Phase 5 (MVP) only needs warm_up,
#: ramp_up, steady, and ramp_down; step/spike/recovery/soak presets are
#: Phase 7. The dataclass accepts the full vocabulary now so later phases
#: do not need a breaking schema change.
STAGE_KINDS = (
    "warm_up",
    "ramp_up",
    "steady",
    "ramp_down",
    "step",
    "spike",
    "recovery",
    "soak",
)

#: Stage kinds implemented by the Phase 5 engine. Anything else is rejected
#: by the planner with a clear validation issue rather than silently
#: mis-scheduled.
MVP_STAGE_KINDS = ("warm_up", "ramp_up", "steady", "ramp_down")

#: Per section 6.7, closed (virtual users) and open (arrival rate) workloads
#: must never share ambiguous labels.
WORKLOAD_MODELS = ("closed_virtual_users",)

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

THRESHOLD_OPERATORS = ("<=", ">=", "<", ">", "==")

#: Metrics a :class:`ThresholdDefinition` may reference. Sourced from
#: ``MetricsAggregator.snapshot()`` plus the two auth/5xx-specific counters
#: the engine tracks itself (section 7.3).
THRESHOLD_METRICS = (
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "mean_ms",
    "max_ms",
    "error_rate",
    "throughput_per_second",
    "auth_failures",
    "http_500_count",
)


def new_scenario_id() -> str:
    return new_run_id("SCN")


@dataclass(frozen=True)
class LoadStage:
    """One stage of a closed-model virtual-user schedule.

    ``start_users``/``end_users`` are interpolated linearly across
    ``duration_seconds`` by the planner's schedule function; a flat stage
    (e.g. steady state) simply sets ``start_users == end_users``.
    """

    kind: str
    duration_seconds: float
    start_users: int
    end_users: int
    think_time_ms: int = 0
    counts_toward_sla: bool | None = None
    label: str = ""

    def __post_init__(self) -> None:
        if self.kind not in STAGE_KINDS:
            raise ValueError(f"Unknown load stage kind: {self.kind!r}")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")
        if self.start_users < 0 or self.end_users < 0:
            raise ValueError("start_users and end_users must not be negative")
        if self.think_time_ms < 0:
            raise ValueError("think_time_ms must not be negative")

    @property
    def counts_toward_sla_effective(self) -> bool:
        """Setup/warm-up/cleanup are excluded from primary SLA math by default
        (section 6.5) unless the author explicitly overrides it."""
        if self.counts_toward_sla is not None:
            return self.counts_toward_sla
        return self.kind != "warm_up"

    def peak_users(self) -> int:
        return max(self.start_users, self.end_users)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "duration_seconds": self.duration_seconds,
            "start_users": self.start_users,
            "end_users": self.end_users,
            "think_time_ms": self.think_time_ms,
            "counts_toward_sla": self.counts_toward_sla,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LoadStage":
        return cls(
            kind=value["kind"],
            duration_seconds=float(value["duration_seconds"]),
            start_users=int(value["start_users"]),
            end_users=int(value["end_users"]),
            think_time_ms=int(value.get("think_time_ms", 0)),
            counts_toward_sla=value.get("counts_toward_sla"),
            label=value.get("label", ""),
        )


@dataclass(frozen=True)
class SafetyLimits:
    """Load safety controls (section 8). Every field here must be checked by
    the planner *before* a single request is sent -- never discovered mid-run."""

    environment_permits_load_test: bool = False
    confirmed_write_endpoints: bool = False
    get_head_only: bool = False
    max_concurrency: int = 50
    max_duration_seconds: float = 600.0
    max_total_requests: int | None = None
    request_timeout_seconds: float = 30.0
    error_rate_stop_threshold: float | None = 0.5
    latency_stop_ms: float | None = None
    auth_failure_stop: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_permits_load_test": self.environment_permits_load_test,
            "confirmed_write_endpoints": self.confirmed_write_endpoints,
            "get_head_only": self.get_head_only,
            "max_concurrency": self.max_concurrency,
            "max_duration_seconds": self.max_duration_seconds,
            "max_total_requests": self.max_total_requests,
            "request_timeout_seconds": self.request_timeout_seconds,
            "error_rate_stop_threshold": self.error_rate_stop_threshold,
            "latency_stop_ms": self.latency_stop_ms,
            "auth_failure_stop": self.auth_failure_stop,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SafetyLimits":
        return cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})


@dataclass(frozen=True)
class ThresholdDefinition:
    metric: str
    operator: str
    target: float
    label: str = ""

    def __post_init__(self) -> None:
        if self.metric not in THRESHOLD_METRICS:
            raise ValueError(f"Unknown threshold metric: {self.metric!r}")
        if self.operator not in THRESHOLD_OPERATORS:
            raise ValueError(f"Unknown threshold operator: {self.operator!r}")

    def evaluate(self, observed: float) -> bool:
        if self.operator == "<=":
            return observed <= self.target
        if self.operator == ">=":
            return observed >= self.target
        if self.operator == "<":
            return observed < self.target
        if self.operator == ">":
            return observed > self.target
        return observed == self.target

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "operator": self.operator,
            "target": self.target,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ThresholdDefinition":
        return cls(
            metric=value["metric"],
            operator=value["operator"],
            target=float(value["target"]),
            label=value.get("label", ""),
        )


@dataclass(frozen=True)
class LoadScenario:
    """A single-endpoint closed-model load scenario (Phase 5 MVP target scope 1).

    Weighted multi-target scenarios, saved-request/collection/suite
    conversion, and controller/service-wide selection are Phase 7 (section
    6.1 lists scopes 2-6 as later work).
    """

    environment: ExecutionEnvironmentSnapshot
    endpoint: Endpoint
    template: RequestTemplate
    stages: tuple[LoadStage, ...]
    limits: SafetyLimits = field(default_factory=SafetyLimits)
    thresholds: tuple[ThresholdDefinition, ...] = field(default_factory=tuple)
    workload_model: str = "closed_virtual_users"
    scenario_id: str = field(default_factory=new_scenario_id)
    name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "thresholds", tuple(self.thresholds))
        if self.workload_model not in WORKLOAD_MODELS:
            raise ValueError(f"Unknown workload model: {self.workload_model!r}")

    @property
    def total_duration_seconds(self) -> float:
        return sum(stage.duration_seconds for stage in self.stages)

    @property
    def peak_users(self) -> int:
        return max((stage.peak_users() for stage in self.stages), default=0)
