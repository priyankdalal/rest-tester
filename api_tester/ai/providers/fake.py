"""Scripted provider for tests: replays responses in order and records requests."""

from __future__ import annotations

from typing import Any

from ..provider import ConnectionReport, LlmResponse


class FakeProvider:
    name = "fake"

    def __init__(self, script: list[LlmResponse | Exception] | None = None) -> None:
        self._script = list(script or [])
        self.requests: list[dict[str, Any]] = []

    def complete(self, messages, **kwargs) -> LlmResponse:
        self.requests.append({"messages": list(messages), **kwargs})
        if not self._script:
            raise AssertionError("FakeProvider ran out of scripted responses")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def check_connection(self, model: str, timeout_seconds: float = 10.0) -> ConnectionReport:
        return ConnectionReport(True, f"Fake provider; model '{model}'.", (model,))
