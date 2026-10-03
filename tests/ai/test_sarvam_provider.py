"""Offline Sarvam wire-contract checks."""

from unittest.mock import Mock

import pytest

from api_tester.ai.config import AiConnection, AiSettings, build_provider
from api_tester.ai.provider import ChatMessage, LlmAuthError, LlmResponseError
from api_tester.ai.providers.hosted import HostedProvider


def test_sarvam_chat_uses_subscription_key_and_native_token_field():
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "model": "sarvam-105b",
        "choices": [{"message": {"content": '{"status":"plan"}'}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4},
    }
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    result = provider.complete(
        [ChatMessage("user", "Return a plan")], model="sarvam-105b",
        response_schema={"type": "object"}, max_output_tokens=4000,
    )
    args, kwargs = session.post.call_args
    assert args == ("https://api.sarvam.ai/v1/chat/completions",)
    assert kwargs["headers"]["api-subscription-key"] == "offline-key"
    assert "Authorization" not in kwargs["headers"]
    assert kwargs["json"]["max_tokens"] == 4000
    assert "max_completion_tokens" not in kwargs["json"]
    assert kwargs["json"]["response_format"] == {"type": "json_object"}
    assert not kwargs["allow_redirects"]
    assert result.json == {"status": "plan"}
    assert (result.input_tokens, result.output_tokens) == (12, 4)
    assert kwargs["json"]["reasoning_effort"] is None


def test_sarvam_profile_roundtrip_and_hosted_consent():
    connection = AiConnection.create("Sarvam", "sarvam")
    assert connection.planner_model == "sarvam-105b"
    settings = AiSettings(connections=[connection], active_connection_id=connection.id)
    restored = AiSettings.from_dict(settings.to_dict())
    assert restored.provider == "sarvam"
    assert restored.endpoint == "https://api.sarvam.ai/v1"
    with pytest.raises(LlmAuthError, match="Enable hosted AI"):
        build_provider(restored, api_key="offline-key")
    restored.allow_hosted = True
    assert build_provider(restored, api_key="offline-key").name == "sarvam"


def _reasoning_probe_session():
    session = Mock()
    session.post.return_value.status_code = 200

    def post(_url, **kwargs):
        body = kwargs["json"]
        disabled = "reasoning_effort" in body and body["reasoning_effort"] is None
        content = "OK" if disabled and body["max_tokens"] >= 128 else ""
        session.post.return_value.json.return_value = {
            "choices": [{"finish_reason": "stop" if content else "length",
                         "message": {"content": content}}],
            "usage": {"prompt_tokens": 8, "completion_tokens": 1},
        }
        return session.post.return_value

    session.post.side_effect = post
    return session


def test_sarvam_disables_reasoning_for_probes_and_plans_but_not_plain_chat():
    session = _reasoning_probe_session()
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    with pytest.raises(LlmResponseError, match="token limit reached"):
        provider.complete([ChatMessage("user", "Reply with OK.")], model="sarvam-105b",
                          max_output_tokens=32)
    report = provider.check_connection("sarvam-105b")
    assert report.ok
    kwargs = session.post.call_args.kwargs
    assert kwargs["json"]["reasoning_effort"] is None
    assert kwargs["json"]["max_tokens"] == 128
    assert kwargs["timeout"] == 30.0
    session.post.side_effect = None
    session.post.return_value.json.return_value = {
        "choices": [{"message": {"content": '{"status":"plan"}'}}],
    }
    provider.complete([ChatMessage("user", "Return a plan")], model="sarvam-105b",
                      response_schema={"type": "object"})
    assert session.post.call_args.kwargs["json"]["reasoning_effort"] is None
    provider.complete([ChatMessage("user", "Hello")], model="sarvam-105b")
    assert "reasoning_effort" not in session.post.call_args.kwargs["json"]


def test_sarvam_controller_probe_tracks_usage_and_reports_available():
    from unittest.mock import patch

    from api_tester.ai.controller import connection_test_task
    from api_tester.ai.usage_store import TrackedProvider

    session = _reasoning_probe_session()
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    connection = AiConnection.create("Sarvam", "sarvam")
    settings = AiSettings(connections=[connection], active_connection_id=connection.id).for_connection(connection.id)
    store = Mock()

    def tracked(provider, settings, kind):
        return TrackedProvider(provider, settings, kind, store=store)

    with patch("api_tester.ai.controller.build_provider", return_value=provider), \
            patch("api_tester.ai.controller.TrackedProvider", side_effect=tracked):
        task = connection_test_task(settings)
        report = task._worker._job(task._worker)
    assert report.ok
    assert store.record.call_count == 1
    assert store.record.call_args.args[-1] == "Completed"
    assert session.post.call_args.kwargs["json"]["reasoning_effort"] is None


@pytest.mark.parametrize("finish, detail", [
    ("length", "token limit reached"),
    ("content_filter", "filtered"),
    ("stop", "stopped without final text"),
    ("private-server-value", "No recognized finish reason"),
])
def test_sarvam_empty_answer_is_explicit_failure(finish, detail):
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"finish_reason": finish, "message": {"content": None}}],
    }
    provider = HostedProvider("sarvam", "https://api.sarvam.ai/v1", "offline-key", session=session)
    report = provider.check_connection("sarvam-105b")
    assert not report.ok
    assert detail in report.message
    assert "private-server-value" not in report.message


@pytest.mark.parametrize("name", ["openai", "azure", "claude"])
def test_other_hosted_connection_checks_allow_reasoning_and_omit_sampling(name):
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"message": {"content": "OK"}}],
        "content": [{"type": "text", "text": "OK"}],
    }
    provider = HostedProvider(name, "https://example.test/v1", "offline-key", session=session)
    assert provider.check_connection("test-model", timeout_seconds=15.0).ok
    kwargs = session.post.call_args.kwargs
    assert kwargs["timeout"] == 60.0
    assert "reasoning_effort" not in kwargs["json"]
    assert "temperature" not in kwargs["json"]
    assert "thinking" not in kwargs["json"]
    field = "max_tokens" if name == "claude" else "max_completion_tokens"
    assert kwargs["json"][field] == 4096
