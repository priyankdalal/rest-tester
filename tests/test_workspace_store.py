"""Tests for the shared workspace database."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from api_tester.workspace_store import (
    RETENTION_DAYS,
    WorkspaceStore,
    as_store,
    migrate_legacy_files,
)


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    value = WorkspaceStore(tmp_path / "workspace.db")
    yield value
    value.close()


# ------------------------------------------------------------ run history


def test_endpoint_history_returns_newest_first_for_one_endpoint(store) -> None:
    store.append_run_history("a.one", status_code=200, timestamp=_iso(2))
    store.append_run_history("b.two", status_code=500, timestamp=_iso(1))
    store.append_run_history("a.one", status_code=404, timestamp=_iso(0))
    history = store.endpoint_history("a.one")
    assert [record["status_code"] for record in history] == [404, 200]


def test_endpoint_history_honours_the_limit(store) -> None:
    for index in range(150):
        store.append_run_history("a.one", status_code=index, timestamp=_iso(150 - index))
    assert len(store.endpoint_history("a.one", limit=100)) == 100
    assert store.run_history_count() == 150


def test_run_history_round_trips_every_field(store) -> None:
    store.append_run_history(
        "a.one",
        passed=True,
        status_code=201,
        elapsed_ms=12.5,
        url="https://example.test/items?x=1",
        environment="Dev",
        timestamp="2026-01-01T00:00:00+00:00",
    )
    assert store.endpoint_history("a.one")[0] == {
        "endpoint_id": "a.one",
        "passed": True,
        "status_code": 201,
        "elapsed_ms": 12.5,
        "url": "https://example.test/items?x=1",
        "environment": "Dev",
        "timestamp": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------- suite history


def test_suite_history_is_readable_back(store) -> None:
    store.append_suite_history(
        {"suite": "Smoke", "total": 4, "passed": 3, "failed": 1, "duration_ms": 90.0}
    )
    records = store.suite_history()
    assert len(records) == 1
    assert records[0]["suite"] == "Smoke"
    assert records[0]["passed"] == 3
    assert store.suite_history(suite="Other") == []


# -------------------------------------------------------------- retention


def test_retention_removes_history_older_than_the_window(tmp_path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    store.append_run_history("a.one", timestamp=_iso(RETENTION_DAYS + 1))
    store.append_run_history("a.one", timestamp=_iso(RETENTION_DAYS - 1))
    store.append_suite_history({"suite": "Old", "recorded_at": _iso(RETENTION_DAYS + 1)})
    store.append_suite_history({"suite": "New", "recorded_at": _iso(1)})
    assert store.apply_retention() == 2
    assert store.run_history_count() == 1
    assert [record["suite"] for record in store.suite_history()] == ["New"]
    store.close()


def test_retention_never_expires_saved_requests(tmp_path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db")
    store.upsert_saved_request(
        {
            "id": "r1",
            "endpoint_id": "a.one",
            "name": "Old favourite",
            "created_at": _iso(RETENTION_DAYS * 5),
            "updated_at": _iso(RETENTION_DAYS * 5),
        }
    )
    store.upsert_collection({"id": "c1", "name": "Old", "request_ids": ["r1"]})
    store.apply_retention()
    assert [record["id"] for record in store.load_saved_requests()] == ["r1"]
    assert store.load_collections()[0]["request_ids"] == ["r1"]
    store.close()


def test_retention_runs_when_the_database_is_opened(tmp_path) -> None:
    path = tmp_path / "workspace.db"
    first = WorkspaceStore(path)
    first.append_run_history("a.one", timestamp=_iso(RETENTION_DAYS + 5))
    first.close()
    second = WorkspaceStore(path)
    assert second.run_history_count() == 0
    second.close()


def test_retention_is_disabled_by_a_non_positive_window(tmp_path) -> None:
    store = WorkspaceStore(tmp_path / "workspace.db", retention_days=0)
    store.append_run_history("a.one", timestamp=_iso(10_000))
    assert store.apply_retention() == 0
    assert store.run_history_count() == 1
    store.close()


# ------------------------------------------------ saved requests and order


def test_collection_membership_keeps_order_and_duplicates(store) -> None:
    for request_id in ("r1", "r2"):
        store.upsert_saved_request(
            {"id": request_id, "endpoint_id": "a.one", "name": request_id}
        )
    store.upsert_collection(
        {"id": "c1", "name": "Flow", "request_ids": ["r1", "r2", "r1"]}
    )
    assert store.load_collections()[0]["request_ids"] == ["r1", "r2", "r1"]


def test_deleting_a_request_compacts_the_remaining_positions(store) -> None:
    for request_id in ("r1", "r2", "r3"):
        store.upsert_saved_request(
            {"id": request_id, "endpoint_id": "a.one", "name": request_id}
        )
    store.upsert_collection(
        {"id": "c1", "name": "Flow", "request_ids": ["r1", "r2", "r3"]}
    )
    store.delete_saved_request("r2")
    assert store.load_collections()[0]["request_ids"] == ["r1", "r3"]
    with sqlite3.connect(store.path) as connection:
        positions = [
            row[0]
            for row in connection.execute(
                "SELECT position FROM collection_items ORDER BY position"
            )
        ]
    assert positions == [0, 1]


def test_saved_request_payload_none_is_distinct_from_json_null(store) -> None:
    store.upsert_saved_request({"id": "r1", "endpoint_id": "a.one", "name": "No body"})
    assert store.load_saved_requests()[0]["payload"] is None


# -------------------------------------------------------------- migration


def test_migration_imports_legacy_files_and_retires_them(tmp_path) -> None:
    run_history = tmp_path / "run_history.jsonl"
    run_history.write_text(
        json.dumps({"endpoint_id": "a.one", "status_code": 200, "timestamp": _iso(1)})
        + "\nnot json at all\n"
        + json.dumps({"endpoint_id": "a.one", "status_code": 500, "timestamp": _iso(0)})
        + "\n",
        encoding="utf-8",
    )
    suite_history = tmp_path / "suite_history.jsonl"
    suite_history.write_text(
        json.dumps({"suite": "Smoke", "total": 2, "finished_at": _iso(1)}) + "\n",
        encoding="utf-8",
    )
    saved = tmp_path / "saved_requests.json"
    saved.write_text(
        json.dumps(
            {
                "requests": [{"id": "r1", "endpoint_id": "a.one", "name": "One"}],
                "collections": [{"id": "c1", "name": "Flow", "request_ids": ["r1"]}],
            }
        ),
        encoding="utf-8",
    )

    store = WorkspaceStore(tmp_path / "workspace.db")
    imported = migrate_legacy_files(
        store,
        run_history=run_history,
        suite_history=suite_history,
        saved_requests=saved,
    )
    # The malformed middle line is skipped instead of hiding the rest.
    assert imported == {"run_history": 2, "suite_history": 1, "saved_requests": 1}
    assert len(store.endpoint_history("a.one")) == 2
    assert store.suite_history()[0]["suite"] == "Smoke"
    assert store.load_collections()[0]["request_ids"] == ["r1"]
    assert not run_history.exists()
    assert (tmp_path / "run_history.jsonl.migrated").exists()
    assert (tmp_path / "saved_requests.json.migrated").exists()
    store.close()


def test_migration_is_idempotent(tmp_path) -> None:
    run_history = tmp_path / "run_history.jsonl"
    run_history.write_text(
        json.dumps({"endpoint_id": "a.one", "timestamp": _iso(1)}) + "\n",
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path / "workspace.db")
    migrate_legacy_files(store, run_history=run_history)
    second = migrate_legacy_files(store, run_history=run_history)
    assert second["run_history"] == 0
    assert store.run_history_count() == 1
    store.close()


def test_legacy_suite_rows_expire_on_their_own_finish_time(tmp_path) -> None:
    suite_history = tmp_path / "suite_history.jsonl"
    suite_history.write_text(
        json.dumps({"suite": "Ancient", "finished_at": _iso(RETENTION_DAYS + 30)})
        + "\n",
        encoding="utf-8",
    )
    store = WorkspaceStore(tmp_path / "workspace.db")
    migrate_legacy_files(store, suite_history=suite_history)
    assert store.apply_retention() == 1
    store.close()


# ---------------------------------------------------------------- plumbing


def test_as_store_passes_an_open_store_through(store, tmp_path) -> None:
    assert as_store(store) is store
    opened = as_store(tmp_path / "nested" / "other.db")
    assert opened.path.exists()
    opened.close()


def test_writes_from_several_threads_are_all_recorded(store) -> None:
    def write(index: int) -> None:
        for _ in range(20):
            store.append_run_history(f"ep.{index}", status_code=200)

    threads = [threading.Thread(target=write, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.run_history_count() == 80


def test_building_a_window_never_retires_legacy_files(qt_app, monkeypatch, tmp_path) -> None:
    """Migration renames files, so it must stay out of the widget layer."""
    import api_tester.main as main_module

    legacy = tmp_path / "run_history.jsonl"
    legacy.write_text(
        json.dumps({"endpoint_id": "a.one", "timestamp": _iso(1)}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(main_module, "LEGACY_HISTORY_PATH", legacy)
    window = main_module.MainWindow()
    try:
        assert legacy.exists()
        assert window.workspace_store.run_history_count() == 0
    finally:
        window.close()
