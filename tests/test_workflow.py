"""Focused dependency and cleanup workflow tests."""

from __future__ import annotations

import json

import pytest

from api_tester import runner as runner_module
from api_tester.authentication import AuthError
from api_tester.catalog import Endpoint
from api_tester.client import ApiResult
from api_tester.runner import export_report, run_suite
from api_tester.suite import TestCase, TestSuite


def endpoint(endpoint_id: str) -> Endpoint:
    return Endpoint(
        id=endpoint_id,
        service="Auth",
        controller="Test",
        action=endpoint_id,
        method="POST",
        path=f"/{endpoint_id}",
    )


@pytest.fixture
def workflow_api(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def execute(ep, *args, **kwargs):
        calls.append(ep.id)
        return ApiResult(
            passed=True,
            status_code=500 if ep.id == "fail" else 200,
            elapsed_ms=1,
            url=f"https://example.test/{ep.id}",
            response_headers={},
            response_body="{}",
        )

    monkeypatch.setattr(runner_module, "execute_endpoint", execute)
    return calls


def run(cases: list[TestCase]):
    endpoints = {case.endpoint_id: endpoint(case.endpoint_id) for case in cases}
    return run_suite(TestSuite(cases=cases), endpoints, {"Auth": "https://example.test"}, "", "")


def test_legacy_suite_loads_with_workflow_defaults() -> None:
    suite = TestSuite.from_dict(
        {"name": "Legacy", "cases": [{"id": "old", "endpoint_id": "list"}]}
    )

    assert suite.cases[0].depends_on == []
    assert suite.cases[0].phase == "normal"
    assert suite.cases[0].always_run is False


def test_workflow_fields_round_trip_in_suite_json() -> None:
    suite = TestSuite(
        cases=[
            TestCase(id="setup", endpoint_id="setup"),
            TestCase(
                id="cleanup",
                endpoint_id="cleanup",
                depends_on=["setup"],
                phase="cleanup",
                always_run=True,
            ),
        ]
    )

    loaded = TestSuite.from_dict(suite.to_dict())
    cleanup = loaded.cases[1]

    assert cleanup.depends_on == ["setup"]
    assert cleanup.phase == "cleanup"
    assert cleanup.always_run is True


@pytest.mark.parametrize(
    ("cases", "message"),
    [
        (
            [TestCase(id="a", endpoint_id="a", depends_on=["missing"])],
            "unknown case missing",
        ),
        (
            [TestCase(id="a", endpoint_id="a", depends_on=["a"])],
            "cannot depend on itself",
        ),
        (
            [
                TestCase(id="a", endpoint_id="a", depends_on=["b"]),
                TestCase(id="b", endpoint_id="b", depends_on=["a"]),
            ],
            "Dependency cycle detected: a -> b -> a",
        ),
    ],
)
def test_dependency_graph_validation(cases: list[TestCase], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TestSuite(cases=cases).validate_dependencies()


def test_normal_cases_run_in_stable_topological_order(workflow_api) -> None:
    cases = [
        TestCase(id="child", endpoint_id="child", depends_on=["root"]),
        TestCase(id="independent", endpoint_id="independent"),
        TestCase(id="root", endpoint_id="root"),
    ]

    result = run(cases)

    assert [item.case_id for item in result.results] == ["root", "child", "independent"]
    assert workflow_api == ["root", "child", "independent"]


def test_failed_dependency_skips_dependents_but_independent_branch_continues(
    workflow_api,
) -> None:
    cases = [
        TestCase(id="failed", endpoint_id="fail", expected_status="200"),
        TestCase(id="blocked", endpoint_id="blocked", depends_on=["failed"]),
        TestCase(id="branch", endpoint_id="branch"),
        TestCase(id="transitive", endpoint_id="transitive", depends_on=["blocked"]),
    ]

    result = run(cases)
    by_id = {item.case_id: item for item in result.results}

    assert workflow_api == ["fail", "branch"]
    assert by_id["blocked"].skip_reason == "dependency_failed"
    assert by_id["transitive"].skip_reason == "dependency_failed"
    assert by_id["branch"].passed is True


def test_cleanup_runs_after_failure_and_stop_request(workflow_api) -> None:
    cases = [
        TestCase(id="failed", endpoint_id="fail", expected_status="200"),
        TestCase(id="not-run", endpoint_id="not-run"),
        TestCase(
            id="cleanup",
            endpoint_id="cleanup",
            phase="cleanup",
            depends_on=["failed"],
        ),
    ]
    endpoints = {case.endpoint_id: endpoint(case.endpoint_id) for case in cases}

    result = run_suite(
        TestSuite(cases=cases),
        endpoints,
        {"Auth": "https://example.test"},
        "",
        "",
        should_stop=lambda: True,
    )

    assert workflow_api == ["fail", "cleanup"]
    assert result.results[1].skip_reason == "stopped"
    assert result.results[-1].phase == "cleanup"
    assert result.results[-1].passed is True


def test_always_run_normal_case_bypasses_stop_request(workflow_api) -> None:
    cases = [
        TestCase(id="first", endpoint_id="first"),
        TestCase(id="ordinary", endpoint_id="ordinary"),
        TestCase(id="always", endpoint_id="always", always_run=True),
    ]
    endpoints = {case.endpoint_id: endpoint(case.endpoint_id) for case in cases}

    result = run_suite(
        TestSuite(cases=cases),
        endpoints,
        {"Auth": "https://example.test"},
        "",
        "",
        should_stop=lambda: True,
    )

    assert workflow_api == ["first", "always"]
    assert result.results[1].skip_reason == "stopped"
    assert result.results[2].passed is True


@pytest.mark.parametrize("exception", [ValueError("request error"), AuthError("auth error")])
def test_cleanup_runs_after_request_or_auth_error(
    monkeypatch: pytest.MonkeyPatch, exception: Exception
) -> None:
    calls: list[str] = []

    def execute(ep, *args, **kwargs):
        calls.append(ep.id)
        if ep.id == "setup":
            raise exception
        return ApiResult(True, 200, 1, "https://example.test", {}, "{}")

    monkeypatch.setattr(runner_module, "execute_endpoint", execute)
    cases = [
        TestCase(id="setup", endpoint_id="setup"),
        TestCase(id="cleanup", endpoint_id="cleanup", phase="cleanup"),
    ]

    result = run(cases)

    assert calls == ["setup", "cleanup"]
    assert result.results[0].error == str(exception)
    assert result.results[-1].passed is True


def test_cleanup_uses_reverse_dependency_and_declaration_order(workflow_api) -> None:
    cases = [
        TestCase(id="setup", endpoint_id="setup"),
        TestCase(
            id="parent-cleanup",
            endpoint_id="parent-cleanup",
            phase="cleanup",
            depends_on=["setup"],
        ),
        TestCase(
            id="child-cleanup",
            endpoint_id="child-cleanup",
            phase="cleanup",
            depends_on=["parent-cleanup"],
        ),
        TestCase(id="last-cleanup", endpoint_id="last-cleanup", phase="cleanup"),
    ]

    result = run(cases)

    assert [item.case_id for item in result.results] == [
        "setup",
        "last-cleanup",
        "child-cleanup",
        "parent-cleanup",
    ]
    assert workflow_api == [
        "setup",
        "last-cleanup",
        "child-cleanup",
        "parent-cleanup",
    ]


def test_disabled_cleanup_remains_skipped(workflow_api) -> None:
    result = run(
        [
            TestCase(id="setup", endpoint_id="setup"),
            TestCase(
                id="cleanup",
                endpoint_id="cleanup",
                phase="cleanup",
                enabled=False,
            ),
        ]
    )

    cleanup = result.results[-1]
    assert cleanup.skipped is True
    assert cleanup.skip_reason == "disabled"
    assert workflow_api == ["setup"]


def test_cleanup_failure_is_separate_and_part_of_overall_failure(
    workflow_api, tmp_path
) -> None:
    result = run(
        [
            TestCase(
                id="cleanup",
                endpoint_id="fail",
                phase="cleanup",
                expected_status="200",
            )
        ]
    )
    report_path = tmp_path / "report.json"
    export_report(result, report_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert result.failed == 1
    assert result.cleanup_failed == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["cleanup_failed"] == 1
    assert report["cases"][0]["phase"] == "cleanup"
