"""The provider-neutral LLM interface every AI feature depends on."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class LlmResponse:
    text: str = ""
    json: Any = None
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    duration_ms: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class LlmError(RuntimeError):
    """A provider call failed; the message is safe to show to the user."""


class LlmConnectionError(LlmError): ...


class LlmAuthError(LlmError): ...


class LlmRateLimitError(LlmError): ...


class LlmTimeoutError(LlmError): ...


class LlmModelNotFoundError(LlmError): ...


class LlmResponseError(LlmError):
    """The provider answered, but not with what was asked for (e.g. invalid JSON)."""

    def __init__(self, message: str, *, diagnostics: LlmResponse | None = None) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics

    @property
    def truncated(self) -> bool:
        return self.diagnostics is not None and self.diagnostics.extra.get("finish_reason") in {
            "length", "max_tokens",
        }


@dataclass(frozen=True)
class ConnectionReport:
    ok: bool
    message: str
    models: tuple[str, ...] = ()


class LlmProvider(Protocol):
    name: str

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
    ) -> LlmResponse: ...

    def check_connection(self, model: str, timeout_seconds: float = 10.0) -> ConnectionReport: ...


def probe_hosted_connection(
    provider: LlmProvider, model: str, timeout_seconds: float = 15.0,
) -> ConnectionReport:
    sarvam = provider.name == "sarvam"
    try:
        provider.complete(
            [ChatMessage("user", "Reply with OK.")], model=model,
            # Other hosted models may require reasoning before their final text.
            max_output_tokens=128 if sarvam else 4096,
            timeout_seconds=max(timeout_seconds, 30.0 if sarvam else 60.0),
        )
    except LlmError as exc:
        return ConnectionReport(False, str(exc))
    return ConnectionReport(True, f"Connected to {provider.name}; '{model}' answered.")
