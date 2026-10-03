"""Per-plan usage reporting, separate from context capacity and billing."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CallUsage:
    purpose: str
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int
    model: str
    status: str = "Completed"

    @property
    def total_tokens(self) -> int | None:
        if self.input_tokens is None or self.output_tokens is None:
            return None
        return self.input_tokens + self.output_tokens


def token_text(value: int | None) -> str:
    return "Not reported" if value is None else f"{value:,}"


def usage_total(calls: list[CallUsage], field: str) -> int | None:
    values = [getattr(call, field) for call in calls]
    return None if any(value is None for value in values) else sum(values)
