import json
from pathlib import Path
from unittest.mock import Mock

from api_tester.ai.catalog_index import CatalogIndex
from api_tester.ai.config import AiSettings
from api_tester.ai.planner import RequestPlanner, SuitePlanner
from api_tester.ai.provider import ChatMessage, LlmResponse, LlmResponseError
from api_tester.ai.providers.fake import FakeProvider
from api_tester.ai.providers.hosted import HostedProvider
from api_tester.ai.request_plan import RequestPlan
from api_tester.ai.suite_plan import SuiteCasePlan, SuitePlan
from api_tester.catalog import load_catalog


def test_activity_reports_repairs_without_known_secrets():
    index = CatalogIndex(load_catalog(Path("examples/store_catalog.json")))
    events = []
    value = RequestPlan(endpoint_id="f76aeb49ca37").to_json()
    provider = FakeProvider([
        LlmResponseError("Invalid JSON; api_key=secret123"),
        LlmResponse(text=json.dumps(value), json=value, input_tokens=10,
                    output_tokens=5, extra={"finish_reason": "stop"}),
    ])
    outcome = RequestPlanner(provider, index, AiSettings(max_repairs=1),
                             secrets=("secret123",), progress=events.append).plan("list products")
    assert outcome.ok and outcome.attempts == 2
    log = "\n".join(events)
    assert "secret123" not in log
    assert "failed after" in log
    assert "finish reason: stop" in log
    assert "Catalog validation passed" in log


def test_suite_activity_reports_cases():
    index = CatalogIndex(load_catalog(Path("examples/store_catalog.json")))
    events = []
    value = SuitePlan(name="Read products", cases=(
        SuiteCasePlan(key="read", endpoint_id="f76aeb49ca37"),
    )).to_json()
    provider = FakeProvider([LlmResponse(text=json.dumps(value), json=value)])
    outcome = SuitePlanner(provider, index, AiSettings(max_repairs=0),
                           progress=events.append).plan("list products")
    assert outcome.ok
    assert any("Suite contains 1 cases" in event for event in events)


def test_invalid_hosted_json_reports_truncation():
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"finish_reason": "length", "message": {"content": '{"broken":'}}],
    }
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    try:
        provider.complete([ChatMessage("user", "Plan")], model="sarvam-105b",
                          response_schema={"type": "object"})
    except LlmResponseError as exc:
        assert "Output token limit reached" in str(exc)
    else:
        raise AssertionError("Invalid JSON must fail")
