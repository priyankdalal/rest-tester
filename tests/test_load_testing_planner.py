from __future__ import annotations

import pytest

from api_tester.catalog import Endpoint, Parameter
from api_tester.execution.models import ExecutionEnvironmentSnapshot, RequestTemplate
from api_tester.load_testing.planner import build_plan, stage_at, target_active_users
from api_tester.load_testing.scenario import (
    LoadScenario,
    LoadStage,
    SafetyLimits,
    ThresholdDefinition,
)


def _endpoint(method: str = "GET") -> Endpoint:
    return Endpoint(
        id="brands.get",
        service="TrialAuth",
        controller="Brands",
        action="Get",
        method=method,
        path="/Brands/{Id}",
        parameters=(Parameter(name="Id", source="path", type="int", required=True),),
        expected_status="200-299",
    )


def _environment() -> ExecutionEnvironmentSnapshot:
    return ExecutionEnvironmentSnapshot(
        environment_id="env-1",
        environment_name="QA",
        base_urls={"TrialAuth": "https://example.test"},
    )


def _template(endpoint: Endpoint) -> RequestTemplate:
    return RequestTemplate(
        endpoint_id=endpoint.id,
        service=endpoint.service,
        method=endpoint.method,
        path=endpoint.path,
        values={"path:Id": "1"},
    )


def _scenario(
    *,
    method: str = "GET",
    stages: tuple[LoadStage, ...] | None = None,
    limits: SafetyLimits | None = None,
    thresholds: tuple[ThresholdDefinition, ...] = (),
) -> LoadScenario:
    endpoint = _endpoint(method)
    stages = stages or (
        LoadStage("warm_up", duration_seconds=1, start_users=1, end_users=1),
        LoadStage("ramp_up", duration_seconds=2, start_users=1, end_users=5),
        LoadStage("steady", duration_seconds=4, start_users=5, end_users=5),
        LoadStage("ramp_down", duration_seconds=1, start_users=5, end_users=0),
    )
    limits = limits or SafetyLimits(environment_permits_load_test=True, max_concurrency=10)
    return LoadScenario(
        environment=_environment(),
        endpoint=endpoint,
        template=_template(endpoint),
        stages=stages,
        limits=limits,
        thresholds=thresholds,
    )


class TestLoadStage:
    def test_rejects_unknown_kind(self) -> None:
        with pytest.raises(ValueError):
            LoadStage("bogus", duration_seconds=1, start_users=1, end_users=1)

    def test_rejects_non_positive_duration(self) -> None:
        with pytest.raises(ValueError):
            LoadStage("steady", duration_seconds=0, start_users=1, end_users=1)

    def test_warm_up_excluded_from_sla_by_default(self) -> None:
        stage = LoadStage("warm_up", duration_seconds=1, start_users=1, end_users=1)
        assert stage.counts_toward_sla_effective is False

    def test_steady_included_in_sla_by_default(self) -> None:
        stage = LoadStage("steady", duration_seconds=1, start_users=1, end_users=1)
        assert stage.counts_toward_sla_effective is True

    def test_explicit_override_wins(self) -> None:
        stage = LoadStage("warm_up", duration_seconds=1, start_users=1, end_users=1, counts_toward_sla=True)
        assert stage.counts_toward_sla_effective is True


class TestScheduleMath:
    def test_target_active_users_interpolates_linearly_within_ramp(self) -> None:
        scenario = _scenario()
        # ramp_up spans elapsed 1..3, users 1 -> 5.
        assert target_active_users(scenario, 1.0) == 1
        assert target_active_users(scenario, 2.0) == 3  # halfway: 1 + (5-1)*0.5 = 3
        assert target_active_users(scenario, 3.0) == 5

    def test_target_active_users_flat_during_steady(self) -> None:
        scenario = _scenario()
        assert target_active_users(scenario, 5.0) == 5

    def test_target_active_users_ramps_down_to_zero(self) -> None:
        scenario = _scenario()
        assert target_active_users(scenario, 8.0) == 0  # end of ramp_down (cursor 7..8)

    def test_target_active_users_clamped_after_schedule_ends(self) -> None:
        scenario = _scenario()
        assert target_active_users(scenario, 999.0) == 0

    def test_target_active_users_before_start_returns_first_stage_start(self) -> None:
        scenario = _scenario()
        assert target_active_users(scenario, -1.0) == 1

    def test_stage_at_returns_expected_stage_kind(self) -> None:
        scenario = _scenario()
        assert stage_at(scenario, 0.5).kind == "warm_up"
        assert stage_at(scenario, 2.0).kind == "ramp_up"
        assert stage_at(scenario, 5.0).kind == "steady"
        assert stage_at(scenario, 7.5).kind == "ramp_down"
        assert stage_at(scenario, 999.0).kind == "ramp_down"  # last stage after schedule ends


class TestBuildPlan:
    def test_executable_scenario_has_no_blocking_issues(self) -> None:
        plan = build_plan(_scenario())
        assert plan.is_executable
        assert not any(issue.severity == "error" for issue in plan.issues)

    def test_blocks_when_environment_does_not_permit_load_testing(self) -> None:
        plan = build_plan(_scenario(limits=SafetyLimits(environment_permits_load_test=False, max_concurrency=10)))
        assert not plan.is_executable
        assert any("allow-listed" in issue.message for issue in plan.issues)

    def test_blocks_write_method_without_confirmation(self) -> None:
        plan = build_plan(
            _scenario(
                method="POST",
                limits=SafetyLimits(environment_permits_load_test=True, max_concurrency=10, confirmed_write_endpoints=False),
            )
        )
        assert not plan.is_executable
        assert any("write method" in issue.message for issue in plan.issues)

    def test_write_method_allowed_once_confirmed(self) -> None:
        plan = build_plan(
            _scenario(
                method="POST",
                limits=SafetyLimits(environment_permits_load_test=True, max_concurrency=10, confirmed_write_endpoints=True),
            )
        )
        assert plan.is_executable

    def test_blocks_when_get_head_only_and_endpoint_is_a_write(self) -> None:
        plan = build_plan(
            _scenario(
                method="POST",
                limits=SafetyLimits(
                    environment_permits_load_test=True,
                    max_concurrency=10,
                    get_head_only=True,
                    confirmed_write_endpoints=True,
                ),
            )
        )
        assert not plan.is_executable
        assert any("GET/HEAD-only" in issue.message for issue in plan.issues)

    def test_blocks_when_peak_users_exceeds_concurrency_ceiling(self) -> None:
        plan = build_plan(_scenario(limits=SafetyLimits(environment_permits_load_test=True, max_concurrency=2)))
        assert not plan.is_executable
        assert any("concurrency ceiling" in issue.message for issue in plan.issues)

    def test_blocks_when_duration_exceeds_ceiling(self) -> None:
        limits = SafetyLimits(environment_permits_load_test=True, max_concurrency=10, max_duration_seconds=2)
        plan = build_plan(_scenario(limits=limits))
        assert not plan.is_executable
        assert any("duration" in issue.message for issue in plan.issues)

    def test_blocks_unsupported_stage_kind(self) -> None:
        stages = (LoadStage("spike", duration_seconds=1, start_users=1, end_users=1),)
        plan = build_plan(_scenario(stages=stages))
        assert not plan.is_executable
        assert any("not yet supported" in issue.message for issue in plan.issues)

    def test_warns_but_does_not_block_high_peak_users(self) -> None:
        stages = (LoadStage("steady", duration_seconds=1, start_users=30, end_users=30),)
        limits = SafetyLimits(environment_permits_load_test=True, max_concurrency=50)
        plan = build_plan(_scenario(stages=stages, limits=limits))
        assert plan.is_executable
        assert any(issue.severity == "warning" for issue in plan.issues)

    def test_estimated_request_count_uses_think_time_and_average_users(self) -> None:
        stages = (LoadStage("steady", duration_seconds=10, start_users=2, end_users=2, think_time_ms=1000),)
        plan = build_plan(_scenario(stages=stages, limits=SafetyLimits(environment_permits_load_test=True, max_concurrency=10)))
        # 10s / 1s think time = 10 iterations/user * 2 users = 20.
        assert plan.estimated_request_count() == 20

    def test_estimated_request_count_is_none_without_think_time(self) -> None:
        stages = (LoadStage("steady", duration_seconds=10, start_users=2, end_users=2, think_time_ms=0),)
        plan = build_plan(_scenario(stages=stages, limits=SafetyLimits(environment_permits_load_test=True, max_concurrency=10)))
        assert plan.estimated_request_count() is None
