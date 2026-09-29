"""Immutable execution contracts shared by Load Testing Studio and the Data Runner.

See ``platform architecture guide: load-testing-and-data-runner.md``
section 4 for the design this module implements. Everything here is a plain,
JSON-serializable dataclass with no Qt or network dependency so it can be
constructed, validated, and unit tested without a running environment.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Final run outcomes. Never invent a new string ad hoc — extend this tuple.
RUN_OUTCOMES = ("PASS", "FAIL", "STOPPED", "ABORTED", "INCONCLUSIVE", "RUNNING")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_run_id(prefix: str) -> str:
    """Builds a short, sortable run identifier, e.g. ``DR-20260925-4f21ac``."""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class ExecutionEnvironmentSnapshot:
    """Immutable environment/authentication context captured at run start.

    Editing the live Environment, its authentication bindings, or its
    variables after a run has started must never change an in-flight run.
    Callers build one of these once, then pass it down; nothing in the
    execution layer re-reads live environment state.
    """

    environment_id: str
    environment_name: str
    base_urls: dict[str, str]
    verify_ssl: bool = True
    timeout: float = 30.0
    variables: dict[str, str] = field(default_factory=dict)
    custom_headers: dict[str, str] = field(default_factory=dict)
    access_token: str = ""
    api_key: str = ""
    auth_context: Any = None  # AuthenticationContext | None, kept untyped to avoid a Qt-adjacent import cycle.
    catalog_fingerprint: str = ""
    captured_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        # Deep-copy mutable inputs so a caller mutating its own dict afterward
        # cannot reach back into an already-started run.
        object.__setattr__(self, "base_urls", dict(self.base_urls))
        object.__setattr__(self, "variables", dict(self.variables))
        object.__setattr__(self, "custom_headers", dict(self.custom_headers))

    def base_url(self, service: str) -> str:
        return self.base_urls.get(service, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_id": self.environment_id,
            "environment_name": self.environment_name,
            "base_urls": dict(self.base_urls),
            "verify_ssl": self.verify_ssl,
            "timeout": self.timeout,
            "variables": dict(self.variables),
            "custom_headers": dict(self.custom_headers),
            "catalog_fingerprint": self.catalog_fingerprint,
            "captured_at": self.captured_at,
        }


@dataclass(frozen=True)
class RequestTemplate:
    """A single reusable, catalog-aware request definition.

    ``values`` follows the same ``"{source}:{name}" -> value`` convention as
    :mod:`api_tester.client` (``path:Id``, ``query:Filter``, ``header:X-Foo``,
    ``form:File``) so a template can be handed straight to
    ``prepare_endpoint_request``/``execute_endpoint`` without translation.
    """

    endpoint_id: str
    service: str
    method: str
    path: str
    values: dict[str, str] = field(default_factory=dict)
    payload: Any = None
    expected_status: str = "200-299"
    auth_mode: str = "inherit"
    custom_headers: dict[str, str] = field(default_factory=dict)
    weight: float = 1.0
    think_time_ms: int = 0
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", dict(self.values))
        object.__setattr__(self, "custom_headers", dict(self.custom_headers))

    def with_values(self, values: dict[str, str], payload: Any = None) -> "RequestTemplate":
        """Returns a copy with resolved per-row/per-iteration values applied.

        ``payload`` replaces the template payload only when explicitly given
        (``None`` legitimately means "no JSON body" for GET/HEAD templates, so
        the caller must pass ``_MISSING``-style intent by only supplying a
        value when it actually has one — see ``data_runner.mapping``).
        """
        merged = dict(self.values)
        merged.update(values)
        return RequestTemplate(
            endpoint_id=self.endpoint_id,
            service=self.service,
            method=self.method,
            path=self.path,
            values=merged,
            payload=payload if payload is not None else copy.deepcopy(self.payload),
            expected_status=self.expected_status,
            auth_mode=self.auth_mode,
            custom_headers=dict(self.custom_headers),
            weight=self.weight,
            think_time_ms=self.think_time_ms,
            label=self.label,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "service": self.service,
            "method": self.method,
            "path": self.path,
            "values": dict(self.values),
            "payload": copy.deepcopy(self.payload),
            "expected_status": self.expected_status,
            "auth_mode": self.auth_mode,
            "custom_headers": dict(self.custom_headers),
            "weight": self.weight,
            "think_time_ms": self.think_time_ms,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RequestTemplate":
        return cls(
            endpoint_id=value["endpoint_id"],
            service=value["service"],
            method=value["method"],
            path=value["path"],
            values=dict(value.get("values", {})),
            payload=value.get("payload"),
            expected_status=value.get("expected_status", "200-299"),
            auth_mode=value.get("auth_mode", "inherit"),
            custom_headers=dict(value.get("custom_headers", {})),
            weight=float(value.get("weight", 1.0)),
            think_time_ms=int(value.get("think_time_ms", 0)),
            label=value.get("label", ""),
        )


@dataclass(frozen=True)
class RequestSample:
    """One lightweight, bounded-size record of a single executed request.

    This is deliberately *not* :class:`api_tester.client.ApiResult` — it must
    stay small enough to retain tens of thousands of instances in memory.
    Full response bodies/headers are never stored here; see
    ``execution.persistence`` for the separate bounded failure-sample store.
    """

    run_id: str
    endpoint_id: str
    service: str
    method: str
    outcome: str  # "passed" | "failed" | "error" | "cancelled" | "skipped"
    started_at: str
    offset_ms: float
    queue_delay_ms: float
    http_ms: float
    status_code: int | None = None
    auth_ms: float = 0.0
    dns_ms: float | None = None
    tcp_ms: float | None = None
    tls_ms: float | None = None
    worker_id: int = 0
    iteration: int = 0
    row_number: int | None = None
    correlation_key: str = ""
    error_category: str | None = None
    error_message: str = ""
    request_bytes: int = 0
    response_bytes: int = 0
    assertions_passed: int = 0
    assertions_failed: int = 0
    retry_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "endpoint_id": self.endpoint_id,
            "service": self.service,
            "method": self.method,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "offset_ms": self.offset_ms,
            "queue_delay_ms": self.queue_delay_ms,
            "http_ms": self.http_ms,
            "status_code": self.status_code,
            "auth_ms": self.auth_ms,
            "dns_ms": self.dns_ms,
            "tcp_ms": self.tcp_ms,
            "tls_ms": self.tls_ms,
            "worker_id": self.worker_id,
            "iteration": self.iteration,
            "row_number": self.row_number,
            "correlation_key": self.correlation_key,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "request_bytes": self.request_bytes,
            "response_bytes": self.response_bytes,
            "assertions_passed": self.assertions_passed,
            "assertions_failed": self.assertions_failed,
            "retry_count": self.retry_count,
        }


#: Canonical event kinds. UI/coordinator code should treat unknown kinds as
#: forward-compatible no-ops instead of raising.
EVENT_KINDS = (
    "run_started",
    "phase_changed",
    "worker_started",
    "request_started",
    "request_completed",
    "request_failed",
    "row_started",
    "row_completed",
    "metric_snapshot",
    "warning",
    "run_stopping",
    "run_finished",
)


@dataclass(frozen=True)
class ExecutionEvent:
    kind: str
    run_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"Unknown execution event kind: {self.kind!r}")
        object.__setattr__(self, "payload", dict(self.payload))
