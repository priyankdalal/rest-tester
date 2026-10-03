"""Turns a typed prompt into a validated :class:`RequestPlan`.

Flow: search the catalog locally -> one structured model call with the
candidates -> validate -> up to ``max_repairs`` repair rounds that quote the
problems and the valid options. The model never sees secrets: every outgoing
message is redacted.
"""

from __future__ import annotations

import json
from time import perf_counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .catalog_index import CatalogIndex, endpoint_summary
from ..catalog import Endpoint
from .config import AiSettings
from .prompts import (
    CONFIRM_WITH_CARD_INSTRUCTION,
    INVALID_JSON_INSTRUCTION,
    MISSING_PATH_INSTRUCTION,
    REPAIR_INSTRUCTION,
    REQUEST_PROMPT_VERSION,
    REQUEST_SYSTEM,
    SUITE_PROMPT_VERSION,
    SUITE_REPAIR_INSTRUCTION,
    SUITE_SYSTEM,
    request_user_message,
    suite_user_message,
)
from .provider import ChatMessage, LlmError, LlmProvider, LlmResponseError
from .redaction import redact_messages, redact_text
from .request_plan import RequestPlan, request_plan_schema
from .suite_plan import SuitePlan, suite_plan_schema, validate_suite_plan
from .validation import PlanIssue, ValidationResult, validate_request_plan
from .usage import CallUsage


class PlanCancelled(Exception):
    """The user cancelled while the plan was being prepared."""


@dataclass
class PlanOutcome:
    plan: RequestPlan
    issues: tuple[PlanIssue, ...] = ()
    candidates: tuple[str, ...] = ()
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    prompt_version: str = REQUEST_PROMPT_VERSION
    model: str = ""
    transcript: list[ChatMessage] = field(default_factory=list)
    usage: list[CallUsage] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


class RequestPlanner:
    def __init__(
        self,
        provider: LlmProvider,
        index: CatalogIndex,
        settings: AiSettings,
        *,
        secrets: Iterable[str] = (),
        progress: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        self.provider = provider
        self.index = index
        self.settings = settings
        self.secrets = tuple(item for item in secrets if item)
        self._progress = progress or (lambda _text: None)
        self._is_cancelled = is_cancelled or (lambda: False)

    def _check_cancelled(self) -> None:
        if self._is_cancelled():
            raise PlanCancelled()

    def _record_usage(self, outcome, attempt: int, response, elapsed_ms: int,
                      status: str = "Completed") -> None:
        outcome.usage.append(CallUsage(
            purpose="Initial plan" if attempt == 0 else f"Repair / refinement {attempt}",
            input_tokens=response.input_tokens if response.extra.get("input_tokens_reported", True) else None,
            output_tokens=response.output_tokens if response.extra.get("output_tokens_reported", True) else None,
            duration_ms=elapsed_ms,
            model=response.model or self.settings.planner_model,
            status=status,
        ))
        usage = outcome.usage[-1]
        finish = str(response.extra.get("finish_reason") or response.extra.get("done_reason") or "not reported")
        self._progress(
            f"Call {attempt + 1} returned in {elapsed_ms / 1000:.1f}s; "
            f"input tokens: {usage.input_tokens if usage.input_tokens is not None else 'not reported'}; "
            f"output tokens: {usage.output_tokens if usage.output_tokens is not None else 'not reported'}; "
            f"finish reason: {redact_text(finish[:80], self.secrets)}"
        )
        if "final_text_state" in response.extra:
            self._progress(f"Final text: {response.extra['final_text_state']}; {status}")

    def _record_response_failure(self, outcome, attempt: int, started: float,
                                 exc: LlmResponseError) -> None:
        self._response_failure(attempt, started, exc)
        elapsed_ms = int((perf_counter() - started) * 1000)
        if exc.diagnostics is not None:
            response = exc.diagnostics
            self._record_usage(outcome, attempt, response, elapsed_ms,
                               "Truncated response" if exc.truncated else "Invalid response")
            outcome.input_tokens += response.input_tokens
            outcome.output_tokens += response.output_tokens
            outcome.duration_ms += response.duration_ms
            outcome.model = response.model or self.settings.planner_model
        else:
            outcome.usage.append(CallUsage(
                "Initial plan" if attempt == 0 else f"Repair / refinement {attempt}",
                None, None, elapsed_ms, self.settings.planner_model,
                "Invalid response; usage unavailable",
            ))
        outcome.issues = (PlanIssue(
            "$", "output_truncated" if exc.truncated else "invalid_json", str(exc),
        ),)
        if exc.truncated:
            self._progress("Stopped: output token limit reached. An identical retry will not be attempted; "
                           "request a smaller plan or choose another model.")

    def _validation_activity(self, issues) -> None:
        if issues:
            # Messages can contain user payload values; log location/code, not raw values.
            self._progress("Validation issues: " + "; ".join(
                redact_text(f"{issue.path}: {issue.code}", self.secrets) for issue in issues
            )[:2000])
        else:
            self._progress("Catalog validation passed")

    def _response_failure(self, attempt: int, started: float, exc: Exception) -> None:
        self._progress(
            f"Call {attempt + 1} failed after {perf_counter() - started:.1f}s: "
            f"{redact_text(str(exc), self.secrets)}"
        )

    def system_prompt(self) -> str:
        return REQUEST_SYSTEM

    def response_schema(self, candidate_ids: list[str]) -> dict[str, Any]:
        return request_plan_schema(candidate_ids)

    def parse_plan(self, value: Any) -> RequestPlan:
        return RequestPlan.from_json(value)

    def validate_plan(self, plan: RequestPlan) -> ValidationResult:
        return validate_request_plan(plan, self.index)

    def output_token_limit(self) -> int:
        return 2000

    def plan(self, prompt: str) -> PlanOutcome:
        prompt = redact_text(prompt.strip(), self.secrets)
        if not prompt:
            raise ValueError("Type what you want to test.")
        self._progress("Searching the catalog")
        hits = self.index.search(prompt, limit=self.settings.candidate_count)
        if not hits:
            return PlanOutcome(
                RequestPlan(
                    status="unsupported",
                    title="No matching endpoint",
                    summary="No catalog endpoint matches the words in this request. Name the resource, e.g. 'brands'.",
                )
            )
        candidate_ids = [hit.endpoint.id for hit in hits]
        self._progress(f"Retrieved {len(candidate_ids)} endpoint candidates; "
                       f"showing {min(len(hits), self.settings.detailed_candidates)} detailed cards")
        cards = [self.index.card(hit.endpoint) for hit in hits[: self.settings.detailed_candidates]]
        messages = [
            ChatMessage("system", self.system_prompt()),
            ChatMessage("user", request_user_message(prompt, [hit.summary() for hit in hits], cards)),
        ]
        schema = self.response_schema(candidate_ids)
        outcome = PlanOutcome(RequestPlan(), candidates=tuple(candidate_ids))
        detailed = {hit.endpoint.id for hit in hits[: self.settings.detailed_candidates]}
        asked_for_path = False

        for attempt in range(self.settings.max_repairs + 1):
            self._check_cancelled()
            self._progress("Designing the request" if attempt == 0 else f"Fixing the plan (round {attempt})")
            outcome.attempts = attempt + 1
            started = perf_counter()
            try:
                response = self.provider.complete(
                    redact_messages(messages, self.secrets),
                    model=self.settings.planner_model,
                    response_schema=schema,
                    temperature=0.0,
                    max_output_tokens=self.output_token_limit(),
                    timeout_seconds=float(self.settings.timeout_seconds),
                )
            except LlmResponseError as exc:
                self._record_response_failure(outcome, attempt, started, exc)
                if exc.truncated:
                    break
                messages.append(ChatMessage("user", INVALID_JSON_INSTRUCTION.format(error=exc)))
                continue
            except LlmError as exc:
                self._response_failure(attempt, started, exc)
                raise
            outcome.input_tokens += response.input_tokens
            self._record_usage(outcome, attempt, response, int((perf_counter() - started) * 1000))
            outcome.output_tokens += response.output_tokens
            outcome.duration_ms += response.duration_ms
            outcome.model = response.model or self.settings.planner_model
            self._check_cancelled()
            self._progress("Validating against the catalog")
            try:
                draft = self.parse_plan(response.json)
            except ValueError as exc:
                self._progress("Plan structure rejected: " + redact_text(str(exc), self.secrets)[:1000])
                outcome.issues = (PlanIssue("$", "invalid_plan", str(exc)),)
                messages.append(ChatMessage("assistant", response.text))
                messages.append(ChatMessage("user", INVALID_JSON_INSTRUCTION.format(error=exc)))
                continue
            result = self.validate_plan(draft)
            outcome.plan = result.plan
            outcome.issues = result.issues
            self._validation_activity(result.issues)
            endpoint = self.index.get(draft.endpoint_id)
            if endpoint is not None:
                self._progress(f"Selected endpoint: {endpoint.id} ({endpoint.method} {endpoint.path})")
            # The model picked a candidate it only saw as a one-line summary:
            # show it the card so filters, sort and payload use real fields.
            needs_card = endpoint is not None and endpoint.id not in detailed
            missing = _missing_path_parameters(result.plan, endpoint) if not asked_for_path else []
            if result.ok and not needs_card and not missing:
                break
            if attempt == self.settings.max_repairs:
                break
            messages.append(ChatMessage("assistant", json.dumps(response.json)))
            if result.issues:
                follow_up = REPAIR_INSTRUCTION.format(
                    issues="\n".join(f"- {item.as_text()}" for item in result.issues)
                )
            elif needs_card:
                follow_up = CONFIRM_WITH_CARD_INSTRUCTION
            else:
                follow_up = ""
            if missing:
                asked_for_path = True
                follow_up += "\n" + MISSING_PATH_INSTRUCTION.format(names=", ".join(missing))
            if needs_card:
                self._progress("Refinement required: selected endpoint needs its detailed catalog card")
                follow_up += "\nCard for the endpoint you chose:\n" + self.index.card(endpoint)
                detailed.add(endpoint.id)
            messages.append(ChatMessage("user", follow_up.strip()))
            if missing:
                self._progress("Refinement required: missing path parameters " + ", ".join(missing))
        outcome.transcript = messages
        return outcome


def _missing_path_parameters(plan: RequestPlan, endpoint: Endpoint | None) -> list[str]:
    if plan.status != "plan" or endpoint is None:
        return []
    given = {name for name, value in plan.parameters if value}
    return [item.name for item in endpoint.parameters if item.source == "path" and item.name not in given]


@dataclass
class SuiteOutcome:
    plan: SuitePlan
    issues: tuple[PlanIssue, ...] = ()
    candidates: tuple[str, ...] = ()
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    prompt_version: str = SUITE_PROMPT_VERSION
    model: str = ""
    transcript: list[ChatMessage] = field(default_factory=list)
    usage: list[CallUsage] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


#: Largest number of endpoints offered to the model for one suite.
SUITE_CANDIDATE_LIMIT = 24
#: Largest number of full endpoint cards in the first suite prompt.
SUITE_CARD_LIMIT = 10


class SuitePlanner(RequestPlanner):
    """Turns a prompt into a validated multi-case :class:`SuitePlan`."""

    def candidates(self, prompt: str) -> tuple[list[Endpoint], list[Endpoint]]:
        """(offered endpoints, endpoints shown as full cards).

        A suite usually exercises one resource end to end, so every endpoint on
        the best-matching controllers is offered, not just the top text hits.
        """
        hits = self.index.search(prompt, limit=self.settings.candidate_count + 4)
        if not hits:
            return [], []
        primary = self.index.siblings(hits[0].endpoint)
        first = (hits[0].endpoint.service, hits[0].endpoint.controller)
        runner_up = next((hit.endpoint for hit in hits if (hit.endpoint.service, hit.endpoint.controller) != first), None)
        secondary = self.index.siblings(runner_up) if runner_up is not None else []
        offered: list[Endpoint] = []
        for endpoint in [*primary, *(hit.endpoint for hit in hits), *secondary]:
            if endpoint not in offered and len(offered) < SUITE_CANDIDATE_LIMIT:
                offered.append(endpoint)
        detailed: list[Endpoint] = []
        for endpoint in [*primary, *(hit.endpoint for hit in hits[: self.settings.detailed_candidates])]:
            if endpoint not in detailed and len(detailed) < SUITE_CARD_LIMIT:
                detailed.append(endpoint)
        return offered, detailed

    def _card(self, endpoint: Endpoint) -> str:
        return self.index.card(endpoint, max_filter_fields=20, include_response=True)

    def plan(self, prompt: str) -> SuiteOutcome:  # type: ignore[override]
        prompt = redact_text(prompt.strip(), self.secrets)
        if not prompt:
            raise ValueError("Describe the suite you want.")
        self._progress("Searching the catalog")
        offered, detailed_endpoints = self.candidates(prompt)
        if not offered:
            return SuiteOutcome(
                SuitePlan(
                    status="unsupported",
                    name="No matching endpoints",
                    description="No catalog endpoint matches the words in this request. Name the resource, e.g. 'brands'.",
                )
            )
        candidate_ids = [item.id for item in offered]
        self._progress(f"Retrieved {len(offered)} suite endpoint candidates; showing {len(detailed_endpoints)} detailed cards")
        messages = [
            ChatMessage("system", SUITE_SYSTEM),
            ChatMessage(
                "user",
                suite_user_message(
                    prompt,
                    [endpoint_summary(item) for item in offered],
                    [self._card(item) for item in detailed_endpoints],
                ),
            ),
        ]
        schema = suite_plan_schema(candidate_ids)
        outcome = SuiteOutcome(SuitePlan(), candidates=tuple(candidate_ids))
        detailed = {item.id for item in detailed_endpoints}

        for attempt in range(self.settings.max_repairs + 1):
            self._check_cancelled()
            self._progress("Designing the suite" if attempt == 0 else f"Fixing the suite (round {attempt})")
            outcome.attempts = attempt + 1
            started = perf_counter()
            try:
                response = self.provider.complete(
                    redact_messages(messages, self.secrets),
                    model=self.settings.planner_model,
                    response_schema=schema,
                    temperature=0.0,
                    max_output_tokens=6000,
                    timeout_seconds=float(self.settings.timeout_seconds),
                )
            except LlmResponseError as exc:
                self._record_response_failure(outcome, attempt, started, exc)
                if exc.truncated:
                    break
                messages.append(ChatMessage("user", INVALID_JSON_INSTRUCTION.format(error=exc)))
                continue
            except LlmError as exc:
                self._response_failure(attempt, started, exc)
                raise
            outcome.input_tokens += response.input_tokens
            self._record_usage(outcome, attempt, response, int((perf_counter() - started) * 1000))
            outcome.output_tokens += response.output_tokens
            outcome.duration_ms += response.duration_ms
            outcome.model = response.model or self.settings.planner_model
            self._check_cancelled()
            self._progress("Validating against the catalog")
            try:
                draft = SuitePlan.from_json(response.json)
            except ValueError as exc:
                self._progress("Suite structure rejected: " + redact_text(str(exc), self.secrets)[:1000])
                outcome.issues = (PlanIssue("$", "invalid_plan", str(exc)),)
                messages.append(ChatMessage("assistant", response.text))
                messages.append(ChatMessage("user", INVALID_JSON_INSTRUCTION.format(error=exc)))
                continue
            result = validate_suite_plan(draft, self.index)
            outcome.plan = result.plan
            outcome.issues = result.issues
            self._validation_activity(result.issues)
            self._progress(f"Suite contains {len(draft.cases)} cases; endpoint IDs: "
                           + ", ".join(dict.fromkeys(case.endpoint_id for case in draft.cases)))
            unseen = list({
                endpoint.id: endpoint
                for endpoint in (self.index.get(case.endpoint_id) for case in draft.cases)
                if endpoint is not None and endpoint.id not in detailed
            }.values())
            if (result.ok and not unseen) or attempt == self.settings.max_repairs:
                break
            messages.append(ChatMessage("assistant", json.dumps(response.json)))
            follow_up = (
                SUITE_REPAIR_INSTRUCTION.format(issues="\n".join(f"- {item.as_text()}" for item in result.issues))
                if result.issues
                else CONFIRM_WITH_CARD_INSTRUCTION
            )
            if unseen:
                self._progress(f"Refinement required: {len(unseen)} endpoints need detailed catalog cards")
                follow_up += "\nCards for endpoints you used without seeing them:\n\n" + "\n\n".join(
                    self._card(item) for item in unseen
                )
                detailed.update(item.id for item in unseen)
            messages.append(ChatMessage("user", follow_up.strip()))
        outcome.transcript = messages
        return outcome
