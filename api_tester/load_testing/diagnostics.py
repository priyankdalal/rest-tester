"""Rule-based diagnostics for a finished load run.

:func:`diagnose` turns a :class:`~api_tester.load_testing.report_data.LoadReportData`
into ranked :class:`Finding` objects: what went wrong, the evidence from this
run, the likely causes, and fixes. The rules are deterministic and cite the
numbers they used, so a reader can check every claim against the charts. The
causes are hypotheses; the report asks the reader to confirm them with
server-side telemetry for the same time window.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from api_tester.load_testing.report_data import LoadReportData

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "INFO")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}

#: Threshold metric → (display name, unit kind).
THRESHOLD_LABELS = {
    "p50_ms": ("P50 latency", "ms"),
    "p95_ms": ("P95 latency", "ms"),
    "p99_ms": ("P99 latency", "ms"),
    "mean_ms": ("Mean latency", "ms"),
    "max_ms": ("Max latency", "ms"),
    "error_rate": ("Error rate", "ratio"),
    "throughput_per_second": ("Throughput", "rps"),
    "auth_failures": ("Auth failures", "count"),
    "http_500_count": ("HTTP 500 responses", "count"),
}

_SLOW_QUEUE_DELAY_MS = 100.0
_HTTP_CODE = re.compile(r"\bHTTP (\d{3})\b")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str
    title: str
    evidence: tuple[str, ...]
    causes: tuple[str, ...]
    fixes: tuple[str, ...]
    confidence: str = "Medium"
    #: Values the charts can annotate (e.g. ``knee_users``).
    annotations: dict[str, Any] = field(default_factory=dict)
    #: ``F1``, ``F2``… assigned after ranking.
    ref: str = ""

    @property
    def is_problem(self) -> bool:
        return self.severity != "INFO"


# ---------------------------------------------------------------------- formatting


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 10_000:
        return f"{value / 1000:,.1f} s"
    return f"{value:,.0f} ms"


def fmt_pct(ratio: float | None, digits: int = 1) -> str:
    return "—" if ratio is None else f"{ratio * 100:.{digits}f}%"


def fmt_seconds(value: float | None) -> str:
    if value is None:
        return "—"
    value = max(0.0, value)
    if value < 120:
        return f"{value:.0f} s"
    minutes, seconds = divmod(int(round(value)), 60)
    return f"{minutes}m {seconds:02d}s"


def format_threshold_value(metric: str, value: float | None) -> str:
    if value is None:
        return "—"
    kind = THRESHOLD_LABELS.get(metric, (metric, "count"))[1]
    if kind == "ms":
        return fmt_ms(value)
    if kind == "ratio":
        return fmt_pct(value)
    if kind == "rps":
        return f"{value:,.1f} req/s"
    return f"{value:,.0f}"


def threshold_margin(metric: str, operator: str, target: float, observed: float) -> str:
    """How far the observed value is from the target, signed so ``+`` means *worse*."""
    kind = THRESHOLD_LABELS.get(metric, (metric, "count"))[1]
    worse_when_higher = operator in ("<=", "<")
    delta = observed - target if worse_when_higher else target - observed
    if operator == "==":
        delta = abs(observed - target)
    if kind == "ratio":
        return f"{delta * 100:+.1f} pt"
    if target:
        return f"{delta / abs(target) * 100:+.0f}%"
    return f"{delta:+,.0f}"


def threshold_rows(data: LoadReportData) -> list[tuple[str, str, str, str, bool]]:
    """``(name, target, observed, margin, passed)`` for each evaluated threshold."""
    rows = []
    for result in data.thresholds:
        definition = result.definition
        metric = definition.metric
        name = definition.label or THRESHOLD_LABELS.get(metric, (metric, ""))[0]
        target = f"{definition.operator} {format_threshold_value(metric, definition.target)}"
        observed = format_threshold_value(metric, result.observed_value)
        margin = threshold_margin(metric, definition.operator, float(definition.target), float(result.observed_value))
        rows.append((name, target, observed, margin, bool(result.passed)))
    return rows


# ---------------------------------------------------------------------- shared facts


def _active_intervals(data: LoadReportData) -> list[dict[str, Any]]:
    return [
        snapshot
        for snapshot in data.snapshots
        if int(snapshot.get("interval_completed", 0) or 0) > 0 and int(snapshot.get("active_users", 0) or 0) > 0
    ]


def _failures(data: LoadReportData) -> int:
    final = data.final
    return int(final.get("failed", 0) or 0) + int(final.get("errored", 0) or 0)


def _status_counts(data: LoadReportData) -> dict[int, int]:
    """HTTP status → count. Samples are exact; otherwise grouped error messages are used."""
    if data.samples_available:
        return {item.status_code: item.count for item in data.samples.status_counts if item.status_code}
    counts: dict[int, int] = defaultdict(int)
    for entry in data.errors:
        match = _HTTP_CODE.search(str(entry.get("message", "")))
        if match:
            counts[int(match.group(1))] += int(entry.get("count", 0) or 0)
    return dict(counts)


def _category_count(data: LoadReportData, *categories: str) -> int:
    if data.samples_available:
        return sum(item.count for item in data.samples.categories if item.category in categories)
    return sum(int(entry.get("count", 0) or 0) for entry in data.errors if entry.get("category") in categories)


def _first_seen(data: LoadReportData, *, categories: tuple[str, ...] = (), codes: tuple[int, ...] = ()) -> float | None:
    """Seconds after start of the first matching failure."""
    candidates: list[float] = []
    if data.samples_available:
        candidates += [
            item.first_seconds
            for item in data.samples.categories
            if item.category in categories and item.first_seconds is not None
        ]
        candidates += [
            item.first_seconds
            for item in data.samples.status_counts
            if item.status_code in codes and item.first_seconds is not None
        ]
    if not candidates:
        for entry in data.errors:
            match = _HTTP_CODE.search(str(entry.get("message", "")))
            if entry.get("category") in categories or (match and int(match.group(1)) in codes):
                offset = data.error_offset_seconds(entry.get("first_seen"))
                if offset is not None:
                    candidates.append(offset)
    return min(candidates) if candidates else None


def _onset_text(data: LoadReportData, offset: float | None) -> str:
    if offset is None:
        return ""
    return f"first seen {fmt_seconds(offset)} into the run (≈ {data.users_at(offset)} virtual users)"


def _share(count: int, total: int) -> float:
    return count / total if total else 0.0


def _codes_text(counts: dict[int, int], predicate) -> str:
    items = sorted(((code, count) for code, count in counts.items() if predicate(code)), key=lambda item: -item[1])
    return ", ".join(f"HTTP {code}: {count:,}" for code, count in items)


# ---------------------------------------------------------------------- rules


def _rule_no_requests(data: LoadReportData) -> Finding | None:
    if data.total_requests > 0:
        return None
    return Finding(
        "no_requests",
        "CRITICAL",
        "No requests completed",
        ("The run finished without a single completed request.", f"Stop reason: {data.stop_reason}"),
        (
            "The plan was blocked by a safety check or stopped before the first request returned.",
            "The base URL or network route to the environment is unreachable.",
        ),
        ("Send the same request once from API Explorer to confirm it works.", "Review the Pre-run summary for blocking issues."),
        "High",
    )


def _rule_failing_from_start(data: LoadReportData) -> Finding | None:
    total = data.total_requests
    failures = _failures(data)
    if total == 0 or _share(failures, total) < 0.5:
        return None
    intervals = _active_intervals(data)
    early = intervals[: max(1, len(intervals) // 10)]
    if early and sum(int(s.get("interval_errors", 0) or 0) for s in early) < 0.5 * sum(
        int(s.get("interval_completed", 0) or 0) for s in early
    ):
        return None
    counts = _status_counts(data)
    auth = counts.get(401, 0) + counts.get(403, 0) + _category_count(data, "authentication")
    client = sum(count for code, count in counts.items() if 400 <= code < 500 and code not in (401, 403, 429))
    connection = _category_count(data, "dns", "connection", "tls")
    evidence = [
        f"{failures:,} of {total:,} requests failed ({fmt_pct(_share(failures, total))}).",
        "Failures began with the first requests, before load could matter.",
    ]
    if counts:
        evidence.append("Status mix: " + _codes_text(counts, lambda code: code >= 400) + ".")
    causes = []
    if auth:
        causes.append("Missing, expired or under-privileged access token or x-api-key for this environment.")
    if client:
        causes.append("Invalid request: path/query values or payload do not match what the API expects, or the record does not exist.")
    if connection:
        causes.append("The base URL is wrong or the environment is not reachable from this machine.")
    if data.definition.get("expected_status"):
        causes.append(f"The expected status ({data.definition.get('expected_status')}) may not match what the endpoint returns.")
    if not causes:
        causes.append("The endpoint is failing independently of load (deployment, configuration or data issue).")
    return Finding(
        "failing_from_start",
        "CRITICAL",
        "Requests fail even at minimal load",
        tuple(evidence),
        tuple(causes),
        (
            "Send the request once from API Explorer with the same environment and fix it there first.",
            "Re-check the environment's base URL, token and x-api-key; seed any records the request depends on.",
            "The load figures in this report describe failures, not capacity; re-run once requests pass.",
        ),
        "High",
    )


def _rule_saturation(data: LoadReportData) -> Finding | None:
    intervals = _active_intervals(data)
    if len(intervals) < 6:
        return None
    by_users: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for snapshot in intervals:
        by_users[int(snapshot.get("active_users", 0))].append(snapshot)
    levels = []
    for users in sorted(by_users):
        items = by_users[users]
        throughput = sum(float(s.get("interval_throughput", 0.0) or 0.0) for s in items) / len(items)
        p95 = sum(float(s.get("interval_p95_ms", 0.0) or 0.0) for s in items) / len(items)
        levels.append((users, throughput, p95))
    if len(levels) < 3:
        return None
    peak_throughput = max(level[1] for level in levels)
    if peak_throughput <= 0:
        return None
    knee = next(level for level in levels if level[1] >= 0.9 * peak_throughput)
    top = levels[-1]
    if top[0] < knee[0] * 1.5 or top[1] > knee[1] * 1.2 or knee[2] <= 0 or top[2] < knee[2] * 1.5:
        return None
    growth = top[2] / knee[2]
    return Finding(
        "saturation",
        "CRITICAL" if growth >= 3 else "HIGH",
        f"Throughput saturates at ~{peak_throughput:,.0f} req/s",
        (
            f"Throughput levels off at about {peak_throughput:,.0f} req/s from ≈ {knee[0]} virtual users, "
            f"while load rose to {top[0]} users.",
            f"P95 latency grew from {fmt_ms(knee[2])} at {knee[0]} users to {fmt_ms(top[2])} at {top[0]} users ({growth:.1f}×).",
            f"Throughput per user fell from {knee[1] / max(knee[0], 1):.1f} to {top[1] / max(top[0], 1):.1f} req/s: "
            "extra users only queue (Little's law).",
        ),
        (
            "Server-side concurrency limit: thread-pool starvation or synchronous I/O in the request path.",
            "Database bottleneck: connection-pool exhaustion, missing index, or throttled Cosmos DB RU/s.",
            "CPU or memory ceiling of the App Service plan instance(s).",
        ),
        (
            "Correlate this window in Application Insights: CPU, thread count, dependency duration.",
            "Make data access fully async; raise the pool size or RU allocation; cache read-mostly lookups.",
            f"Scale out and re-run this scenario to confirm the knee moves; plan capacity at ≤ {0.8 * peak_throughput:,.0f} req/s.",
        ),
        "High" if len(levels) >= 5 and growth >= 2 else "Medium",
        {"knee_users": knee[0], "plateau_rps": peak_throughput},
    )


def _rule_server_errors(data: LoadReportData) -> Finding | None:
    counts = _status_counts(data)
    count = sum(value for code, value in counts.items() if 500 <= code < 600) or _category_count(data, "http_5xx")
    if not count:
        return None
    total = data.total_requests
    share = _share(count, total)
    onset = _first_seen(data, categories=("http_5xx",), codes=tuple(code for code in counts if 500 <= code < 600))
    evidence = [f"{count:,} server errors ({fmt_pct(share)} of requests)."]
    breakdown = _codes_text(counts, lambda code: 500 <= code < 600)
    if breakdown:
        evidence[0] = f"{count:,} server errors ({fmt_pct(share)} of requests) — {breakdown}."
    if onset is not None:
        evidence.append(f"Server errors were {_onset_text(data, onset)}.")
    peak_error = data.stats.error_rate.maximum
    if peak_error:
        evidence.append(f"Worst 1-second interval error rate: {fmt_pct(peak_error)}.")
    causes = []
    if counts.get(503):
        causes.append("503 from APIM or App Service when the backend is at capacity, recycling, or failing health probes.")
    if counts.get(502) or counts.get(504):
        causes.append("502/504: the gateway could not get a timely response from the backend (APIM backend timeout).")
    if counts.get(500) or not causes:
        causes.append("500: unhandled exceptions under concurrency — downstream call timeouts, DbContext or connection exhaustion, race conditions.")
    return Finding(
        "server_errors",
        "HIGH" if share >= 0.01 else "MEDIUM",
        "HTTP 5xx errors under load",
        tuple(evidence),
        tuple(causes),
        (
            "Query failed requests in Application Insights for this window and group them by exception type.",
            "Add timeouts, retries with jitter and circuit breakers on downstream calls (e.g. calls to TrialAuth).",
            "Return 503 with Retry-After instead of 500 when overloaded; check APIM logs to see which layer failed.",
        ),
        "High" if data.samples_available else "Medium",
        {"onset_seconds": onset},
    )


def _rule_rate_limited(data: LoadReportData) -> Finding | None:
    counts = _status_counts(data)
    count = counts.get(429, 0)
    if not count:
        return None
    share = _share(count, data.total_requests)
    onset = _first_seen(data, codes=(429,))
    evidence = [f"{count:,} requests were rejected with HTTP 429 Too Many Requests ({fmt_pct(share)})."]
    if onset is not None:
        evidence.append(f"Rate limiting was {_onset_text(data, onset)}.")
    evidence.append("Results above this request rate describe the gateway policy, not the service.")
    return Finding(
        "rate_limited",
        "HIGH" if share >= 0.01 else "MEDIUM",
        "Requests are being rate limited (HTTP 429)",
        tuple(evidence),
        (
            "An APIM rate-limit or quota policy applies to the subscription key used for this run.",
            "The x-api-key is shared with other clients, so the quota is consumed faster than this run alone suggests.",
        ),
        (
            "Use a dedicated load-test subscription with raised limits, or test the backend directly.",
            "Honour Retry-After in clients, and keep the peak below the documented quota.",
        ),
        "High",
        {"onset_seconds": onset},
    )


def _rule_timeouts(data: LoadReportData) -> Finding | None:
    count = _category_count(data, "timeout")
    if not count:
        return None
    share = _share(count, data.total_requests)
    timeout = (data.definition.get("limits") or {}).get("request_timeout_seconds")
    onset = _first_seen(data, categories=("timeout",))
    evidence = [f"{count:,} requests timed out ({fmt_pct(share)})" + (f" after {float(timeout):g} s." if timeout else ".")]
    if onset is not None:
        evidence.append(f"Timeouts were {_onset_text(data, onset)}.")
    p99 = data.final.get("p99_ms")
    if p99:
        evidence.append(f"Completed requests had a P99 of {fmt_ms(float(p99))}.")
    return Finding(
        "timeouts",
        "HIGH" if share >= 0.01 else "MEDIUM",
        "Requests time out",
        tuple(evidence),
        (
            "Requests stall on the server: blocked threads, long-running queries or lock waits.",
            "A downstream dependency stops responding under load and has no timeout of its own.",
            "The client timeout is shorter than this endpoint's normal worst-case latency.",
        ),
        (
            "Find the slowest dependency calls for this window in Application Insights.",
            "Add server-side timeouts and cancellation tokens so work stops when the caller gives up.",
            "Only raise the request timeout if the endpoint's SLA allows that latency.",
        ),
        "Medium",
        {"onset_seconds": onset},
    )


def _rule_connection(data: LoadReportData) -> Finding | None:
    count = _category_count(data, "dns", "connection", "tls")
    if not count:
        return None
    onset = _first_seen(data, categories=("dns", "connection", "tls"))
    evidence = [f"{count:,} requests failed before an HTTP response ({fmt_pct(_share(count, data.total_requests))})."]
    if onset is not None:
        evidence.append(f"Connection failures were {_onset_text(data, onset)}.")
    return Finding(
        "connection",
        "HIGH",
        "Connection, DNS or TLS failures",
        tuple(evidence),
        (
            "The server or gateway closed or refused connections under load (connection limits, instance restarts).",
            "The client machine ran out of ephemeral ports or network capacity.",
            "DNS or TLS problems reaching this environment (VPN, proxy, certificate).",
        ),
        (
            "Check App Service / APIM health and restarts for this window.",
            "Re-run from a machine closer to the environment, or with fewer virtual users per machine.",
        ),
        "Medium",
        {"onset_seconds": onset},
    )


def _rule_authentication(data: LoadReportData) -> Finding | None:
    counts = _status_counts(data)
    count = counts.get(401, 0) + counts.get(403, 0) + _category_count(data, "authentication")
    if not count:
        return None
    onset = _first_seen(data, categories=("authentication",), codes=(401, 403))
    evidence = [f"{count:,} requests were rejected as unauthenticated or forbidden."]
    expired_mid_run = onset is not None and onset >= 60 and int(data.final.get("passed", 0) or 0) > 0
    if onset is not None:
        evidence.append(f"Authentication failures were {_onset_text(data, onset)}.")
    causes = (
        ("The access token expired during the run and was not renewed.",)
        if expired_mid_run
        else ("The access token or x-api-key is missing, invalid, or lacks the role/scope this endpoint requires.",)
    ) + ("TrialAuth itself throttled or failed under the extra authorization load.",)
    return Finding(
        "authentication",
        "HIGH",
        "Authentication / authorization failures",
        tuple(evidence),
        causes,
        (
            "Sign in again (or refresh the managed token) right before long runs.",
            "Verify the user's TrialAuth role and feature permissions for this endpoint.",
        ),
        "High" if expired_mid_run else "Medium",
        {"onset_seconds": onset},
    )


def _rule_client_errors(data: LoadReportData) -> Finding | None:
    counts = _status_counts(data)
    count = sum(value for code, value in counts.items() if 400 <= code < 500 and code not in (401, 403, 429))
    if not count:
        return None
    breakdown = _codes_text(counts, lambda code: 400 <= code < 500 and code not in (401, 403, 429))
    causes = ["Request values or payload are invalid for some records (validation failures)."]
    if counts.get(409):
        causes.append("409 Conflict: many virtual users replay the same payload, so unique constraints collide.")
    if counts.get(404):
        causes.append("404 Not Found: the path id does not exist in this environment, or records were deleted during the run.")
    return Finding(
        "client_errors",
        "MEDIUM",
        "Client errors (4xx) during the run",
        (f"{count:,} requests were rejected by the API — {breakdown}.",),
        tuple(causes),
        (
            "Generate unique values per request (Data Runner) for write endpoints instead of one fixed payload.",
            "Seed the referenced records before the run and keep them stable while it runs.",
        ),
        "Medium",
    )


def _rule_latency_thresholds(data: LoadReportData, saturated: bool) -> list[Finding]:
    findings = []
    for result in data.thresholds:
        definition = result.definition
        metric = definition.metric
        if result.passed:
            continue
        if metric == "throughput_per_second":
            findings.append(
                Finding(
                    "throughput_threshold",
                    "MEDIUM",
                    "Throughput below target",
                    (
                        f"Target {definition.operator} {format_threshold_value(metric, definition.target)}, "
                        f"observed {format_threshold_value(metric, result.observed_value)}.",
                    ),
                    ("The endpoint saturated before the target rate." if saturated else "Too few virtual users or too much think time to reach the target rate.",),
                    ("Raise peak users or reduce think time; if latency also climbs, see the saturation finding.",),
                    "Medium",
                )
            )
            continue
        if metric not in ("p50_ms", "p95_ms", "p99_ms", "mean_ms", "max_ms"):
            continue
        interval_key = {"max_ms": "interval_p99_ms"}.get(metric, f"interval_{metric}")
        onset = next(
            (
                float(s.get("elapsed_seconds", 0.0))
                for s in _active_intervals(data)
                if float(s.get(interval_key, 0.0) or 0.0) > float(definition.target)
            ),
            None,
        )
        name = THRESHOLD_LABELS[metric][0]
        evidence = [
            f"{name} target {definition.operator} {fmt_ms(float(definition.target))}; observed {fmt_ms(float(result.observed_value))} "
            f"({threshold_margin(metric, definition.operator, float(definition.target), float(result.observed_value))})."
        ]
        if onset is not None:
            evidence.append(f"Intervals first exceeded the target {fmt_seconds(onset)} into the run (≈ {data.users_at(onset)} users).")
        causes = ["Latency climbs once the endpoint stops scaling (see the saturation finding)."] if saturated else [
            "Slow work in the request path: unindexed queries, large payloads, or chatty downstream calls."
        ]
        findings.append(
            Finding(
                f"threshold_{metric}",
                "HIGH",
                f"{name} threshold breached",
                tuple(evidence),
                tuple(causes),
                (
                    "Profile the endpoint at the onset load: query plans, payload size, dependency calls.",
                    "Add caching or pagination for large result sets; then re-run to confirm.",
                ),
                "High",
                {"onset_seconds": onset},
            )
        )
    return findings


def _rule_long_tail(data: LoadReportData) -> Finding | None:
    p50 = float(data.final.get("p50_ms", 0.0) or 0.0)
    p99 = float(data.final.get("p99_ms", 0.0) or 0.0)
    if p50 <= 0 or p99 < 500 or p99 < 5 * p50:
        return None
    max_ms = data.final.get("max_ms")
    evidence = [f"P99 {fmt_ms(p99)} is {p99 / p50:.1f}× the median P50 {fmt_ms(p50)}."]
    if max_ms:
        evidence.append(f"Slowest request: {fmt_ms(float(max_ms))}.")
    if data.samples_available and data.samples.latency_buckets:
        slow = sum(data.samples.latency_buckets[-2:])
        evidence.append(f"{fmt_pct(_share(slow, sum(data.samples.latency_buckets)))} of requests took 2 s or more.")
    return Finding(
        "long_tail",
        "MEDIUM",
        f"Long latency tail — P99 is {p99 / p50:.1f}× P50",
        tuple(evidence),
        (
            "Queuing, lock contention or garbage-collection pauses under memory pressure.",
            "Cold caches or uneven data (some ids return far larger result sets).",
        ),
        (
            "Watch dotnet-counters (GC %, thread-pool queue length) during a steady run at this load.",
            "Review hot-path allocations and locks; warm caches before the steady stage.",
        ),
        "Medium",
    )


def _rule_load_generator(data: LoadReportData) -> Finding | None:
    if not data.samples_available:
        return None
    delay = data.samples.queue_delay_ms
    overload = _category_count(data, "scheduler_overload")
    if (delay.average or 0.0) > _SLOW_QUEUE_DELAY_MS or overload:
        evidence = [f"Average scheduling delay {fmt_ms(delay.average)} (max {fmt_ms(delay.maximum)})."]
        if overload:
            evidence.append(f"{overload:,} requests were dropped as scheduler_overload.")
        return Finding(
            "load_generator",
            "HIGH",
            "Load generator could not keep its schedule — results inconclusive",
            tuple(evidence),
            (
                "This machine ran out of CPU or network capacity for the number of virtual users.",
                "Latency figures include client-side waiting, so the server may be faster than reported.",
            ),
            ("Re-run with fewer virtual users per machine or on a larger machine.",),
            "High",
        )
    return Finding(
        "load_generator_ok",
        "INFO",
        "Load generator kept its schedule",
        (f"Average scheduling delay {fmt_ms(delay.average)} (max {fmt_ms(delay.maximum)}); no scheduler overload.",),
        ("The client was not the bottleneck, so these results reflect the server.",),
        ("No action needed.",),
        "High",
    )


def _rule_amplification(data: LoadReportData) -> Finding | None:
    messages = [
        str(warning.get("message", ""))
        for warning in data.warnings
        if "amplif" in str(warning.get("message", "")).lower() or "downstream" in str(warning.get("message", "")).lower()
    ]
    if not messages:
        return None
    return Finding(
        "amplification",
        "INFO",
        "Downstream amplification risk",
        tuple(messages[:2]),
        ("This endpoint calls other TrialWyze services (for example TrialAuth for authorization), so load multiplies.",),
        ("Watch dependent services during the run and coordinate with their owners before raising load.",),
        "Medium",
    )


def _rule_healthy(data: LoadReportData) -> Finding:
    final = data.final
    peak = data.stats.throughput.maximum
    return Finding(
        "healthy",
        "INFO",
        "No performance problems detected",
        (
            f"{data.total_requests:,} requests with error rate {fmt_pct(float(final.get('error_rate', 0.0) or 0.0))}.",
            f"P95 {fmt_ms(final.get('p95_ms'))}, P99 {fmt_ms(final.get('p99_ms'))}"
            + (f", peak {peak:,.1f} req/s." if peak else "."),
        ),
        ("The endpoint handled this load profile within its limits.",),
        (
            "Keep this run as a baseline and compare after each release.",
            "Raise the peak load in a follow-up run to find where it stops scaling.",
        ),
        "Medium",
    )


# ---------------------------------------------------------------------- public API


def _rule_unclassified_errors(data: LoadReportData) -> Finding | None:
    """Fallback when requests failed but no error detail exists (unsaved runs keep counts only)."""
    failures = _failures(data)
    if not failures:
        return None
    share = _share(failures, data.total_requests)
    evidence = [f"{failures:,} failed or errored requests ({fmt_pct(share)} of requests)."]
    onset = next(
        (
            float(s.get("elapsed_seconds", 0.0) or 0.0) - float(s.get("interval_seconds", 1.0) or 1.0)
            for s in _active_intervals(data)
            if int(s.get("interval_errors", 0) or 0)
        ),
        None,
    )
    if onset is not None:
        evidence.append(f"Errors were {_onset_text(data, max(0.0, onset))}.")
    if data.stats.error_rate.maximum:
        evidence.append(f"Worst 1-second interval error rate: {fmt_pct(data.stats.error_rate.maximum)}.")
    return Finding(
        "errors",
        "HIGH" if share >= 0.01 else "MEDIUM",
        "Errors under load",
        tuple(evidence),
        (
            "Status codes and error messages were not recorded for this run, so the cause cannot be classified.",
            "Errors that start only at higher load usually mean the service or a dependency is at capacity.",
        ),
        (
            "Re-run with “Persist this run” enabled to capture status codes, error signatures and timings.",
            "Check Application Insights failures for the run window.",
        ),
        "Low",
        {"onset_seconds": onset},
    )


_CLASSIFIED_ERROR_RULES = {
    "failing_from_start", "server_errors", "rate_limited", "timeouts", "connection", "authentication", "client_errors",
}


def diagnose(data: LoadReportData) -> list[Finding]:
    """Ranked findings (most severe first), numbered ``F1``, ``F2``…"""
    empty = _rule_no_requests(data)
    if empty is not None:
        return [replace(empty, ref="F1")]
    findings: list[Finding] = []
    saturation = _rule_saturation(data)
    for rule in (
        lambda: _rule_failing_from_start(data),
        lambda: saturation,
        lambda: _rule_server_errors(data),
        lambda: _rule_rate_limited(data),
        lambda: _rule_timeouts(data),
        lambda: _rule_connection(data),
        lambda: _rule_authentication(data),
        lambda: _rule_client_errors(data),
        lambda: _rule_long_tail(data),
        lambda: _rule_load_generator(data),
        lambda: _rule_amplification(data),
    ):
        finding = rule()
        if finding is not None:
            findings.append(finding)
    if not any(finding.rule in _CLASSIFIED_ERROR_RULES for finding in findings):
        unclassified = _rule_unclassified_errors(data)
        if unclassified is not None:
            findings.append(unclassified)
    findings.extend(_rule_latency_thresholds(data, saturation is not None))
    if not any(finding.is_problem for finding in findings):
        findings.append(_rule_healthy(data))
    findings.sort(key=lambda finding: _SEVERITY_RANK.get(finding.severity, len(SEVERITIES)))
    return [replace(finding, ref=f"F{index}") for index, finding in enumerate(findings, start=1)]


def executive_summary(data: LoadReportData, findings: list[Finding]) -> list[tuple[str, str]]:
    """Up to five ``(tone, sentence)`` bullets; tone is ``pass``, ``fail``, ``warn`` or ``info``."""
    final = data.final
    bullets: list[tuple[str, str]] = []
    tone = {"PASS": "pass", "FAIL": "fail", "ABORTED": "fail"}.get(data.outcome, "warn")
    duration = fmt_seconds(data.elapsed_seconds or data.duration_seconds)
    peak = data.stats.throughput.maximum
    headline = (
        f"{data.endpoint_text}"
        + (f" ({data.service})" if data.service else "")
        + f" completed {data.total_requests:,} requests in {duration} with up to {data.peak_users} virtual users"
    )
    if data.total_requests:
        headline += (
            f": peak {peak or 0:,.1f} req/s, P95 {fmt_ms(final.get('p95_ms'))}, "
            f"error rate {fmt_pct(float(final.get('error_rate', 0.0) or 0.0))}."
        )
    else:
        headline += "."
    bullets.append((tone, headline))
    rows = threshold_rows(data)
    if rows:
        failed = [row[0] for row in rows if not row[4]]
        if failed:
            bullets.append(("fail", f"{len(rows) - len(failed)} of {len(rows)} thresholds passed; failed: {', '.join(failed)}."))
        else:
            bullets.append(("pass", f"All {len(rows)} thresholds passed."))
    for finding in [item for item in findings if item.is_problem][:3]:
        bullets.append(
            ("fail" if finding.severity in ("CRITICAL", "HIGH") else "warn", f"{finding.title}. {finding.evidence[0]}")
        )
    for finding in findings:
        if len(bullets) >= 5:
            break
        if not finding.is_problem:
            bullets.append(("info" if finding.rule != "healthy" else "pass", f"{finding.title}. {finding.evidence[0]}"))
    return bullets[:5]
