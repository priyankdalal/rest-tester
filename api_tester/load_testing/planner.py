"""Load Testing Studio planning layer: validation and stage scheduling math.

Per section 4.1, the planning layer "validates definitions against the
catalog and an immutable environment snapshot" and "produces an executable
plan with resolved endpoint metadata". This module is deliberately UI-free
and network-free so scheduling correctness can be proven with deterministic
unit tests (checkpoint requirement in section 18).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .scenario import MVP_STAGE_KINDS, WRITE_METHODS, LoadScenario


@dataclass(frozen=True)
class LoadValidationIssue:
    severity: str  # "error" | "warning"
    message: str


@dataclass(frozen=True)
class LoadPlan:
    """The executable, pre-validated plan the engine actually runs from.

    ``is_executable`` is false whenever any *blocking* (``severity="error"``)
    issue exists; the engine must refuse to send a single request in that
    case (section 8: "Required controls ... enforced before any request is
    sent").
    """

    scenario: LoadScenario
    issues: tuple[LoadValidationIssue, ...] = field(default_factory=tuple)

    @property
    def is_executable(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def total_duration_seconds(self) -> float:
        return self.scenario.total_duration_seconds

    @property
    def peak_users(self) -> int:
        return self.scenario.peak_users

    def estimated_request_count(self) -> int | None:
        """A rough upper bound used only for the pre-run summary (section 8),
        never for scheduling. Returns ``None`` when it cannot be estimated
        (a stage has zero think time, so request volume is bounded only by
        wall-clock/HTTP latency rather than a computable iteration rate)."""
        total = 0
        for stage in self.scenario.stages:
            if stage.think_time_ms <= 0:
                return None
            iterations_per_user = stage.duration_seconds / (stage.think_time_ms / 1000.0)
            average_users = (stage.start_users + stage.end_users) / 2.0
            total += int(iterations_per_user * average_users)
        return total


def build_plan(scenario: LoadScenario) -> LoadPlan:
    """Validates ``scenario`` and returns the executable :class:`LoadPlan`.

    Every check here mirrors section 8's "Required controls" list. Anything
    not yet implemented (RPS ceiling / queue bounds -- open-model only, see
    section 6.2) is intentionally absent rather than silently ignored: those
    fields do not exist on :class:`~api_tester.load_testing.scenario.SafetyLimits`
    for the closed-model MVP.
    """
    issues: list[LoadValidationIssue] = []
    limits = scenario.limits

    if not scenario.stages:
        issues.append(LoadValidationIssue("error", "A load scenario needs at least one stage."))

    for stage in scenario.stages:
        if stage.kind not in MVP_STAGE_KINDS:
            issues.append(
                LoadValidationIssue(
                    "error",
                    f"Stage kind {stage.kind!r} is not yet supported by the Phase 5 load engine "
                    f"(supported: {', '.join(MVP_STAGE_KINDS)}).",
                )
            )

    if not limits.environment_permits_load_test:
        issues.append(
            LoadValidationIssue(
                "error",
                f"Environment {scenario.environment.environment_name!r} is not allow-listed for "
                "load testing. Enable load-test permission for this environment before starting.",
            )
        )

    method = scenario.endpoint.method.upper()
    if limits.get_head_only and method not in ("GET", "HEAD"):
        issues.append(
            LoadValidationIssue(
                "error",
                f"Safety mode is GET/HEAD-only, but the target endpoint uses {method}.",
            )
        )
    elif method in WRITE_METHODS and not limits.confirmed_write_endpoints:
        issues.append(
            LoadValidationIssue(
                "error",
                f"{method} is a write method. Confirm write endpoints before starting this scenario.",
            )
        )

    peak_users = scenario.peak_users
    if peak_users > limits.max_concurrency:
        issues.append(
            LoadValidationIssue(
                "error",
                f"Peak virtual users ({peak_users}) exceeds the configured concurrency ceiling "
                f"({limits.max_concurrency}).",
            )
        )
    if peak_users <= 0:
        issues.append(LoadValidationIssue("error", "Every stage has zero virtual users; nothing to run."))

    total_duration = scenario.total_duration_seconds
    if total_duration > limits.max_duration_seconds:
        issues.append(
            LoadValidationIssue(
                "error",
                f"Total scenario duration ({total_duration:.0f}s) exceeds the configured duration "
                f"ceiling ({limits.max_duration_seconds:.0f}s).",
            )
        )

    if limits.request_timeout_seconds <= 0:
        issues.append(LoadValidationIssue("error", "request_timeout_seconds must be greater than zero."))

    if limits.error_rate_stop_threshold is not None and not (0.0 <= limits.error_rate_stop_threshold <= 1.0):
        issues.append(LoadValidationIssue("error", "error_rate_stop_threshold must be between 0 and 1."))

    if peak_users >= 25:
        issues.append(
            LoadValidationIssue(
                "warning",
                f"{peak_users} peak virtual users may amplify downstream traffic into dependent "
                "services (see section 8 amplification note).",
            )
        )

    return LoadPlan(scenario=scenario, issues=tuple(issues))


def target_active_users(scenario: LoadScenario, elapsed_seconds: float) -> int:
    """Returns how many virtual users should be active ``elapsed_seconds``
    after the scenario started, per section 6.2's linear ramp semantics.

    Stages are consumed back to back. Before the first stage starts (should
    not normally happen) it returns that stage's ``start_users``; after the
    last stage ends it returns the last stage's ``end_users`` (typically 0
    for a ramp-down stage).
    """
    if not scenario.stages:
        return 0
    if elapsed_seconds < 0:
        return scenario.stages[0].start_users

    cursor = 0.0
    for stage in scenario.stages:
        stage_end = cursor + stage.duration_seconds
        if elapsed_seconds < stage_end or stage is scenario.stages[-1]:
            local_elapsed = elapsed_seconds - cursor
            fraction = (
                0.0
                if stage.duration_seconds <= 0
                else min(1.0, max(0.0, local_elapsed / stage.duration_seconds))
            )
            value = stage.start_users + (stage.end_users - stage.start_users) * fraction
            return max(0, round(value))
        cursor = stage_end
    return scenario.stages[-1].end_users


def stage_at(scenario: LoadScenario, elapsed_seconds: float):
    """Returns the :class:`~api_tester.load_testing.scenario.LoadStage`
    active at ``elapsed_seconds``, or the last stage once the schedule has
    finished."""
    if not scenario.stages:
        return None
    cursor = 0.0
    for stage in scenario.stages:
        stage_end = cursor + stage.duration_seconds
        if elapsed_seconds < stage_end or stage is scenario.stages[-1]:
            return stage
        cursor = stage_end
    return scenario.stages[-1]
