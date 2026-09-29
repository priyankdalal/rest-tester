from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.execution.events import EventBus
from api_tester.execution.models import ExecutionEnvironmentSnapshot, RequestTemplate
from api_tester.execution.persistence import RunStore
from api_tester.load_testing.engine import LoadEngine, LoadRunOptions
from api_tester.load_testing.planner import build_plan
from api_tester.load_testing.scenario import LoadScenario, LoadStage, SafetyLimits, ThresholdDefinition


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def request(self, *args, **kwargs):
        from api_tester import client

        return client.requests.request(*args, **kwargs)

    def close(self) -> None:
        self.closed = True


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


def _catalog(endpoint: Endpoint) -> Catalog:
    return Catalog(
        services=(Service("TrialAuth", "TrialAuth", "https://example.test", (endpoint,)),),
        filter_schemas={},
        payload_schemas={},
        enums={},
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


def _install_fake_request(monkeypatch: pytest.MonkeyPatch, status_code: int = 200):
    calls: list[dict[str, object]] = []
    lock = threading.Lock()

    def fake_request(method, url, **kwargs):
        with lock:
            calls.append({"method": method, "url": url})
        return SimpleNamespace(
            status_code=status_code,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
            url=url,
            request=SimpleNamespace(headers=kwargs.get("headers", {}), body=None),
            reason="OK",
        )

    monkeypatch.setattr("api_tester.client.requests.request", fake_request)
    monkeypatch.setattr("api_tester.execution.transport.requests.Session", _FakeSession)
    return calls


def _fast_options(**overrides) -> LoadRunOptions:
    defaults = dict(
        batch_size=1,
        metric_snapshot_interval_seconds=0.05,
        drain_timeout_seconds=2.0,
        active_user_poll_seconds=0.01,
    )
    defaults.update(overrides)
    return LoadRunOptions(**defaults)


def _scenario(
    *,
    method: str = "GET",
    stages: tuple[LoadStage, ...] | None = None,
    limits: SafetyLimits | None = None,
    thresholds: tuple[ThresholdDefinition, ...] = (),
) -> LoadScenario:
    endpoint = _endpoint(method)
    stages = stages or (LoadStage("steady", duration_seconds=0.3, start_users=2, end_users=2, think_time_ms=5),)
    limits = limits or SafetyLimits(environment_permits_load_test=True, max_concurrency=10, auth_failure_stop=False)
    return LoadScenario(
        environment=_environment(),
        endpoint=endpoint,
        template=_template(endpoint),
        stages=stages,
        limits=limits,
        thresholds=thresholds,
    )


def test_engine_executes_requests_across_the_full_schedule(monkeypatch) -> None:
    calls = _install_fake_request(monkeypatch)
    scenario = _scenario()
    plan = build_plan(scenario)
    engine = LoadEngine(plan, options=_fast_options())

    summary = engine.run()

    assert summary.outcome == "PASS"
    assert summary.total_requests > 0
    assert summary.total_requests == len(calls)
    assert summary.passed == summary.total_requests
    assert summary.failed == 0
    assert summary.errored == 0
    assert summary.stop_reason is None


def test_engine_never_sends_a_request_when_the_plan_is_not_executable(monkeypatch) -> None:
    calls = _install_fake_request(monkeypatch)
    scenario = _scenario(limits=SafetyLimits(environment_permits_load_test=False, max_concurrency=10))
    plan = build_plan(scenario)
    engine = LoadEngine(plan, options=_fast_options())

    summary = engine.run()

    assert summary.outcome == "ABORTED"
    assert summary.total_requests == 0
    assert calls == []


def test_engine_reports_fail_outcome_when_a_threshold_is_violated(monkeypatch) -> None:
    _install_fake_request(monkeypatch, status_code=500)
    scenario = _scenario(
        limits=SafetyLimits(
            environment_permits_load_test=True,
            max_concurrency=10,
            auth_failure_stop=False,
            error_rate_stop_threshold=None,
        ),
        thresholds=(ThresholdDefinition("error_rate", "<=", 0.0),),
    )
    plan = build_plan(scenario)
    engine = LoadEngine(plan, options=_fast_options())

    summary = engine.run()

    assert summary.outcome == "FAIL"
    assert summary.failed == summary.total_requests
    assert len(summary.threshold_results) == 1
    assert summary.threshold_results[0].passed is False


def test_engine_stops_early_when_error_rate_exceeds_the_safety_threshold(monkeypatch) -> None:
    _install_fake_request(monkeypatch, status_code=500)
    scenario = _scenario(
        stages=(LoadStage("steady", duration_seconds=5.0, start_users=2, end_users=2, think_time_ms=5),),
        limits=SafetyLimits(
            environment_permits_load_test=True,
            max_concurrency=10,
            auth_failure_stop=False,
            error_rate_stop_threshold=0.1,
        ),
    )
    plan = build_plan(scenario)
    engine = LoadEngine(plan, options=_fast_options())

    started = time.monotonic()
    summary = engine.run()
    elapsed = time.monotonic() - started

    assert summary.outcome == "STOPPED"
    assert summary.stop_reason is not None
    assert "error rate" in summary.stop_reason.lower()
    # A 5s schedule stopped early by the safety threshold must not run to completion.
    assert elapsed < 4.0


def test_engine_stop_requested_externally_produces_a_valid_partial_result(monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    scenario = _scenario(stages=(LoadStage("steady", duration_seconds=5.0, start_users=2, end_users=2, think_time_ms=5),))
    plan = build_plan(scenario)
    engine = LoadEngine(plan, options=_fast_options())

    result_holder: dict[str, object] = {}

    def _run():
        result_holder["summary"] = engine.run()

    thread = threading.Thread(target=_run)
    thread.start()
    time.sleep(0.2)
    engine.cancellation.stop()
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    summary = result_holder["summary"]
    assert summary.outcome == "STOPPED"
    assert summary.stop_reason == "Run stopped by request."
    assert summary.total_requests > 0


def test_engine_persists_samples_and_run_metadata_to_the_store(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    scenario = _scenario()
    plan = build_plan(scenario)
    store = RunStore(tmp_path / "load_runs.db")
    engine = LoadEngine(plan, store=store, options=_fast_options())

    summary = engine.run()

    run_row = store.get_run(engine.run_id)
    assert run_row is not None
    assert run_row["kind"] == "load_test"
    assert run_row["outcome"] == summary.outcome

    sample_count = store._connection.execute(
        "SELECT COUNT(*) FROM request_samples WHERE run_id = ?", (engine.run_id,)
    ).fetchone()[0]
    assert sample_count == summary.total_requests


def test_engine_publishes_run_started_metric_snapshot_and_run_finished_events(monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    scenario = _scenario(stages=(LoadStage("steady", duration_seconds=0.4, start_users=2, end_users=2, think_time_ms=5),))
    plan = build_plan(scenario)
    bus = EventBus()
    engine = LoadEngine(plan, event_bus=bus, options=_fast_options())

    engine.run()

    kinds = [event.kind for event in bus.drain()]
    assert "run_started" in kinds
    assert "metric_snapshot" in kinds
    assert "run_finished" in kinds


def test_engine_scales_active_users_according_to_the_stage_schedule(monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    scenario = _scenario(
        stages=(
            LoadStage("ramp_up", duration_seconds=0.2, start_users=1, end_users=4, think_time_ms=5),
            LoadStage("steady", duration_seconds=0.2, start_users=4, end_users=4, think_time_ms=5),
        ),
        limits=SafetyLimits(environment_permits_load_test=True, max_concurrency=10, auth_failure_stop=False),
    )
    plan = build_plan(scenario)
    assert plan.peak_users == 4
    engine = LoadEngine(plan, options=_fast_options())

    summary = engine.run()

    assert summary.peak_users == 4
    assert summary.total_requests > 0
