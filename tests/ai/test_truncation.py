"""Structured-output truncation stops without repeating an unchanged budget."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from api_tester.ai.catalog_index import CatalogIndex
from api_tester.ai.config import AiSettings
from api_tester.ai.planner import RequestPlanner, SuitePlanner
from api_tester.ai.provider import ChatMessage, LlmResponseError
from api_tester.ai.providers.hosted import HostedProvider
from api_tester.ai.usage_store import TrackedProvider, UsageStore
from api_tester.ai.workflow_plan import WorkflowPlanner
from api_tester.catalog import load_catalog


@pytest.mark.parametrize("mode,budget", [("request", 2000), ("suite", 6000), ("data", 4000), ("load", 4000)])
@pytest.mark.parametrize("content", ["", '{"private-payload":"incomplete', '{"status":"plan"}'])
def test_truncation_stops_once_and_preserves_usage(mode, budget, content, tmp_path):
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"finish_reason": "length", "message": {"content": content}}],
        "usage": {"prompt_tokens": 120, "completion_tokens": budget},
    }
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    settings = AiSettings(max_repairs=5)
    store = UsageStore(tmp_path / "usage.db")
    tracked = TrackedProvider(provider, settings, mode, store=store)
    index = CatalogIndex(load_catalog(Path(__file__).resolve().parents[2] / "examples" / "store_catalog.json"))
    events = []
    if mode == "suite":
        planner = SuitePlanner(tracked, index, settings, progress=events.append)
    elif mode in {"load", "data"}:
        planner = WorkflowPlanner(tracked, index, settings, mode=mode, progress=events.append)
    else:
        planner = RequestPlanner(tracked, index, settings, progress=events.append)
    outcome = planner.plan("list products")
    assert not outcome.ok
    assert outcome.attempts == 1
    assert outcome.issues[0].code == "output_truncated"
    assert session.post.call_count == 1
    body = session.post.call_args.kwargs["json"]
    assert body["max_tokens"] == budget
    assert body["reasoning_effort"] is None
    assert outcome.usage[0].status == "Truncated response"
    assert outcome.usage[0].input_tokens == 120
    assert outcome.usage[0].output_tokens == budget
    assert outcome.output_tokens == budget
    row = store.rows()[0]
    assert row["input_tokens"] == 120
    assert row["output_tokens"] == budget
    assert row["status"] == "LlmResponseError"
    assert row["plan_ready"] == 0
    log = "\n".join(events)
    assert "finish reason: length" in log
    assert "Final text: " + ("non-empty" if content else "empty") in log
    assert "identical retry will not be attempted" in log
    assert "private-payload" not in log
    assert "offline-key" not in log
    assert not any("Fixing" in event for event in events)


def test_nontruncated_invalid_json_still_repairs_with_reported_usage():
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.side_effect = [
        {"choices": [{"finish_reason": "stop", "message": {"content": "invalid"}}],
         "usage": {"prompt_tokens": 20, "completion_tokens": 10}},
        {"choices": [{"finish_reason": "stop", "message": {
            "content": '{"endpoint_id":"f76aeb49ca37","status":"plan"}',
        }}], "usage": {"prompt_tokens": 30, "completion_tokens": 15}},
    ]
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    index = CatalogIndex(load_catalog(Path(__file__).resolve().parents[2] / "examples" / "store_catalog.json"))
    outcome = RequestPlanner(provider, index, AiSettings(max_repairs=1)).plan("list products")
    assert outcome.ok
    assert outcome.attempts == 2
    assert len(outcome.usage) == 2
    assert outcome.usage[0].status == "Invalid response"
    assert outcome.input_tokens == 50
    assert outcome.output_tokens == 25


def test_missing_failure_usage_is_not_fabricated():
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"finish_reason": "length", "message": {"content": ""}}],
    }
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    index = CatalogIndex(load_catalog(Path(__file__).resolve().parents[2] / "examples" / "store_catalog.json"))
    outcome = RequestPlanner(provider, index, AiSettings(max_repairs=5)).plan("list products")
    assert outcome.usage[0].input_tokens is None
    assert outcome.usage[0].output_tokens is None


def test_failure_diagnostics_never_contain_raw_response():
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"finish_reason": "stop", "message": {"content": "private-value"}}],
        "model": "private-model-value",
    }
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    with pytest.raises(LlmResponseError) as caught:
        provider.complete([ChatMessage("user", "private-prompt")], model="sarvam-105b",
                          response_schema={"type": "object"})
    diagnostic = caught.value.diagnostics
    assert diagnostic is not None
    assert not diagnostic.text
    assert diagnostic.json is None
    assert "private-" not in repr(diagnostic)
