"""Tests for test-suite authoring, assertions, variables, and suite execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_tester.catalog import Endpoint, Parameter
from api_tester.client import ApiResult
from api_tester import runner as runner_module
from api_tester.runner import export_report, run_suite
from api_tester.suite import (
    ASSERTION_KINDS,
    Assertion,
    BaselineConfig,
    Capture,
    TestCase,
    TestSuite,
    apply_variables,
    clone_case,
    evaluate_assertion,
    extract_capture,
    load_suite,
    resolve_path,
    save_suite,
    validate_response_schema,
)

BODY = {
    "id": 7,
    "name": "Bayer",
    "status": "Active",
    "ratio": 1.5,
    "active": True,
    "deletedAt": None,
    "items": [{"code": "A", "value": 1}, {"code": "B", "value": 2}],
    "nested": {"deep": {"leaf": "found"}},
}


def endpoint(method: str = "GET", path: str = "/Brand") -> Endpoint:
    return Endpoint(
        id="ep1",
        service="MasterData",
        controller="Brand",
        action="Get",
        method=method,
        path=path,
        parameters=(Parameter(name="id", source="path", type="int", required=True),),
    )


def check(kind: str, path: str = "", value: str = "", elapsed_ms: int = 10) -> bool:
    return evaluate_assertion(
        Assertion(kind=kind, path=path, value=value),
        200,
        {"Content-Type": "application/json", "Content-Length": "42"},
        json.dumps(BODY),
        BODY,
        elapsed_ms,
    ).passed


# ------------------------------------------------------------------ paths


@pytest.mark.parametrize(
    "path,expected",
    [
        ("id", 7),
        ("name", "Bayer"),
        ("nested.deep.leaf", "found"),
        ("items[0].code", "A"),
        ("items[1].value", 2),
        ("$.name", "Bayer"),
        ("items.code", ["A", "B"]),
    ],
)
def test_resolve_path(path: str, expected: object) -> None:
    assert resolve_path(BODY, path) == expected


def test_resolve_path_is_case_insensitive_for_object_keys() -> None:
    assert resolve_path(BODY, "Name") == "Bayer"


def test_resolve_path_reports_missing_distinctly_from_null() -> None:
    from api_tester.suite import _MISSING

    assert resolve_path(BODY, "deletedAt") is None
    assert resolve_path(BODY, "nope") is _MISSING
    assert resolve_path(BODY, "items[9]") is _MISSING


# ------------------------------------------------------------- assertions


def test_every_declared_assertion_kind_is_implemented() -> None:
    for kind, meta in ASSERTION_KINDS.items():
        outcome = evaluate_assertion(
            Assertion(
                kind=kind,
                path="id" if meta["path"] else "",
                value="7" if meta["value"] else "",
            ),
            200,
            {"Content-Type": "application/json"},
            json.dumps(BODY),
            BODY,
            5,
        )
        assert "unknown assertion kind" not in outcome.detail


def test_json_equals_across_types() -> None:
    assert check("json_equals", "name", "Bayer")
    assert check("json_equals", "id", "7")
    assert check("json_equals", "ratio", "1.5")
    assert check("json_equals", "active", "true")
    assert check("json_equals", "deletedAt", "null")
    assert not check("json_equals", "name", "Syngenta")


def test_json_exists_and_absent() -> None:
    assert check("json_exists", "nested.deep.leaf")
    assert not check("json_exists", "nested.deep.missing")
    assert check("json_absent", "nested.deep.missing")
    assert not check("json_absent", "id")


def test_null_field_exists_but_is_empty() -> None:
    assert check("json_exists", "deletedAt")
    assert not check("json_not_empty", "deletedAt")


def test_length_assertions() -> None:
    assert check("json_length_eq", "items", "2")
    assert check("json_length_gte", "items", "1")
    assert check("json_length_lte", "items", "2")
    assert not check("json_length_eq", "items", "3")


def test_length_of_non_sized_value_fails_cleanly() -> None:
    outcome = evaluate_assertion(
        Assertion(kind="json_length_eq", path="id", value="1"),
        200,
        {},
        json.dumps(BODY),
        BODY,
        1,
    )
    assert not outcome.passed
    assert "no length" in outcome.detail


def test_json_type_and_contains() -> None:
    assert check("json_type", "items", "array")
    assert check("json_type", "nested", "object")
    assert check("json_type", "id", "number")
    assert check("json_type", "active", "boolean")
    assert check("json_type", "deletedAt", "null")
    assert check("json_contains", "items.code", "A")
    assert check("json_contains", "name", "Bay")


def test_body_and_header_assertions() -> None:
    assert check("body_contains", value="Bayer")
    assert not check("body_not_contains", value="Bayer")
    assert check("body_matches", value=r'"id":\s*7')
    assert check("header_equals", "content-type", "application/json")
    assert check("header_exists", "Content-Length")
    assert not check("header_exists", "X-Nope")


def test_duration_assertion() -> None:
    assert check("max_duration_ms", value="100", elapsed_ms=50)
    assert not check("max_duration_ms", value="10", elapsed_ms=50)


BRAND_RESPONSE_SCHEMA = {
    "kind": "object",
    "name": "Brand",
    "clr_type": "Brand",
    "fields": [
        {"name": "id", "kind": "integer", "clr_type": "int", "required": True, "nullable": False},
        {"name": "name", "kind": "text", "clr_type": "string", "required": True, "nullable": False},
        {"name": "status", "kind": "enum", "clr_type": "BrandStatus", "lov": ["Active", "Inactive"]},
        {"name": "ratio", "kind": "number", "clr_type": "decimal"},
        {"name": "active", "kind": "boolean", "clr_type": "bool"},
        {"name": "deletedAt", "kind": "date", "clr_type": "DateTime", "nullable": True},
        {
            "name": "items",
            "kind": "array",
            "item": {
                "kind": "object",
                "fields": [
                    {"name": "code", "kind": "text"},
                    {"name": "value", "kind": "integer"},
                ],
            },
        },
    ],
}


def test_validate_response_schema_accepts_matching_document() -> None:
    assert validate_response_schema(BRAND_RESPONSE_SCHEMA, BODY) == []


def test_validate_response_schema_flags_missing_required_field() -> None:
    document = dict(BODY)
    del document["name"]
    problems = validate_response_schema(BRAND_RESPONSE_SCHEMA, document)
    assert any("name" in item and "missing" in item for item in problems)


def test_validate_response_schema_flags_type_mismatch() -> None:
    document = dict(BODY)
    document["ratio"] = "not-a-number"
    problems = validate_response_schema(BRAND_RESPONSE_SCHEMA, document)
    assert any("ratio" in item for item in problems)


def test_validate_response_schema_flags_bad_enum_value() -> None:
    document = dict(BODY)
    document["status"] = "Unknown"
    problems = validate_response_schema(BRAND_RESPONSE_SCHEMA, document)
    assert any("status" in item for item in problems)


def test_validate_response_schema_flags_wrong_array_item_shape() -> None:
    document = dict(BODY)
    document["items"] = [{"code": "A", "value": "not-a-number"}]
    problems = validate_response_schema(BRAND_RESPONSE_SCHEMA, document)
    assert any("items[0].value" in item for item in problems)


def test_validate_response_schema_missing_schema_fails() -> None:
    assert validate_response_schema(None, BODY) == ["no response schema is configured for this endpoint"]


def test_validate_response_schema_top_level_shape_mismatch() -> None:
    problems = validate_response_schema(BRAND_RESPONSE_SCHEMA, ["not", "an", "object"])
    assert any("expected an object" in item for item in problems)


def test_response_schema_assertion_kind_registered() -> None:
    assert "response_schema" in ASSERTION_KINDS
    assert ASSERTION_KINDS["response_schema"]["path"] is False
    assert ASSERTION_KINDS["response_schema"]["value"] is False


def test_response_schema_assertion_passes_and_fails() -> None:
    passing = evaluate_assertion(
        Assertion(kind="response_schema"),
        200,
        {},
        json.dumps(BODY),
        BODY,
        10,
        response_schema=BRAND_RESPONSE_SCHEMA,
    )
    assert passing.passed

    broken = dict(BODY)
    del broken["id"]
    failing = evaluate_assertion(
        Assertion(kind="response_schema"),
        200,
        {},
        json.dumps(broken),
        broken,
        10,
        response_schema=BRAND_RESPONSE_SCHEMA,
    )
    assert not failing.passed
    assert "id" in failing.detail


def test_response_schema_assertion_without_configured_schema_fails() -> None:
    outcome = evaluate_assertion(
        Assertion(kind="response_schema"), 200, {}, json.dumps(BODY), BODY, 10
    )
    assert not outcome.passed
    assert "no response schema" in outcome.detail


def test_invalid_regex_fails_instead_of_raising() -> None:
    outcome = evaluate_assertion(
        Assertion(kind="body_matches", value="["), 200, {}, "text", None, 1
    )
    assert not outcome.passed
    assert "invalid regex" in outcome.detail


def test_json_assertion_on_non_json_body_fails_clearly() -> None:
    outcome = evaluate_assertion(
        Assertion(kind="json_equals", path="id", value="7"),
        200,
        {},
        "<html>not json</html>",
        None,
        1,
    )
    assert not outcome.passed
    assert "not valid JSON" in outcome.detail


# -------------------------------------------------------------- variables


def test_apply_variables_substitutes_nested_structures() -> None:
    template = {
        "path:id": "{{brandId}}",
        "payload": {"name": "{{prefix}}-suffix", "tags": ["{{prefix}}"]},
    }
    result = apply_variables(template, {"brandId": "42", "prefix": "trial"})
    assert result["path:id"] == "42"
    assert result["payload"]["name"] == "trial-suffix"
    assert result["payload"]["tags"] == ["trial"]


def test_unknown_variables_are_left_untouched() -> None:
    assert apply_variables("{{missing}}", {}) == "{{missing}}"


def test_extract_capture_from_body_header_and_status() -> None:
    headers = {"Location": "/Brand/7"}
    assert extract_capture(Capture("id", "id"), 201, headers, BODY) == "7"
    assert (
        extract_capture(Capture("loc", "location", "header"), 201, headers, BODY)
        == "/Brand/7"
    )
    assert extract_capture(Capture("code", "", "status"), 201, headers, BODY) == "201"
    assert extract_capture(Capture("x", "nope"), 201, headers, BODY) is None


def test_capture_of_object_is_serialized_as_json() -> None:
    captured = extract_capture(Capture("nested", "nested.deep"), 200, {}, BODY)
    assert json.loads(captured) == {"leaf": "found"}


# ------------------------------------------------------------ persistence


def test_suite_round_trips_through_disk(tmp_path: Path) -> None:
    suite = TestSuite(
        name="Brand smoke",
        variables={"brandId": "1"},
        stop_on_failure=True,
        cases=[
            TestCase(
                endpoint_id="ep1",
                name="List brands",
                values={"query:Filter": "Status__eq:=Active"},
                payload={"Name": "X"},
                expected_status="200",
                assertions=[Assertion(kind="json_length_gte", path="items", value="1")],
                captures=[Capture(name="brandId", path="items[0].id")],
                baseline=BaselineConfig(
                    enabled=True,
                    document={"Name": "X"},
                    ignored_paths=["$.updatedAt"],
                    ignore_array_order=True,
                    array_identity_keys={"$.items": "id"},
                    numeric_tolerance=0.001,
                ),
            )
        ],
    )
    path = tmp_path / "suite.json"
    save_suite(suite, path)
    loaded = load_suite(path)

    assert loaded.name == "Brand smoke"
    assert loaded.stop_on_failure is True
    assert loaded.variables == {"brandId": "1"}
    assert len(loaded.cases) == 1
    case = loaded.cases[0]
    assert case.values == {"query:Filter": "Status__eq:=Active"}
    assert case.payload == {"Name": "X"}
    assert case.assertions[0].kind == "json_length_gte"
    assert case.captures[0].name == "brandId"
    assert case.baseline.enabled is True
    assert case.baseline.document == {"Name": "X"}
    assert case.baseline.ignored_paths == ["$.updatedAt"]
    assert case.baseline.ignore_array_order is True
    assert case.baseline.array_identity_keys == {"$.items": "id"}
    assert case.baseline.numeric_tolerance == 0.001
    assert loaded.path == path


def test_legacy_case_without_baseline_gets_disabled_default() -> None:
    case = TestCase.from_dict({"endpoint_id": "ep1"})

    assert case.baseline.enabled is False
    assert case.baseline.document is None
    assert case.baseline.ignored_paths == []


def test_clone_case_is_independent() -> None:
    case = TestCase(
        endpoint_id="ep1",
        name="One",
        values={"a": "1"},
        payload={"nested": {"x": 1}},
        assertions=[Assertion(kind="json_exists", path="id")],
    )
    copy = clone_case(case)
    copy.values["a"] = "2"
    copy.payload["nested"]["x"] = 9
    copy.assertions[0].path = "other"

    assert case.values["a"] == "1"
    assert case.payload["nested"]["x"] == 1
    assert case.assertions[0].path == "id"
    assert copy.id != case.id


# ---------------------------------------------------------------- runner


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch):
    """Replaces the HTTP call so suite execution can be tested offline."""
    calls: list[dict] = []

    def fake_execute(
        ep,
        base_url,
        token,
        key,
        values,
        payload,
        expected,
        timeout=30,
        verify_ssl=True,
        custom_headers=None,
    ):
        calls.append(
            {
                "endpoint": ep.id,
                "values": dict(values),
                "payload": payload,
                "base_url": base_url,
                "timeout": timeout,
                "verify_ssl": verify_ssl,
                "custom_headers": dict(custom_headers or {}),
            }
        )
        return ApiResult(
            passed=True,
            status_code=200,
            elapsed_ms=12,
            url=f"{base_url}{ep.path}",
            response_headers={"Content-Type": "application/json"},
            response_body=json.dumps(BODY),
        )

    monkeypatch.setattr(runner_module, "execute_endpoint", fake_execute)
    return calls


def test_run_suite_passes_when_status_and_assertions_hold(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(
                endpoint_id="ep1",
                expected_status="200",
                assertions=[Assertion(kind="json_equals", path="name", value="Bayer")],
            )
        ]
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.total == 1
    assert result.passed == 1
    assert result.failed == 0
    assert result.pass_rate == 100.0
    assert result.results[0].outcome == "PASS"


def test_run_suite_passes_when_enabled_baseline_matches(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(
                endpoint_id="ep1",
                expected_status="200",
                baseline=BaselineConfig(enabled=True, document=BODY),
            )
        ]
    )

    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.passed == 1
    assert result.results[0].baseline_differences == []
    assert result.results[0].assertions[-1].assertion.kind == "baseline"
    assert result.results[0].assertions[-1].passed is True


def test_run_suite_fails_once_when_enabled_baseline_mismatches(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(
                endpoint_id="ep1",
                expected_status="200",
                baseline=BaselineConfig(enabled=True, document={**BODY, "name": "Wrong"}),
            )
        ]
    )

    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.failed == 1
    case = result.results[0]
    assert [(item.path, item.kind) for item in case.baseline_differences] == [
        ("$.name", "value")
    ]
    assert [item.assertion.kind for item in case.failed_assertions] == ["baseline"]
    assert case.baseline_expected
    assert case.baseline_actual
    assert case.baseline_expected_text == case.baseline_expected
    assert case.baseline_actual_text == case.baseline_actual


def test_run_suite_fails_clearly_when_enabled_baseline_gets_non_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_execute(*args, **kwargs):
        return ApiResult(
            passed=True,
            status_code=200,
            elapsed_ms=1,
            url="https://x/Brand",
            response_headers={"Content-Type": "text/html"},
            response_body="<html>not json</html>",
        )

    monkeypatch.setattr(runner_module, "execute_endpoint", fake_execute)
    suite = TestSuite(
        cases=[
            TestCase(
                endpoint_id="ep1",
                expected_status="200",
                baseline=BaselineConfig(enabled=True, document=BODY),
            )
        ]
    )

    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.failed == 1
    assert result.results[0].failed_assertions[0].assertion.kind == "baseline"
    assert "not valid JSON" in result.results[0].failed_assertions[0].detail


def test_valid_json_null_is_a_structural_value_not_a_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_execute(*args, **kwargs):
        return ApiResult(
            passed=True,
            status_code=200,
            elapsed_ms=1,
            url="https://x/Brand",
            response_headers={"Content-Type": "application/json"},
            response_body="null",
        )

    monkeypatch.setattr(runner_module, "execute_endpoint", fake_execute)
    suite = TestSuite(
        cases=[
            TestCase(
                endpoint_id="ep1",
                baseline=BaselineConfig(enabled=True, document={"id": 1}),
            )
        ]
    )

    result = run_suite(
        suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", ""
    )

    outcome = result.results[0].failed_assertions[0]
    assert "not valid JSON" not in outcome.detail
    assert result.results[0].baseline_differences[0].kind == "type"


def test_run_suite_verifies_ssl_by_default(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1", expected_status="200")])
    run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")
    assert fake_api[0]["verify_ssl"] is True


def test_run_suite_can_disable_ssl_verification_for_local_development(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1", expected_status="200")])
    run_suite(
        suite,
        {"ep1": endpoint()},
        {"MasterData": "https://localhost:5001"},
        "",
        "",
        verify_ssl=False,
    )
    assert fake_api[0]["verify_ssl"] is False


def test_run_suite_uses_the_configured_request_timeout(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1", expected_status="200")])
    run_suite(
        suite,
        {"ep1": endpoint()},
        {"MasterData": "https://x"},
        "",
        "",
        timeout=180,
    )
    assert fake_api[0]["timeout"] == 180


def test_case_fails_when_an_assertion_fails_even_if_status_matches(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(
                endpoint_id="ep1",
                expected_status="200",
                assertions=[Assertion(kind="json_equals", path="name", value="Wrong")],
            )
        ]
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.failed == 1
    assert result.results[0].failed_assertions


def test_case_fails_when_status_does_not_match(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1", expected_status="404")])
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")
    assert result.failed == 1


def test_disabled_cases_are_skipped(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(endpoint_id="ep1", expected_status="200", enabled=False),
            TestCase(endpoint_id="ep1", expected_status="200"),
        ]
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.skipped == 1
    assert result.passed == 1
    assert len(fake_api) == 1


def test_captured_variable_flows_into_the_next_case(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(
                endpoint_id="ep1",
                expected_status="200",
                captures=[Capture(name="brandId", path="id")],
            ),
            TestCase(
                endpoint_id="ep1",
                expected_status="200",
                values={"path:id": "{{brandId}}"},
            ),
        ]
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.results[0].captured == {"brandId": "7"}
    assert fake_api[1]["values"]["path:id"] == "7"


def test_suite_variables_seed_the_first_case(fake_api) -> None:
    suite = TestSuite(
        variables={"brandId": "99"},
        cases=[TestCase(endpoint_id="ep1", values={"path:id": "{{brandId}}"})],
    )
    run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")
    assert fake_api[0]["values"]["path:id"] == "99"


def test_stop_on_failure_skips_remaining_cases(fake_api) -> None:
    suite = TestSuite(
        stop_on_failure=True,
        cases=[
            TestCase(endpoint_id="ep1", expected_status="404"),
            TestCase(endpoint_id="ep1", expected_status="200"),
        ],
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.failed == 1
    assert result.skipped == 1
    assert len(fake_api) == 1


def test_missing_base_url_is_reported_as_an_error_not_a_crash(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1")])
    result = run_suite(suite, {"ep1": endpoint()}, {}, "", "")

    assert result.errored == 1
    assert "base URL" in result.results[0].error
    assert fake_api == []


def test_case_pointing_at_a_removed_endpoint_errors_clearly(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="gone")])
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.errored == 1
    assert "no longer in the catalog" in result.results[0].error


def test_request_exception_is_captured_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise ValueError("Required path values are missing: id")

    monkeypatch.setattr(runner_module, "execute_endpoint", boom)
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1")])
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.errored == 1
    assert "missing" in result.results[0].error


def test_should_stop_callback_halts_the_run(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1") for _ in range(4)])
    result = run_suite(
        suite,
        {"ep1": endpoint()},
        {"MasterData": "https://x"},
        "",
        "",
        should_stop=lambda: True,
    )

    assert len(fake_api) == 1
    assert result.skipped == 3


def test_on_result_streams_each_case(fake_api) -> None:
    seen: list[str] = []
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1") for _ in range(3)])
    run_suite(
        suite,
        {"ep1": endpoint()},
        {"MasterData": "https://x"},
        "",
        "",
        on_result=lambda item: seen.append(item.outcome),
    )
    assert len(seen) == 3


def test_suite_result_summary_and_slowest(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1", expected_status="200")])
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.duration_ms == 12
    assert result.slowest()[0].elapsed_ms == 12
    assert result.finished_at


def test_export_report_contains_summary_and_cases(fake_api, tmp_path: Path) -> None:
    suite = TestSuite(
        name="Report",
        cases=[
            TestCase(
                endpoint_id="ep1",
                name="Check",
                expected_status="200",
                assertions=[Assertion(kind="json_equals", path="name", value="Bayer")],
            )
        ],
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")
    path = tmp_path / "report.json"
    export_report(result, path)

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["summary"]["passed"] == 1
    assert document["summary"]["pass_rate"] == 100.0
    assert document["cases"][0]["assertions"][0]["passed"] is True
    assert document["cases"][0]["response_body"]


def test_export_report_contains_baseline_mismatch_details(fake_api, tmp_path: Path) -> None:
    suite = TestSuite(
        name="Report",
        cases=[
            TestCase(
                endpoint_id="ep1",
                name="Check",
                expected_status="200",
                baseline=BaselineConfig(enabled=True, document={**BODY, "name": "Wrong"}),
            )
        ],
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")
    path = tmp_path / "report.json"
    export_report(result, path)

    document = json.loads(path.read_text(encoding="utf-8"))
    baseline = document["cases"][0]["baseline"]
    assert baseline["differences"] == [
        {"path": "$.name", "kind": "value", "expected": "Wrong", "actual": "Bayer"}
    ]
    assert '"name": "Wrong"' in baseline["expected"]
    assert '"name": "Bayer"' in baseline["actual"]


def test_run_suite_records_a_wall_clock_offset_for_every_case(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(endpoint_id="ep1", expected_status="200"),
            TestCase(endpoint_id="ep1", expected_status="200"),
            TestCase(endpoint_id="ep1", expected_status="200"),
        ]
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    offsets = [case.start_offset_ms for case in result.results]
    assert offsets[0] == pytest.approx(0.0, abs=1.0), "the first case starts at the run origin"
    assert offsets == sorted(offsets), "cases run in order, so offsets must not go backwards"
    for earlier, later in zip(result.results, result.results[1:]):
        assert earlier.start_offset_ms + earlier.wall_ms <= later.start_offset_ms + 1e-6


def test_run_suite_times_the_whole_case_not_just_the_http_call(fake_api) -> None:
    suite = TestSuite(cases=[TestCase(endpoint_id="ep1", expected_status="200")])
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    case = result.results[0]
    assert case.wall_ms > 0, "a case that ran took some measurable time"
    assert case.request_offset_ms >= 0


def test_a_skipped_case_still_carries_its_position_in_the_run(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(endpoint_id="ep1", expected_status="200"),
            TestCase(endpoint_id="ep1", expected_status="200", enabled=False),
        ]
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    skipped = result.results[1]
    assert skipped.skipped is True
    assert skipped.start_offset_ms >= result.results[0].start_offset_ms


def test_wall_duration_covers_overhead_that_the_sum_of_calls_misses(fake_api) -> None:
    suite = TestSuite(
        cases=[
            TestCase(endpoint_id="ep1", expected_status="200"),
            TestCase(endpoint_id="ep1", expected_status="200"),
        ]
    )
    result = run_suite(suite, {"ep1": endpoint()}, {"MasterData": "https://x"}, "", "")

    assert result.duration_ms == 24, "unchanged: still the sum of the HTTP calls"
    assert result.wall_duration_ms > 0


def test_wall_duration_is_zero_for_a_result_without_recorded_offsets() -> None:
    from api_tester.runner import CaseResult, SuiteResult

    result = SuiteResult(
        suite_name="legacy",
        started_at="",
        results=[
            CaseResult(
                case_id="a",
                case_name="a",
                endpoint_id="ep1",
                method="GET",
                path="/Brand",
                service="MasterData",
                passed=True,
                elapsed_ms=30,
            )
        ],
    )
    assert result.wall_duration_ms == 0.0
