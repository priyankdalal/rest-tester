from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace

from api_tester.execution.errors import ClassifiedError
from api_tester.execution.models import RequestSample
from api_tester.execution.persistence import RunStore


def _sample(index: int) -> RequestSample:
    return RequestSample(
        run_id="DR-1",
        endpoint_id=f"endpoint-{index}",
        service="Test",
        method="GET",
        outcome="passed",
        started_at="2026-09-25T00:00:00.000+00:00",
        offset_ms=float(index),
        queue_delay_ms=0.0,
        http_ms=10.0 + index,
        status_code=200,
        worker_id=index % 4,
    )


def test_run_store_start_and_finish_round_trip(tmp_path) -> None:
    path = tmp_path / "runs.db"
    store = RunStore(path)

    store.start_run(
        "DR-1",
        "load",
        "Smoke",
        "env-1",
        "QA",
        "2026-09-25T00:00:00Z",
        {"users": 5},
    )
    store.finish_run("DR-1", "2026-09-25T00:10:00Z", "PASS")

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT kind, name, environment_id, environment_name, started_at, finished_at, outcome, definition_json FROM runs WHERE run_id = ?",
            ("DR-1",),
        ).fetchone()
    assert row == (
        "load",
        "Smoke",
        "env-1",
        "QA",
        "2026-09-25T00:00:00Z",
        "2026-09-25T00:10:00Z",
        "PASS",
        '{"users": 5}',
    )


def test_run_store_records_single_sample(tmp_path) -> None:
    path = tmp_path / "samples.db"
    store = RunStore(path)

    store.record_sample(_sample(1))

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT run_id, endpoint_id, method, outcome, status_code FROM request_samples"
        ).fetchone()
    assert row == ("DR-1", "endpoint-1", "GET", "passed", 200)


def test_run_store_records_sample_batch(tmp_path) -> None:
    path = tmp_path / "batch.db"
    store = RunStore(path)

    store.record_samples_batch([_sample(1), _sample(2), _sample(3)])

    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM request_samples").fetchone()[0]
    assert count == 3


def test_run_store_upserts_errors_by_signature(tmp_path) -> None:
    path = tmp_path / "errors.db"
    store = RunStore(path)
    classified = ClassifiedError("http_5xx", "HTTP 500")

    store.record_error("DR-1", classified, "endpoint-1", "2026-09-25T00:00:00Z")
    store.record_error("DR-1", classified, "endpoint-1", "2026-09-25T00:01:00Z")

    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT signature, count, first_seen, last_seen FROM errors"
        ).fetchall()
    assert rows == [
        (classified.signature("endpoint-1"), 2, "2026-09-25T00:00:00Z", "2026-09-25T00:01:00Z")
    ]


def test_run_store_record_sample_is_thread_safe(tmp_path) -> None:
    path = tmp_path / "concurrent.db"
    store = RunStore(path)
    failures: list[BaseException] = []

    def worker(start: int) -> None:
        try:
            for index in range(start, start + 5):
                store.record_sample(_sample(index))
        except BaseException as exc:  # pragma: no cover - assertion surface only
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(offset,)) for offset in (0, 5, 10, 15)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    with sqlite3.connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM request_samples").fetchone()[0]
    assert count == 20


# ------------------------------------------------------------------ reads (history browsing)


def test_list_runs_returns_most_recent_first(tmp_path) -> None:
    store = RunStore(tmp_path / "history.db")
    store.start_run("DR-1", "data_runner", "Brands", "env-1", "QA", "2026-09-25T00:00:00Z", {"csv_path": "a.csv"})
    store.start_run("DR-2", "data_runner", "Teams", "env-1", "QA", "2026-09-25T01:00:00Z", {"csv_path": "b.csv"})
    store.finish_run("DR-1", "2026-09-25T00:05:00Z", "PASS")
    store.finish_run("DR-2", "2026-09-25T01:05:00Z", "FAIL")

    runs = store.list_runs()

    assert [run["run_id"] for run in runs] == ["DR-2", "DR-1"]
    assert runs[0]["outcome"] == "FAIL"
    assert runs[0]["definition"] == {"csv_path": "b.csv"}


def test_get_run_returns_none_for_unknown_id(tmp_path) -> None:
    store = RunStore(tmp_path / "history.db")
    assert store.get_run("does-not-exist") is None


def test_get_run_returns_a_single_run(tmp_path) -> None:
    store = RunStore(tmp_path / "history.db")
    store.start_run("DR-1", "data_runner", "Brands", "env-1", "QA", "2026-09-25T00:00:00Z", {})
    run = store.get_run("DR-1")
    assert run is not None
    assert run["name"] == "Brands"


def test_count_by_outcome_groups_samples(tmp_path) -> None:
    store = RunStore(tmp_path / "history.db")
    passed = _sample(1)
    failed = replace(_sample(2), outcome="failed")
    store.record_samples_batch([passed, passed, failed])

    counts = store.count_by_outcome("DR-1")

    assert counts == {"passed": 2, "failed": 1}


def test_list_samples_returns_rows_in_insertion_order(tmp_path) -> None:
    store = RunStore(tmp_path / "history.db")
    store.record_samples_batch([_sample(1), _sample(2), _sample(3)])

    samples = store.list_samples("DR-1")

    assert [s["endpoint_id"] for s in samples] == ["endpoint-1", "endpoint-2", "endpoint-3"]


def test_list_samples_supports_limit_and_offset(tmp_path) -> None:
    store = RunStore(tmp_path / "history.db")
    store.record_samples_batch([_sample(1), _sample(2), _sample(3)])

    samples = store.list_samples("DR-1", limit=1, offset=1)

    assert [s["endpoint_id"] for s in samples] == ["endpoint-2"]


def test_list_errors_orders_by_count_descending(tmp_path) -> None:
    store = RunStore(tmp_path / "history.db")
    frequent = ClassifiedError("http_5xx", "HTTP 500")
    rare = ClassifiedError("timeout", "Timed out")
    store.record_error("DR-1", frequent, "endpoint-1", "2026-09-25T00:00:00Z")
    store.record_error("DR-1", frequent, "endpoint-1", "2026-09-25T00:01:00Z")
    store.record_error("DR-1", rare, "endpoint-1", "2026-09-25T00:02:00Z")

    errors = store.list_errors("DR-1")

    assert [e["signature"] for e in errors] == [
        frequent.signature("endpoint-1"),
        rare.signature("endpoint-1"),
    ]


def test_record_row_detail_round_trips_a_single_row(tmp_path) -> None:
    store = RunStore(tmp_path / "row_details.db")
    detail = {"input_row": {"name": "Acme"}, "response": {"status": 200}, "assertions": []}

    store.record_row_detail("DR-1", 3, detail)

    assert store.get_row_detail("DR-1", 3) == detail


def test_record_row_details_batch_round_trips_multiple_rows(tmp_path) -> None:
    store = RunStore(tmp_path / "row_details_batch.db")
    items = [(1, {"input_row": {"a": "1"}}), (2, {"input_row": {"a": "2"}})]

    store.record_row_details_batch("DR-1", items)

    assert store.get_row_detail("DR-1", 1) == {"input_row": {"a": "1"}}
    assert store.get_row_detail("DR-1", 2) == {"input_row": {"a": "2"}}


def test_get_row_detail_returns_none_for_unknown_row(tmp_path) -> None:
    store = RunStore(tmp_path / "row_details_missing.db")

    assert store.get_row_detail("DR-1", 99) is None


def test_row_details_are_scoped_per_run(tmp_path) -> None:
    store = RunStore(tmp_path / "row_details_scoped.db")
    store.record_row_detail("DR-1", 1, {"input_row": {"run": "one"}})
    store.record_row_detail("DR-2", 1, {"input_row": {"run": "two"}})

    assert store.get_row_detail("DR-1", 1) == {"input_row": {"run": "one"}}
    assert store.get_row_detail("DR-2", 1) == {"input_row": {"run": "two"}}


def test_record_row_detail_replaces_an_existing_row(tmp_path) -> None:
    store = RunStore(tmp_path / "row_details_replace.db")
    store.record_row_detail("DR-1", 1, {"input_row": {"a": "old"}})

    store.record_row_detail("DR-1", 1, {"input_row": {"a": "new"}})

    assert store.get_row_detail("DR-1", 1) == {"input_row": {"a": "new"}}
