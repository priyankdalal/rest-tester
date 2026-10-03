"""Hosted providers over HTTPS, sharing the existing planner and validators."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any
from urllib.parse import quote, urlparse

import requests

from ..provider import (
    ChatMessage, ConnectionReport, LlmAuthError, LlmConnectionError, LlmError,
    LlmModelNotFoundError, LlmRateLimitError, LlmResponse, LlmResponseError,
    LlmTimeoutError, ToolSpec, probe_hosted_connection,
)
from .ollama import parse_json_text


class HostedProvider:
    def __init__(self, name: str, endpoint: str, api_key: str, *,
                 api_version: str = "2024-10-21", session: requests.Session | None = None,
                 connection_check: bool = False) -> None:
        if name not in {"azure", "openai", "claude", "sarvam"}:
            raise ValueError(f"Unsupported hosted provider: {name}")
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise LlmConnectionError("Hosted AI requires an HTTPS base URL without credentials, query, or fragment.")
        if not api_key.strip():
            raise LlmAuthError("An API key is required for the selected AI provider.")
        self.name = name
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.api_version = api_version
        self._connection_check = connection_check
        self._session = session or requests.Session()

    def complete(self, messages: list[ChatMessage], *, model: str,
                 tools: list[ToolSpec] | None = None, response_schema: dict[str, Any] | None = None,
                 temperature: float = 0.0, max_output_tokens: int = 4000,
                 timeout_seconds: float = 60.0) -> LlmResponse:
        if tools:
            raise LlmResponseError("Hosted planner calls support a response schema, not arbitrary tools.")
        if not model.strip():
            raise LlmModelNotFoundError("Enter a model ID or Azure deployment name in AI Settings.")
        started = perf_counter()
        finish_reason = ""
        if self.name == "claude":
            system = "\n\n".join(item.content for item in messages if item.role == "system")
            body: dict[str, Any] = {
                "model": model, "system": system,
                "messages": [{"role": item.role, "content": item.content} for item in messages if item.role != "system"],
                "max_tokens": max_output_tokens,
            }
            if not self._connection_check:
                body["temperature"] = temperature
            if response_schema is not None:
                body["tools"] = [{"name": "submit_plan", "description": "Return the requested validated plan.",
                                  "input_schema": response_schema}]
                body["tool_choice"] = {"type": "tool", "name": "submit_plan"}
            data = self._post(f"{self.endpoint}/v1/messages", body, timeout_seconds)
            finish_reason = str(data.get("stop_reason") or "")
            blocks = data.get("content") or []
            text = "".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")
            parsed = next((block.get("input") for block in blocks
                           if block.get("type") == "tool_use" and block.get("name") == "submit_plan"), None)
            usage = data.get("usage") or {}
            in_tokens, out_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            in_tokens += int(usage.get("cache_creation_input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
        else:
            wire = [{"role": item.role, "content": item.content} for item in messages]
            token_field = "max_tokens" if self.name == "sarvam" else "max_completion_tokens"
            body = {"messages": wire, token_field: max_output_tokens}
            if not self._connection_check:
                body["temperature"] = temperature
            if self.name == "sarvam" and (self._connection_check or response_schema is not None):
                body["reasoning_effort"] = None
            if response_schema is not None:
                # JSON mode accepts the catalog's optional fields and dynamic maps.
                # The existing validators enforce the full schema after generation.
                wire.insert(0, {"role": "system", "content": "Return only a JSON object matching this schema:\n"
                               + json.dumps(response_schema)})
                body["response_format"] = {"type": "json_object"}
            if self.name == "azure":
                url = (f"{self.endpoint}/openai/deployments/{quote(model, safe='')}/chat/completions"
                       f"?api-version={quote(self.api_version, safe='')}")
            else:
                body["model"] = model
                url = f"{self.endpoint}/chat/completions"
            data = self._post(url, body, timeout_seconds)
            choices = data.get("choices") or []
            if not choices:
                raise LlmResponseError("The provider returned no completion.")
            message = choices[0].get("message") or {}
            finish_reason = str(choices[0].get("finish_reason") or "")
            if message.get("refusal"):
                raise LlmResponseError("The provider declined this request.")
            text = str(message.get("content") or "")
            parsed = None
            usage = data.get("usage") or {}
            in_tokens, out_tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        diagnostics = LlmResponse(
            input_tokens=int(in_tokens or 0), output_tokens=int(out_tokens or 0),
            model=model, duration_ms=int((perf_counter() - started) * 1000),
            extra={
                "finish_reason": finish_reason if finish_reason in {
                    "length", "max_tokens", "stop", "end_turn", "content_filter", "refusal",
                    "tool_calls", "tool_use", "function_call", "pause_turn", "stop_sequence",
                    "model_context_window_exceeded",
                } else "not reported",
                "final_text_state": "non-empty" if text.strip() else "empty",
                "input_tokens_reported": usage.get(
                    "input_tokens" if self.name == "claude" else "prompt_tokens",
                ) is not None,
                "output_tokens_reported": usage.get(
                    "output_tokens" if self.name == "claude" else "completion_tokens",
                ) is not None,
            },
        )
        if response_schema is not None:
            if finish_reason in {"length", "max_tokens"}:
                raise LlmResponseError(
                    "The provider did not return a complete plan. Output token limit reached. "
                    "Request a smaller plan or choose another model; an identical retry will not be attempted.",
                    diagnostics=diagnostics,
                )
            if parsed is None:
                try:
                    parsed = parse_json_text(text)
                except (ValueError, TypeError) as exc:
                    raise LlmResponseError(
                        "The provider did not return valid plan JSON.", diagnostics=diagnostics,
                    ) from exc
            if not isinstance(parsed, dict):
                raise LlmResponseError(
                    "The provider returned a plan that is not a JSON object.", diagnostics=diagnostics,
                )
            if not text:
                text = json.dumps(parsed)
        elif not text.strip():
            reasons = {
                "length": " Output token limit reached before a final answer.",
                "max_tokens": " Output token limit reached before a final answer.",
                "content_filter": " The provider filtered the answer.",
                "tool_calls": " The provider returned a tool call instead of text.",
                "function_call": " The provider returned a function call instead of text.",
                "stop": " The provider stopped without final text.",
                "end_turn": " The provider ended its turn without final text.",
                "refusal": " The provider declined this request.",
                "tool_use": " The provider returned a tool call instead of text.",
                "pause_turn": " The provider paused its turn without final text.",
                "stop_sequence": " A stop sequence ended generation before final text.",
                "model_context_window_exceeded": " The model context window was exceeded.",
            }
            raise LlmResponseError(
                "The provider returned an empty answer."
                + reasons.get(finish_reason, " No recognized finish reason was reported."),
                diagnostics=diagnostics,
            )
        return LlmResponse(text=text, json=parsed, input_tokens=int(in_tokens), output_tokens=int(out_tokens),
                           model=str(data.get("model") or model),
                           duration_ms=int((perf_counter() - started) * 1000),
                           extra={
                               "finish_reason": finish_reason,
                               "input_tokens_reported": usage.get("input_tokens" if self.name == "claude" else "prompt_tokens") is not None,
                               "output_tokens_reported": usage.get("output_tokens" if self.name == "claude" else "completion_tokens") is not None,
                           })

    def _post(self, url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.name == "azure":
            headers["api-key"] = self.api_key
        elif self.name == "claude":
            headers.update({"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        elif self.name == "sarvam":
            headers["api-subscription-key"] = self.api_key
        else:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self._session.post(url, json=body, headers=headers, timeout=timeout, allow_redirects=False)
        except requests.Timeout as exc:
            raise LlmTimeoutError(f"{self.name} did not answer within {timeout:.0f} seconds.") from exc
        except requests.RequestException as exc:
            raise LlmConnectionError(f"Cannot reach {self.name}. Check the endpoint and network.") from exc
        code = response.status_code
        if code in {401, 403}:
            raise LlmAuthError("AI authentication failed. Check the API key and resource permissions.")
        if code == 429:
            raise LlmRateLimitError("AI rate limit or quota exceeded. Try later or check billing.")
        if code == 404:
            raise LlmModelNotFoundError("AI endpoint or model/deployment not found. Check AI Settings.")
        if code >= 300:
            # Never expose server bodies, which may echo keys or prompt data.
            raise LlmError(f"{self.name} returned HTTP {code}. Check model compatibility and connection settings.")
        try:
            data = response.json()
        except ValueError as exc:
            raise LlmResponseError("The AI provider returned a non-JSON response.") from exc
        if not isinstance(data, dict):
            raise LlmResponseError("The AI provider returned an invalid response object.")
        return data

    def for_connection_check(self) -> HostedProvider:
        return HostedProvider(
            self.name, self.endpoint, self.api_key, api_version=self.api_version,
            session=self._session, connection_check=True,
        )

    def check_connection(self, model: str, timeout_seconds: float = 10.0) -> ConnectionReport:
        return probe_hosted_connection(self.for_connection_check(), model, timeout_seconds)
