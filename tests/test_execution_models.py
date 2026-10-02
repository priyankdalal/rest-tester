from __future__ import annotations

import copy

import pytest

from api_tester.execution.models import (
    EVENT_KINDS,
    ExecutionEnvironmentSnapshot,
    ExecutionEvent,
    RequestSample,
    RequestTemplate,
    new_run_id,
)


def test_environment_snapshot_deep_copies_mutable_inputs() -> None:
    base_urls = {"TrialMaster": "https://qa.example.com"}
    variables = {"PartnerId": "1"}
    snapshot = ExecutionEnvironmentSnapshot(
        environment_id="env-1", environment_name="QA", base_urls=base_urls, variables=variables,
    )
    base_urls["TrialMaster"] = "mutated"
    variables["PartnerId"] = "mutated"
    assert snapshot.base_url("TrialMaster") == "https://qa.example.com"
    assert snapshot.variables["PartnerId"] == "1"


def test_environment_snapshot_to_dict_excludes_auth_context() -> None:
    snapshot = ExecutionEnvironmentSnapshot(
        environment_id="env-1", environment_name="QA", base_urls={}, auth_context=object(),
    )
    document = snapshot.to_dict()
    assert "auth_context" not in document
    assert document["environment_id"] == "env-1"


def test_request_template_with_values_merges_and_preserves_payload() -> None:
    template = RequestTemplate(
        endpoint_id="brand.create", service="TrialMaster", method="POST", path="/Brand",
        payload={"Name": "template-default"},
    )
    resolved = template.with_values({"path:Id": "42"})
    assert resolved.values == {"path:Id": "42"}
    assert resolved.payload == {"Name": "template-default"}
    # Mutating the resolved copy's payload must not reach back into the template.
    resolved.payload["Name"] = "mutated"
    assert template.payload["Name"] == "template-default"


def test_request_template_with_values_can_replace_payload() -> None:
    template = RequestTemplate(endpoint_id="e", service="s", method="POST", path="/p", payload={"a": 1})
    resolved = template.with_values({}, payload={"a": 2})
    assert resolved.payload == {"a": 2}


def test_request_template_round_trips_through_dict() -> None:
    template = RequestTemplate(
        endpoint_id="brand.create", service="TrialMaster", method="POST", path="/Brand",
        values={"payload:Name": "x"}, payload={"Name": "x"}, weight=0.45, think_time_ms=250,
    )
    restored = RequestTemplate.from_dict(template.to_dict())
    assert restored == template


def test_request_sample_to_dict_has_no_response_body_field() -> None:
    sample = RequestSample(
        run_id="DR-1", endpoint_id="brand.create", service="TrialMaster", method="POST",
        outcome="passed", started_at="2026-09-25T00:00:00Z", offset_ms=12.0,
        queue_delay_ms=0.0, http_ms=88.0, status_code=201,
    )
    document = sample.to_dict()
    assert "response_body" not in document
    assert "response_headers" not in document
    assert document["status_code"] == 201


@pytest.mark.parametrize("kind", EVENT_KINDS)
def test_execution_event_accepts_every_canonical_kind(kind: str) -> None:
    event = ExecutionEvent(kind=kind, run_id="DR-1")
    assert event.kind == kind


def test_execution_event_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        ExecutionEvent(kind="not_a_real_event", run_id="DR-1")


def test_execution_event_payload_is_copied() -> None:
    payload = {"row": 1}
    event = ExecutionEvent(kind="row_completed", run_id="DR-1", payload=payload)
    payload["row"] = 2
    assert event.payload["row"] == 1


def test_new_run_id_has_stable_prefix_and_is_unique() -> None:
    first = new_run_id("DR")
    second = new_run_id("DR")
    assert first.startswith("DR-")
    assert first != second
