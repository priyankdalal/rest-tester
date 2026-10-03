"""Ollama adapter using the native ``/api/chat`` endpoint.

The native API (rather than the OpenAI-compatible one) is used because it
supports a JSON Schema in ``format`` and the ``think`` switch that reasoning
models such as Qwen 3 understand.
"""

from __future__ import annotations

import json
import re
import uuid
from time import perf_counter
from typing import Any

import requests

from ..provider import (
    ChatMessage,
    ConnectionReport,
    LlmConnectionError,
    LlmError,
    LlmModelNotFoundError,
    LlmResponse,
    LlmResponseError,
    LlmTimeoutError,
    ToolCall,
    ToolSpec,
)

DEFAULT_ENDPOINT = "http://localhost:11434"
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_text(text: str) -> Any:
    """Parses model output as JSON, tolerating a surrounding Markdown fence."""
    candidate = text.strip()
    match = _FENCE.match(candidate)
    if match:
        candidate = match.group(1)
    return json.loads(candidate)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        think: bool = False,
        keep_alive: str = "30m",
        context_window: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self.think = think
        self.keep_alive = keep_alive
        self.context_window = context_window
        self._session = session or requests.Session()

    # -- requests ----------------------------------------------------------
    def build_body(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[ToolSpec] | None,
        response_schema: dict[str, Any] | None,
        temperature: float,
        max_output_tokens: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [self._message(item) for item in messages],
            "stream": False,
            "think": self.think,
            "keep_alive": self.keep_alive,
            "options": {"temperature": temperature, "num_predict": max_output_tokens},
        }
        if self.context_window:
            body["options"]["num_ctx"] = int(self.context_window)
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in tools
            ]
        if response_schema is not None:
            body["format"] = response_schema
        return body

    @staticmethod
    def _message(message: ChatMessage) -> dict[str, Any]:
        value: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            value["tool_calls"] = [
                {"function": {"name": call.name, "arguments": call.arguments}}
                for call in message.tool_calls
            ]
        if message.role == "tool" and message.tool_call_id:
            value["tool_name"] = message.tool_call_id
        return value

    def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        tools: list[ToolSpec] | None = None,
        response_schema: dict[str, Any] | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4000,
        timeout_seconds: float = 60.0,
    ) -> LlmResponse:
        body = self.build_body(
            messages,
            model=model,
            tools=tools,
            response_schema=response_schema,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        started = perf_counter()
        data = self._post("/api/chat", body, timeout_seconds, model)
        return self.parse_response(
            data,
            expect_json=response_schema is not None,
            duration_ms=int((perf_counter() - started) * 1000),
        )

    @staticmethod
    def parse_response(data: dict[str, Any], *, expect_json: bool, duration_ms: int = 0) -> LlmResponse:
        message = data.get("message") or {}
        text = str(message.get("content") or "")
        calls = tuple(
            ToolCall(
                id=str(uuid.uuid4()),
                name=str((item.get("function") or {}).get("name", "")),
                arguments=_arguments((item.get("function") or {}).get("arguments")),
            )
            for item in message.get("tool_calls") or ()
        )
        parsed: Any = None
        if expect_json and not calls:
            if not text.strip():
                raise LlmResponseError("The model returned an empty answer.")
            try:
                parsed = parse_json_text(text)
            except json.JSONDecodeError as exc:
                raise LlmResponseError(f"The model did not return valid JSON: {exc}") from exc
        return LlmResponse(
            text=text,
            json=parsed,
            tool_calls=calls,
            input_tokens=int(data.get("prompt_eval_count") or 0),
            output_tokens=int(data.get("eval_count") or 0),
            model=str(data.get("model") or ""),
            duration_ms=duration_ms,
            extra={"done_reason": data.get("done_reason", ""),
                   "input_tokens_reported": data.get("prompt_eval_count") is not None,
                   "output_tokens_reported": data.get("eval_count") is not None},
        )

    # -- diagnostics ---------------------------------------------------------
    def list_models(self, timeout_seconds: float = 10.0) -> tuple[str, ...]:
        try:
            response = self._session.get(f"{self.endpoint}/api/tags", timeout=timeout_seconds)
            response.raise_for_status()
        except requests.Timeout as exc:
            raise LlmTimeoutError(f"Ollama at {self.endpoint} did not answer in time.") from exc
        except requests.RequestException as exc:
            raise LlmConnectionError(
                f"Cannot reach Ollama at {self.endpoint}. Is it running? ({exc.__class__.__name__})"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise LlmResponseError("Ollama returned a non-JSON model list.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise LlmResponseError("Ollama returned an invalid model list.")
        models = data["models"]
        if any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
               or not item["name"].strip() for item in models):
            raise LlmResponseError("Ollama returned an invalid model entry.")
        return tuple(item["name"] for item in models)

    def check_connection(self, model: str, timeout_seconds: float = 10.0) -> ConnectionReport:
        try:
            models = self.list_models(timeout_seconds)
        except LlmError as exc:
            return ConnectionReport(False, str(exc))
        if model not in models and f"{model}:latest" not in models:
            available = ", ".join(models) or "none"
            return ConnectionReport(
                False, f"Connected, but model '{model}' is not installed. Available: {available}", models
            )
        return ConnectionReport(True, f"Connected to Ollama; model '{model}' is available.", models)

    def _post(self, path: str, body: dict[str, Any], timeout: float, model: str) -> dict[str, Any]:
        try:
            response = self._session.post(f"{self.endpoint}{path}", json=body, timeout=timeout)
        except requests.Timeout as exc:
            raise LlmTimeoutError(
                f"Ollama did not answer within {timeout:.0f} s. The first call loads the model and can be slow;"
                " try again or raise the timeout in AI settings."
            ) from exc
        except requests.RequestException as exc:
            raise LlmConnectionError(
                f"Cannot reach Ollama at {self.endpoint}. Is it running? ({exc.__class__.__name__})"
            ) from exc
        if response.status_code == 404:
            raise LlmModelNotFoundError(f"Ollama has no model '{model}'. Run: ollama pull {model}")
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise LlmError(f"Ollama returned HTTP {response.status_code}: {detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise LlmResponseError("Ollama returned a non-JSON response.") from exc


def _arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _error_detail(response: requests.Response) -> str:
    try:
        return str(response.json().get("error") or response.text)[:300]
    except ValueError:
        return response.text[:300]
