from __future__ import annotations

import csv
from types import SimpleNamespace

import pytest

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.data_runner.csv_source import CsvImportSettings, CsvSource
from api_tester.data_runner.mapping import ColumnMapping, Transform
from api_tester.data_runner.planner import build_plan
from api_tester.data_runner.runner import DataRunner, DataRunnerOptions
from api_tester.execution.events import EventBus
from api_tester.execution.models import ExecutionEnvironmentSnapshot
from api_tester.execution.persistence import RunStore


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def request(self, *args, **kwargs):
        from api_tester import client

        return client.requests.request(*args, **kwargs)

    def close(self) -> None:
        self.closed = True


def _endpoint() -> Endpoint:
    return Endpoint(
        id="brands.update",
        service="TrialAuth",
        controller="Brands",
        action="Update",
        method="PATCH",
        path="/Brands/{Id}",
        parameters=(
            Parameter(name="Id", source="path", type="int", required=True),
            Parameter(name="Status", source="query", type="string", required=False),
        ),
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


def _write_csv(tmp_path, rows: list[dict[str, str]], header: list[str]) -> str:
    path = tmp_path / "data.csv"
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(row.get(column, "") for column in header))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _install_fake_request(monkeypatch: pytest.MonkeyPatch, status_by_id: dict[str, int] | None = None):
    status_by_id = status_by_id or {}
    calls: list[dict[str, object]] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        status_code = 200
        for row_id, status in status_by_id.items():
            if url.endswith(f"/Brands/{row_id}"):
                status_code = status
                break
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


def _build_plan(tmp_path, rows, header=("Id", "Status")):
    endpoint = _endpoint()
    catalog = _catalog(endpoint)
    csv_path = _write_csv(tmp_path, rows, list(header))
    csv_source = CsvSource(CsvImportSettings(path=csv_path))
    mappings = [
        ColumnMapping("Id", "path:Id", transforms=(Transform("required"),)),
        ColumnMapping("Status", "query:Status"),
    ]
    return build_plan(endpoint, catalog, csv_source, mappings, _environment())


def test_runner_executes_all_valid_rows_and_reports_pass(tmp_path, monkeypatch) -> None:
    calls = _install_fake_request(monkeypatch)
    plan = _build_plan(
        tmp_path,
        [{"Id": "1", "Status": "Active"}, {"Id": "2", "Status": "Inactive"}],
    )
    runner = DataRunner(plan)

    summary = runner.run()

    assert summary.outcome == "PASS"
    assert summary.executed == 2
    assert summary.passed == 2
    assert summary.failed == 0
    assert len(calls) == 2
    assert [row.outcome for row in runner.row_outcomes] == ["passed", "passed"]


def test_runner_skips_invalid_rows_without_calling_transport(tmp_path, monkeypatch) -> None:
    calls = _install_fake_request(monkeypatch)
    plan = _build_plan(tmp_path, [{"Id": "1", "Status": "Active"}, {"Id": "", "Status": "Bad"}])
    runner = DataRunner(plan)

    summary = runner.run()

    assert summary.executed == 1
    assert summary.invalid == 1
    assert len(calls) == 1
    outcomes = [row.outcome for row in runner.row_outcomes]
    assert outcomes == ["passed", "invalid"]


def test_runner_reports_fail_outcome_on_http_failure(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch, status_by_id={"1": 500})
    plan = _build_plan(tmp_path, [{"Id": "1", "Status": "Active"}])
    runner = DataRunner(plan)

    summary = runner.run()

    assert summary.outcome == "FAIL"
    assert summary.failed == 1
    assert runner.row_outcomes[0].error_category == "http_5xx"


def test_runner_aborts_immediately_when_plan_is_not_executable(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    endpoint = _endpoint()
    catalog = _catalog(endpoint)
    csv_source = CsvSource(CsvImportSettings(path=str(tmp_path / "missing.csv")))
    plan = build_plan(endpoint, catalog, csv_source, [ColumnMapping("Id", "path:Id")], _environment())
    runner = DataRunner(plan)

    summary = runner.run()

    assert summary.outcome == "ABORTED"
    assert summary.executed == 0


def test_runner_persists_samples_and_errors_to_run_store(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch, status_by_id={"2": 404})
    plan = _build_plan(tmp_path, [{"Id": "1", "Status": "Active"}, {"Id": "2", "Status": "Bad"}])
    store = RunStore(tmp_path / "runs.db")
    runner = DataRunner(plan, store=store, options=DataRunnerOptions(batch_size=1))

    summary = runner.run()

    cursor = store._connection.execute(
        "SELECT COUNT(*) FROM request_samples WHERE run_id = ?", (runner.run_id,)
    )
    assert cursor.fetchone()[0] == 2
    error_cursor = store._connection.execute(
        "SELECT COUNT(*) FROM errors WHERE run_id = ?", (runner.run_id,)
    )
    assert error_cursor.fetchone()[0] == 1
    run_row = store._connection.execute(
        "SELECT outcome FROM runs WHERE run_id = ?", (runner.run_id,)
    ).fetchone()
    assert run_row[0] == summary.outcome
    store.close()


def test_runner_persists_row_details_to_run_store(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch, status_by_id={"2": 404})
    plan = _build_plan(
        tmp_path,
        [{"Id": "1", "Status": "Active"}, {"Id": "2", "Status": "Bad"}, {"Id": "", "Status": "Bad"}],
    )
    store = RunStore(tmp_path / "runs.db")
    runner = DataRunner(plan, store=store, options=DataRunnerOptions(batch_size=1))

    runner.run()

    passed_detail = store.get_row_detail(runner.run_id, 1)
    assert passed_detail is not None
    assert passed_detail["response"]["body_retained"] is False

    failed_detail = store.get_row_detail(runner.run_id, 2)
    assert failed_detail is not None
    assert failed_detail["response"]["body_retained"] is True

    invalid_detail = store.get_row_detail(runner.run_id, 3)
    assert invalid_detail is not None
    assert invalid_detail["response"] is None
    store.close()


def test_runner_publishes_events_for_each_row(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    plan = _build_plan(tmp_path, [{"Id": "1", "Status": "Active"}])
    bus = EventBus()
    runner = DataRunner(plan, event_bus=bus)

    runner.run()

    kinds = [event.kind for event in bus.drain()]
    assert "run_started" in kinds
    assert "row_started" in kinds
    assert "request_started" in kinds
    assert "request_completed" in kinds
    assert "run_finished" in kinds


def test_request_completed_event_carries_a_row_detail_payload(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    plan = _build_plan(tmp_path, [{"Id": "1", "Status": "Active"}])
    bus = EventBus()
    runner = DataRunner(plan, event_bus=bus)

    runner.run()

    events = bus.drain()
    completed = next(e for e in events if e.kind == "request_completed")
    detail = completed.payload["detail"]
    assert detail["input_row"] == {"Id": "1", "Status": "Active"}
    assert detail["resolved_request"]["path"] == {"Id": "1"}
    assert detail["resolved_request"]["query"] == {"Status": "Active"}
    assert detail["response"]["status_code"] == 200
    # Passed rows don't retain the full response body, to bound memory.
    assert detail["response"]["body_retained"] is False
    assert detail["response"]["body"] is None
    assert detail["assertions"][0]["passed"] is True


def test_request_failed_event_retains_the_response_body(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch, status_by_id={"1": 500})
    plan = _build_plan(tmp_path, [{"Id": "1", "Status": "Active"}])
    bus = EventBus()
    runner = DataRunner(plan, event_bus=bus)

    runner.run()

    events = bus.drain()
    failed = next(e for e in events if e.kind == "request_failed")
    detail = failed.payload["detail"]
    assert detail["response"]["body_retained"] is True
    assert "true" in detail["response"]["body"]
    assert detail["assertions"][0]["passed"] is False


def test_row_completed_event_carries_a_detail_payload_for_invalid_rows(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    plan = _build_plan(tmp_path, [{"Id": "", "Status": "Bad"}])
    bus = EventBus()
    runner = DataRunner(plan, event_bus=bus)

    runner.run()

    events = bus.drain()
    row_completed = next(e for e in events if e.kind == "row_completed")
    detail = row_completed.payload["detail"]
    assert detail["input_row"] == {"Id": "", "Status": "Bad"}
    assert detail["response"] is None
    assert detail["assertions"]
    assert all(assertion["passed"] is False for assertion in detail["assertions"])


def test_runner_stop_halts_remaining_rows(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    plan = _build_plan(
        tmp_path,
        [{"Id": "1", "Status": "A"}, {"Id": "2", "Status": "B"}, {"Id": "3", "Status": "C"}],
    )
    runner = DataRunner(plan)
    runner.cancellation.stop()

    summary = runner.run()

    assert summary.stopped is True
    assert summary.outcome == "STOPPED"
    assert summary.executed == 0


def test_export_rows_csv_writes_reserved_columns(tmp_path, monkeypatch) -> None:
    _install_fake_request(monkeypatch)
    plan = _build_plan(tmp_path, [{"Id": "1", "Status": "Active"}])
    runner = DataRunner(plan)
    runner.run()

    export_path = tmp_path / "results.csv"
    runner.export_rows_csv(str(export_path))

    with open(export_path, newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    assert rows[0][:3] == ["_run_id", "_row_number", "_outcome"]
    assert rows[1][2] == "passed"


def test_run_definition_records_template_keys_but_never_values() -> None:
    from types import SimpleNamespace

    from api_tester.data_runner.runner import DataRunner

    mapper = SimpleNamespace(template_values={"header:X-Tenant": "secret", "path:Id": "1"}, template_payload=None)
    definition = DataRunner._request_template_definition(SimpleNamespace(mapper=mapper))
    assert definition == {"request_template": {"keys": ["header:X-Tenant", "path:Id"], "payload": False}}
    assert "secret" not in repr(definition)
    empty = SimpleNamespace(template_values={}, template_payload=None)
    assert DataRunner._request_template_definition(SimpleNamespace(mapper=empty)) == {}
    assert DataRunner._request_template_definition(SimpleNamespace(mapper=None)) == {}
