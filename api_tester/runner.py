"""Executes test suites and reports per-case results.

The runner is UI-free so it can be exercised by tests; the PyQt layer wraps it
in a worker thread and renders the emitted results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable

from .baseline import BaselineDifference, compare_json_baseline, serialize_json_text
from .catalog import Catalog, Endpoint
from .client import ApiResult, execute_endpoint, status_matches
from .suite import (
    Assertion,
    AssertionOutcome,
    TestCase,
    TestSuite,
    apply_variables,
    extract_capture,
    evaluate_assertion,
)
from .request_auth import AuthenticationContext
from .authentication import AuthError
from .workspace_store import WorkspaceStore, as_store


@dataclass
class CaseResult:
    case_id: str
    case_name: str
    endpoint_id: str
    method: str
    path: str
    service: str
    passed: bool
    skipped: bool = False
    status_code: int = 0
    expected_status: str = ""
    elapsed_ms: int = 0
    url: str = ""
    error: str = ""
    assertions: list[AssertionOutcome] = field(default_factory=list)
    captured: dict[str, str] = field(default_factory=dict)
    response_body: str = ""
    response_headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""
    content_type: str = ""
    content_disposition: str = ""
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: str = ""
    timings: dict[str, float | str] = field(default_factory=dict)
    #: Wall-clock offsets of this case within the run, in milliseconds. They are
    #: measured rather than derived, because ``elapsed_ms`` covers only the HTTP
    #: call - variable substitution, assertions and captures happen around it.
    start_offset_ms: float = 0.0
    wall_ms: float = 0.0
    request_offset_ms: float = 0.0
    authentication_failed: bool = False
    phase: str = "normal"
    skip_reason: str = ""
    baseline_differences: list[BaselineDifference] = field(default_factory=list)
    baseline_expected: str = ""
    baseline_actual: str = ""

    @property
    def baseline_expected_text(self) -> str:
        return self.baseline_expected

    @baseline_expected_text.setter
    def baseline_expected_text(self, value: str) -> None:
        self.baseline_expected = value

    @property
    def baseline_actual_text(self) -> str:
        return self.baseline_actual

    @baseline_actual_text.setter
    def baseline_actual_text(self, value: str) -> None:
        self.baseline_actual = value

    @property
    def outcome(self) -> str:
        if self.skipped:
            return "SKIPPED"
        if self.error:
            return "ERROR"
        return "PASS" if self.passed else "FAIL"

    @property
    def failed_assertions(self) -> list[AssertionOutcome]:
        return [item for item in self.assertions if not item.passed]


@dataclass
class SuiteResult:
    suite_name: str
    started_at: str
    finished_at: str = ""
    results: list[CaseResult] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def executed(self) -> list[CaseResult]:
        return [item for item in self.results if not item.skipped]

    @property
    def passed(self) -> int:
        return sum(1 for item in self.executed if item.passed and not item.error)

    @property
    def failed(self) -> int:
        return sum(1 for item in self.executed if not item.passed and not item.error)

    @property
    def errored(self) -> int:
        return sum(1 for item in self.executed if item.error)

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.results if item.skipped)

    @property
    def duration_ms(self) -> int:
        return sum(item.elapsed_ms for item in self.results)

    @property
    def wall_duration_ms(self) -> float:
        """Wall-clock span of the run, including between-case overhead.

        ``duration_ms`` only adds up the HTTP calls, so it under-reports how long
        the run actually took. Returns ``0.0`` for results that predate offset
        recording, which callers treat as "offsets unavailable".
        """
        return max((item.start_offset_ms + item.wall_ms for item in self.results), default=0.0)

    @property
    def pass_rate(self) -> float:
        executed = len(self.executed)
        return (self.passed / executed * 100) if executed else 0.0

    def slowest(self, count: int = 5) -> list[CaseResult]:
        return sorted(self.executed, key=lambda item: item.elapsed_ms, reverse=True)[:count]

    @property
    def cleanup_failed(self) -> int:
        return sum(
            1
            for item in self.executed
            if item.phase == "cleanup" and not item.passed and not item.error
        )

    @property
    def cleanup_errored(self) -> int:
        return sum(
            1 for item in self.executed if item.phase == "cleanup" and bool(item.error)
        )


def _decode(body: str) -> Any:
    text = (body or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _baseline_outcome(
    case: TestCase, document: Any, valid_json: bool
) -> AssertionOutcome | None:
    baseline = case.baseline
    if not baseline.enabled:
        return None

    assertion = Assertion(kind="baseline")
    if baseline.document is None:
        return AssertionOutcome(assertion, False, "no structural baseline document is configured")
    if not valid_json:
        return AssertionOutcome(assertion, False, "response body is not valid JSON")

    differences = compare_json_baseline(baseline.document, document, baseline)
    if not differences:
        return AssertionOutcome(assertion, True, "matches structural baseline")
    shown = "; ".join(
        f"{item.path}: {item.kind} (expected={item.expected!r}, actual={item.actual!r})"
        for item in differences[:5]
    )
    if len(differences) > 5:
        shown += f"; and {len(differences) - 5} more"
    return AssertionOutcome(assertion, False, shown)


def run_case(
    case: TestCase,
    endpoint: Endpoint,
    base_url: str,
    access_token: str,
    api_key: str,
    variables: dict[str, str],
    timeout: float = 30,
    verify_ssl: bool = True,
    custom_headers: dict[str, str] | None = None,
    auth_context: AuthenticationContext | None = None,
    catalog: Catalog | None = None,
) -> CaseResult:
    """Executes one case and evaluates its assertions, updating ``variables``."""
    case_started = perf_counter()
    result = CaseResult(
        case_id=case.id,
        case_name=case.name or f"{endpoint.method} {endpoint.path}",
        endpoint_id=endpoint.id,
        method=endpoint.method,
        path=endpoint.path,
        service=endpoint.service,
        expected_status=case.expected_status,
        passed=False,
        phase=case.phase,
    )
    values = {key: str(item) for key, item in apply_variables(case.values, variables).items()}
    payload = apply_variables(case.payload, variables)
    result.request_offset_ms = (perf_counter() - case_started) * 1000
    try:
        api_result: ApiResult = execute_endpoint(
            endpoint,
            base_url,
            access_token,
            api_key,
            values,
            payload,
            case.expected_status,
            timeout=timeout,
            verify_ssl=verify_ssl,
            custom_headers=(
                {key: str(value) for key, value in apply_variables(custom_headers, variables).items()}
                if custom_headers is not None else None
            ),
            **(
                {"auth_context": auth_context, "auth_mode": case.authentication}
                if auth_context is not None or case.authentication != "inherit" else {}
            ),
        )
    except AuthError as exc:
        result.error = str(exc)
        result.authentication_failed = True
        return result
    except (RuntimeError, ValueError) as exc:
        result.error = str(exc)
        return result

    result.status_code = api_result.status_code
    result.elapsed_ms = api_result.elapsed_ms
    result.url = api_result.url
    result.response_body = api_result.response_body
    result.response_headers = dict(api_result.response_headers)
    result.content = api_result.content
    result.content_type = api_result.content_type
    result.content_disposition = api_result.content_disposition
    result.request_headers = dict(api_result.request_headers)
    result.request_body = api_result.request_body
    result.timings = dict(api_result.timings)
    result.request_offset_ms += float(api_result.timings.get("request_offset_ms", 0))
    document = _decode(api_result.response_body)

    status_ok = status_matches(api_result.status_code, case.expected_status)
    response_schema = (
        catalog.response_schema(endpoint.response_schema) if catalog is not None else None
    )
    outcomes = [
        evaluate_assertion(
            assertion,
            api_result.status_code,
            api_result.response_headers,
            api_result.response_body,
            document,
            api_result.elapsed_ms,
            response_schema=response_schema,
        )
        for assertion in case.assertions
        if assertion.enabled
    ]
    baseline_document = document
    baseline_json_valid = True
    if case.baseline.enabled:
        try:
            baseline_document = (
                json.loads(api_result.response_body)
                if api_result.response_body.strip()
                else None
            )
        except json.JSONDecodeError:
            baseline_json_valid = False
    baseline_outcome = _baseline_outcome(
        case, baseline_document, baseline_json_valid
    )
    if baseline_outcome is not None:
        result.baseline_expected = (
            serialize_json_text(case.baseline.document)
            if case.baseline.document is not None else ""
        )
        result.baseline_actual = (
            serialize_json_text(baseline_document)
            if baseline_json_valid
            else api_result.response_body
        )
        if case.baseline.document is not None and not baseline_outcome.passed:
            result.baseline_differences = compare_json_baseline(
                case.baseline.document, baseline_document, case.baseline
            ) if baseline_json_valid else []
        outcomes.append(baseline_outcome)
    result.assertions = outcomes
    result.passed = status_ok and all(item.passed for item in outcomes)

    for capture in case.captures:
        captured = extract_capture(
            capture, api_result.status_code, api_result.response_headers, document
        )
        if captured is not None:
            variables[capture.name] = captured
            result.captured[capture.name] = captured
    return result


def _phase_order(suite: TestSuite, phase: str) -> list[TestCase]:
    """Return a stable dependency order, reversed for teardown."""
    selected = [case for case in suite.cases if case.phase == phase]
    selected_ids = {case.id for case in selected}
    visited: set[str] = set()
    ordered: list[TestCase] = []

    def visit(case: TestCase) -> None:
        if case.id in visited:
            return
        visited.add(case.id)
        for dependency_id in case.depends_on:
            if dependency_id in selected_ids:
                dependency = suite.case(dependency_id)
                if dependency is not None:
                    visit(dependency)
        ordered.append(case)

    for case in selected:
        visit(case)
    return list(reversed(ordered)) if phase == "cleanup" else ordered


def _case_result(
    case: TestCase,
    endpoint: Endpoint | None,
    *,
    skipped: bool = False,
    skip_reason: str = "",
    error: str = "",
) -> CaseResult:
    return CaseResult(
        case_id=case.id,
        case_name=case.name
        or (f"{endpoint.method} {endpoint.path}" if endpoint is not None else case.endpoint_id),
        endpoint_id=case.endpoint_id,
        method=endpoint.method if endpoint is not None else "",
        path=endpoint.path if endpoint is not None else "",
        service=endpoint.service if endpoint is not None else "",
        passed=False,
        skipped=skipped,
        skip_reason=skip_reason,
        error=error,
        phase=case.phase,
    )


def run_suite(
    suite: TestSuite,
    endpoints: dict[str, Endpoint],
    base_urls: dict[str, str],
    access_token: str,
    api_key: str,
    timeout: float = 30,
    verify_ssl: bool = True,
    custom_headers: dict[str, str] | None = None,
    environment_variables: dict[str, str] | None = None,
    on_result: Callable[[CaseResult], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    auth_context: AuthenticationContext | None = None,
    catalog: Catalog | None = None,
) -> SuiteResult:
    """Run dependency-ordered normal cases, then reverse-ordered cleanup cases."""
    suite.validate_dependencies()
    suite_result = SuiteResult(
        suite_name=suite.name, started_at=datetime.now(UTC).isoformat(timespec="seconds")
    )
    variables = dict(environment_variables or {})
    variables.update(suite.variables)
    stopped = False
    run_started = perf_counter()
    results_by_id: dict[str, CaseResult] = {}

    ordered_cases = _phase_order(suite, "normal") + _phase_order(suite, "cleanup")
    for case in ordered_cases:
        case_started = perf_counter()
        endpoint = endpoints.get(case.endpoint_id)
        failed_dependencies = [
            dependency_id
            for dependency_id in case.depends_on
            if dependency_id in results_by_id
            and (
                results_by_id[dependency_id].skipped
                or results_by_id[dependency_id].error
                or not results_by_id[dependency_id].passed
            )
        ]
        is_cleanup = case.phase == "cleanup"
        if not case.enabled:
            result = _case_result(
                case, endpoint, skipped=True, skip_reason="disabled"
            )
        elif failed_dependencies and not case.always_run and not is_cleanup:
            result = _case_result(
                case, endpoint, skipped=True, skip_reason="dependency_failed"
            )
        elif endpoint is None:
            result = _case_result(
                case,
                endpoint,
                error="Endpoint is no longer in the catalog — regenerate or repoint this case.",
            )
        elif stopped and not case.always_run and not is_cleanup:
            result = _case_result(
                case, endpoint, skipped=True, skip_reason="stopped"
            )
        else:
            base_url = base_urls.get(endpoint.service, "")
            if not base_url.strip():
                result = _case_result(
                    case,
                    endpoint,
                    error=f"No base URL configured for {endpoint.service}.",
                )
            else:
                result = run_case(
                    case,
                    endpoint,
                    base_url,
                    access_token,
                    api_key,
                    variables,
                    timeout,
                    verify_ssl,
                    custom_headers,
                    auth_context,
                    catalog,
                )

        result.start_offset_ms = (case_started - run_started) * 1000
        result.wall_ms = (perf_counter() - case_started) * 1000
        suite_result.results.append(result)
        results_by_id[case.id] = result
        if on_result is not None:
            on_result(result)
        if not is_cleanup and should_stop is not None and should_stop():
            stopped = True
        if (
            not is_cleanup
            and suite.stop_on_failure
            and not result.passed
            and not result.skipped
        ):
            stopped = True
        if not is_cleanup and result.authentication_failed:
            stopped = True

    suite_result.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    return suite_result


def append_suite_history(
    target: "WorkspaceStore | Path | str", result: SuiteResult
) -> None:
    """Records one suite run in the workspace database."""
    as_store(target).append_suite_history(
        {
            "suite": result.suite_name,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "errored": result.errored,
            "skipped": result.skipped,
            "duration_ms": result.duration_ms,
            "cleanup_failed": result.cleanup_failed,
            "cleanup_errored": result.cleanup_errored,
        }
    )


def export_report(result: SuiteResult, path: Path) -> None:
    """Writes a full JSON report of a suite run."""
    document = {
        "suite": result.suite_name,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "summary": {
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "errored": result.errored,
            "skipped": result.skipped,
            "pass_rate": round(result.pass_rate, 2),
            "duration_ms": result.duration_ms,
            "cleanup_failed": result.cleanup_failed,
            "cleanup_errored": result.cleanup_errored,
        },
        "cases": [
            {
                "name": case.case_name,
                "endpoint": f"{case.method} {case.path}".strip(),
                "service": case.service,
                "outcome": case.outcome,
                "status_code": case.status_code,
                "expected_status": case.expected_status,
                "elapsed_ms": case.elapsed_ms,
                "url": case.url,
                "error": case.error,
                "phase": case.phase,
                "skip_reason": case.skip_reason,
                "captured": case.captured,
                "response_body": case.response_body,
                "response_headers": case.response_headers,
                "baseline": {
                    "differences": [
                        difference.to_dict() for difference in case.baseline_differences
                    ],
                    "expected": case.baseline_expected,
                    "actual": case.baseline_actual,
                },
                "assertions": [
                    {
                        "assertion": item.assertion.label,
                        "passed": item.passed,
                        "detail": item.detail,
                    }
                    for item in case.assertions
                ],
            }
            for case in result.results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2)
        stream.write("\n")
