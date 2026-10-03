"""Offline provider-specific connection checks, including toolbar availability."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from api_tester.ai.config import AiConnection, AiSettings
from api_tester.ai.controller import connection_test_task
from api_tester.ai.provider import ConnectionReport
from api_tester.ai.providers.hosted import HostedProvider
from api_tester.ai.providers.ollama import OllamaProvider
from api_tester.ai.usage_store import TrackedProvider


@pytest.mark.parametrize("name,model", [
    ("openai", "o3"),
    ("openai", "gpt-5.4"),
    ("azure", "arbitrary-deployment-name"),
    ("claude", "claude-opus-5-5"),
])
def test_hosted_probe_supports_default_reasoning_without_sampling(name, model):
    session = Mock()
    response = session.post.return_value
    response.status_code = 200

    def post(_url, **kwargs):
        body = kwargs["json"]
        assert "temperature" not in body
        assert "reasoning_effort" not in body
        assert "thinking" not in body
        field = "max_tokens" if name == "claude" else "max_completion_tokens"
        assert body[field] == 4096
        assert kwargs["timeout"] == 60.0
        assert not kwargs["allow_redirects"]
        if name == "claude":
            response.json.return_value = {
                "stop_reason": "end_turn",
                "content": [{"type": "thinking", "thinking": "not displayed"},
                            {"type": "text", "text": "OK"}],
                "usage": {"input_tokens": 8, "output_tokens": 100},
            }
        else:
            response.json.return_value = {
                "choices": [{"finish_reason": "stop", "message": {"content": "OK"}}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 100},
            }
        return response

    session.post.side_effect = post
    provider = HostedProvider(name, "https://example.test", "offline-key", session=session)
    connection = AiConnection.create("Test", name)
    connection.planner_model = model
    settings = AiSettings(
        connections=[connection], active_connection_id=connection.id,
    ).for_connection(connection.id)
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
    args, kwargs = session.post.call_args
    if name == "azure":
        assert "/openai/deployments/arbitrary-deployment-name/chat/completions" in args[0]
        assert "api-version=" in args[0]
        assert "model" not in kwargs["json"]
        assert kwargs["headers"]["api-key"] == "offline-key"
    elif name == "claude":
        assert args[0].endswith("/v1/messages")
        assert kwargs["headers"]["anthropic-version"] == "2023-06-01"
        assert kwargs["headers"]["x-api-key"] == "offline-key"
    else:
        assert args[0].endswith("/chat/completions")
        assert kwargs["headers"]["Authorization"] == "Bearer offline-key"

    session.post.side_effect = None
    provider.complete([], model=model)
    assert session.post.call_args.kwargs["json"]["temperature"] == 0.0


@pytest.mark.parametrize("name,finish,detail", [
    ("openai", "length", "token limit reached"),
    ("azure", "content_filter", "filtered"),
    ("claude", "max_tokens", "token limit reached"),
    ("claude", "end_turn", "ended its turn"),
    ("claude", "refusal", "declined"),
    ("claude", "pause_turn", "paused"),
])
def test_hosted_empty_final_text_remains_failure(name, finish, detail):
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"finish_reason": finish, "message": {"content": " \n"}}],
        "stop_reason": finish,
        "content": [{"type": "thinking", "thinking": "private-text"}],
    }
    provider = HostedProvider(name, "https://example.test", "offline-key", session=session)
    report = provider.check_connection("model")
    assert not report.ok
    assert detail in report.message
    assert "private-text" not in report.message


@pytest.mark.parametrize("name", ["openai", "azure", "claude"])
@pytest.mark.parametrize("status,detail", [(401, "authentication"), (429, "quota"), (404, "not found")])
def test_hosted_probe_does_not_hide_provider_failures(name, status, detail):
    session = Mock()
    session.post.return_value.status_code = status
    provider = HostedProvider(name, "https://example.test", "offline-key", session=session)
    report = provider.check_connection("model")
    assert not report.ok
    assert detail in report.message
    assert session.post.call_count == 1


@pytest.mark.parametrize("name", ["openai", "azure", "claude", "sarvam"])
def test_hosted_probe_preserves_longer_requested_timeout(name):
    session = Mock()
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {
        "choices": [{"message": {"content": "OK"}}],
        "content": [{"type": "text", "text": "OK"}],
    }
    provider = HostedProvider(name, "https://example.test", "offline-key", session=session)
    assert provider.check_connection("model", timeout_seconds=90.0).ok
    assert session.post.call_args.kwargs["timeout"] == 90.0


def test_ollama_uses_model_list_without_generation():
    session = Mock()
    session.get.return_value.json.return_value = {"models": [{"name": "qwen3:latest"}]}
    provider = OllamaProvider(session=session)
    assert provider.check_connection("qwen3").ok
    session.get.assert_called_once_with("http://localhost:11434/api/tags", timeout=10.0)
    session.post.assert_not_called()
    report = provider.check_connection("missing")
    assert not report.ok
    assert report.models == ("qwen3:latest",)


@pytest.mark.parametrize("body", [[], {}, {"models": None}, {"models": [{}]}, {"models": ["qwen3"]}])
def test_ollama_invalid_model_list_is_safe_failure(body):
    session = Mock()
    session.get.return_value.json.return_value = body
    report = OllamaProvider(session=session).check_connection("qwen3")
    assert not report.ok
    assert "invalid model" in report.message


def test_ollama_non_json_model_list_is_safe_failure():
    session = Mock()
    session.get.return_value.json.side_effect = ValueError("private-server-body")
    report = OllamaProvider(session=session).check_connection("qwen3")
    assert not report.ok
    assert "non-JSON" in report.message
    assert "private-server-body" not in report.message


@pytest.mark.parametrize("name", ["openai", "azure", "claude", "sarvam", "ollama"])
def test_main_connection_report_controls_toolbar_availability(name):
    from api_tester.main import MainWindow
    from api_tester.widgets import HeaderAiButton

    connection = AiConnection.create("Test", name)
    settings = AiSettings(
        connections=[connection], active_connection_id=connection.id,
    ).for_connection(connection.id)
    button = HeaderAiButton()
    host = SimpleNamespace(
        _closing=False, _ai_connection_token=0, _ai_connection_task=None, ask_ai_button=button,
    )
    task = connection_test_task(settings)
    task.start = lambda: task.succeeded.emit(ConnectionReport(True, "Connected"))
    with patch("api_tester.main.AI_SETTINGS_PATH", Path(__file__)), \
            patch("api_tester.ai.config.load_ai_settings", return_value=settings), \
            patch("api_tester.ai.controller.connection_test_task", return_value=task):
        MainWindow._check_ai_connection(host)
    assert button.available
    assert button._effect.opacity() == 1.0
    task.succeeded.emit(ConnectionReport(False, "Failed"))
    assert not button.available
    assert button._effect.opacity() == 0.4
