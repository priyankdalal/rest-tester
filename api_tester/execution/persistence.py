"""SQLite persistence for execution runs, samples, grouped errors, and artifacts."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .errors import ClassifiedError
from .models import RequestSample

_REQUEST_SAMPLE_COLUMNS = (
    "run_id",
    "endpoint_id",
    "service",
    "method",
    "outcome",
    "started_at",
    "offset_ms",
    "queue_delay_ms",
    "http_ms",
    "status_code",
    "worker_id",
    "iteration",
    "row_number",
    "correlation_key",
    "error_category",
    "error_message",
    "request_bytes",
    "response_bytes",
    "assertions_passed",
    "assertions_failed",
    "retry_count",
)


def store_file_size(path: str | Path) -> int:
    """Bytes used by a SQLite store, including its ``-wal``/``-journal`` side files."""
    base = Path(path)
    total = 0
    for candidate in (base, Path(f"{base}-wal"), Path(f"{base}-journal")):
        try:
            total += candidate.stat().st_size
        except OSError:
            continue
    return total


class RunStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        # One shared connection keeps batched writes efficient; check_same_thread=False
        # is safe here because every DB operation is serialized by this lock.
        self._connection = sqlite3.connect(self._path, check_same_thread=False)
        self.initialize()

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs(
                    run_id TEXT PRIMARY KEY,
                    kind TEXT,
                    name TEXT,
                    environment_id TEXT,
                    environment_name TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    outcome TEXT,
                    definition_json TEXT
                );

                CREATE TABLE IF NOT EXISTS request_samples(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    endpoint_id TEXT,
                    service TEXT,
                    method TEXT,
                    outcome TEXT,
                    started_at TEXT,
                    offset_ms REAL,
                    queue_delay_ms REAL,
                    http_ms REAL,
                    status_code INTEGER,
                    worker_id INTEGER,
                    iteration INTEGER,
                    row_number INTEGER,
                    correlation_key TEXT,
                    error_category TEXT,
                    error_message TEXT,
                    request_bytes INTEGER,
                    response_bytes INTEGER,
                    assertions_passed INTEGER,
                    assertions_failed INTEGER,
                    retry_count INTEGER
                );

                CREATE TABLE IF NOT EXISTS errors(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    signature TEXT,
                    category TEXT,
                    endpoint_id TEXT,
                    message TEXT,
                    first_seen TEXT,
                    last_seen TEXT,
                    count INTEGER
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_errors_run_signature
                ON errors(run_id, signature);

                CREATE TABLE IF NOT EXISTS artifacts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    kind TEXT,
                    path TEXT,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS row_details(
                    run_id TEXT,
                    row_number INTEGER,
                    detail_json TEXT,
                    PRIMARY KEY (run_id, row_number)
                );

                CREATE TABLE IF NOT EXISTS run_snapshots(
                    run_id TEXT,
                    seq INTEGER,
                    elapsed_seconds REAL,
                    snapshot_json TEXT,
                    PRIMARY KEY (run_id, seq)
                );

                CREATE TABLE IF NOT EXISTS run_reports(
                    run_id TEXT PRIMARY KEY,
                    report_json TEXT
                );
                """
            )
            self._connection.commit()

    def start_run(
        self,
        run_id: str,
        kind: str,
        name: str,
        environment_id: str,
        environment_name: str,
        started_at: str,
        definition: dict,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO runs(
                    run_id, kind, name, environment_id, environment_name,
                    started_at, finished_at, outcome, definition_json
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    name,
                    environment_id,
                    environment_name,
                    started_at,
                    "RUNNING",
                    json.dumps(definition),
                ),
            )
            self._connection.commit()

    def finish_run(self, run_id: str, finished_at: str, outcome: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE runs SET finished_at = ?, outcome = ? WHERE run_id = ?",
                (finished_at, outcome, run_id),
            )
            self._connection.commit()

    def record_sample(self, sample: RequestSample) -> None:
        self.record_samples_batch([sample])

    def record_samples_batch(self, samples: list[RequestSample]) -> None:
        if not samples:
            return
        rows = [self._sample_row(sample) for sample in samples]
        placeholders = ", ".join("?" for _ in _REQUEST_SAMPLE_COLUMNS)
        with self._lock:
            self._connection.executemany(
                f"""
                INSERT INTO request_samples(
                    {", ".join(_REQUEST_SAMPLE_COLUMNS)}
                )
                VALUES ({placeholders})
                """,
                rows,
            )
            self._connection.commit()

    def record_error(
        self,
        run_id: str,
        classified: ClassifiedError,
        endpoint_id: str,
        when: str,
    ) -> None:
        signature = classified.signature(endpoint_id)
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO errors(
                    run_id, signature, category, endpoint_id, message, first_seen, last_seen, count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(run_id, signature) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    count = errors.count + 1
                """,
                (
                    run_id,
                    signature,
                    classified.category,
                    endpoint_id,
                    classified.message,
                    when,
                    when,
                ),
            )
            self._connection.commit()

    def add_artifact(self, run_id: str, kind: str, path: str, created_at: str) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO artifacts(run_id, kind, path, created_at) VALUES (?, ?, ?, ?)",
                (run_id, kind, path, created_at),
            )
            self._connection.commit()

    def record_row_detail(self, run_id: str, row_number: int, detail: dict) -> None:
        self.record_row_details_batch(run_id, [(row_number, detail)])

    def record_row_details_batch(self, run_id: str, items: list[tuple[int, dict]]) -> None:
        """Persists the Live results master-detail payload (see
        DataRunner._build_executed_row_detail) so the History tab's row
        inspector works after an application restart, not only within the
        run that produced it."""
        if not items:
            return
        rows = [(run_id, row_number, json.dumps(detail)) for row_number, detail in items]
        with self._lock:
            self._connection.executemany(
                "INSERT OR REPLACE INTO row_details(run_id, row_number, detail_json) VALUES (?, ?, ?)",
                rows,
            )
            self._connection.commit()

    def get_row_detail(self, run_id: str, row_number: int) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT detail_json FROM row_details WHERE run_id = ? AND row_number = ?",
                (run_id, row_number),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def record_snapshot(self, run_id: str, seq: int, elapsed_seconds: float, snapshot: dict) -> None:
        """Stores one periodic metrics snapshot (load tests publish ~1/s)."""
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO run_snapshots(run_id, seq, elapsed_seconds, snapshot_json) VALUES (?, ?, ?, ?)",
                (run_id, seq, float(elapsed_seconds), json.dumps(snapshot)),
            )
            self._connection.commit()

    def list_snapshots(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT snapshot_json FROM run_snapshots WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def save_report(self, run_id: str, report: dict) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO run_reports(run_id, report_json) VALUES (?, ?)",
                (run_id, json.dumps(report)),
            )
            self._connection.commit()

    def get_report(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT report_json FROM run_reports WHERE run_id = ?", (run_id,)
            ).fetchone()
        return None if row is None else json.loads(row[0])

    @property
    def path(self) -> Path:
        return self._path

    def size_bytes(self) -> int:
        return store_file_size(self._path)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # -- reads (run-history browsing) -----------------------------------
    def list_runs(self, *, kind: str | None = None, limit: int = 200) -> list[dict]:
        """Most-recent-first summary rows for the history browser."""
        query = "SELECT run_id, kind, name, environment_id, environment_name, started_at, finished_at, outcome, definition_json FROM runs"
        params: tuple[object, ...] = ()
        if kind is not None:
            query += " WHERE kind = ?"
            params = (kind,)
        query += " ORDER BY started_at DESC LIMIT ?"
        params = (*params, limit)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [self._run_row_to_dict(row) for row in rows]

    def get_run(self, run_id: str) -> dict | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT run_id, kind, name, environment_id, environment_name, started_at, finished_at, outcome, definition_json "
                "FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return None if row is None else self._run_row_to_dict(row)

    def count_by_outcome(self, run_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT outcome, COUNT(*) FROM request_samples WHERE run_id = ? GROUP BY outcome",
                (run_id,),
            ).fetchall()
        return {outcome: count for outcome, count in rows}

    def list_samples(self, run_id: str, *, limit: int | None = None, offset: int = 0) -> list[dict]:
        query = f"SELECT {', '.join(_REQUEST_SAMPLE_COLUMNS)} FROM request_samples WHERE run_id = ? ORDER BY id ASC"
        params: tuple[object, ...] = (run_id,)
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            params = (*params, limit, offset)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
        return [dict(zip(_REQUEST_SAMPLE_COLUMNS, row)) for row in rows]

    def sample_aggregates(
        self,
        run_id: str,
        latency_edges_ms: tuple[float, ...] = (100, 250, 500, 1000, 2000, 5000),
        *,
        error_sample_limit: int = 50,
    ) -> dict | None:
        """Report aggregates computed in SQL, so large runs are never loaded into memory.

        Returns ``None`` when the run recorded no request samples. Offsets are
        seconds from the start of the run.
        """
        with self._lock:
            connection = self._connection
            total = connection.execute(
                "SELECT COUNT(*) FROM request_samples WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            if not total:
                return None
            edges = [float(edge) for edge in latency_edges_ms]
            bucket_columns = [
                f"SUM(CASE WHEN http_ms >= {low} AND http_ms < {high} THEN 1 ELSE 0 END)"
                for low, high in zip([0.0, *edges], edges)
            ]
            bucket_columns.append(f"SUM(CASE WHEN http_ms >= {edges[-1] if edges else 0.0} THEN 1 ELSE 0 END)")
            bucket_row = connection.execute(
                f"SELECT {', '.join(bucket_columns)} "
                "FROM request_samples WHERE run_id = ? AND http_ms IS NOT NULL AND outcome != 'cancelled'",
                (run_id,),
            ).fetchone()
            status_rows = connection.execute(
                "SELECT status_code, COUNT(*), MIN(offset_ms), MAX(offset_ms) FROM request_samples "
                "WHERE run_id = ? GROUP BY status_code ORDER BY COUNT(*) DESC",
                (run_id,),
            ).fetchall()
            category_rows = connection.execute(
                "SELECT error_category, COUNT(*), MIN(offset_ms), MAX(offset_ms) FROM request_samples "
                "WHERE run_id = ? AND error_category IS NOT NULL AND error_category != '' "
                "GROUP BY error_category ORDER BY COUNT(*) DESC",
                (run_id,),
            ).fetchall()
            queue_row = connection.execute(
                "SELECT MIN(queue_delay_ms), AVG(queue_delay_ms), MAX(queue_delay_ms) FROM request_samples "
                "WHERE run_id = ? AND queue_delay_ms IS NOT NULL",
                (run_id,),
            ).fetchone()
            size_row = connection.execute(
                "SELECT MIN(response_bytes), AVG(response_bytes), MAX(response_bytes), SUM(response_bytes) "
                "FROM request_samples WHERE run_id = ? AND response_bytes IS NOT NULL",
                (run_id,),
            ).fetchone()
            retries = connection.execute(
                "SELECT COALESCE(SUM(retry_count), 0) FROM request_samples WHERE run_id = ?", (run_id,)
            ).fetchone()[0]
            error_rows = connection.execute(
                "SELECT offset_ms, status_code, error_category, error_message, http_ms FROM request_samples "
                "WHERE run_id = ? AND outcome IN ('failed', 'error') ORDER BY id ASC LIMIT ?",
                (run_id, int(error_sample_limit)),
            ).fetchall()

        def seconds(value: float | None) -> float | None:
            return None if value is None else float(value) / 1000.0

        return {
            "total": int(total),
            "latency_edges_ms": list(latency_edges_ms),
            "latency_buckets": [int(value or 0) for value in bucket_row],
            "status_counts": [
                {"status_code": code, "count": int(count), "first_seconds": seconds(first), "last_seconds": seconds(last)}
                for code, count, first, last in status_rows
            ],
            "categories": [
                {"category": category, "count": int(count), "first_seconds": seconds(first), "last_seconds": seconds(last)}
                for category, count, first, last in category_rows
            ],
            "queue_delay_ms": {"min": queue_row[0], "avg": queue_row[1], "max": queue_row[2]},
            "response_bytes": {"min": size_row[0], "avg": size_row[1], "max": size_row[2], "total": size_row[3]},
            "retries": int(retries or 0),
            "error_samples": [
                {
                    "offset_seconds": seconds(offset),
                    "status_code": status,
                    "category": category or "",
                    "message": message or "",
                    "http_ms": http_ms,
                }
                for offset, status, category, message, http_ms in error_rows
            ],
        }

    def list_errors(self, run_id: str) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT signature, category, endpoint_id, message, first_seen, last_seen, count "
                "FROM errors WHERE run_id = ? ORDER BY count DESC",
                (run_id,),
            ).fetchall()
        columns = ("signature", "category", "endpoint_id", "message", "first_seen", "last_seen", "count")
        return [dict(zip(columns, row)) for row in rows]

    @staticmethod
    def _run_row_to_dict(row: tuple) -> dict:
        (
            run_id,
            kind,
            name,
            environment_id,
            environment_name,
            started_at,
            finished_at,
            outcome,
            definition_json,
        ) = row
        return {
            "run_id": run_id,
            "kind": kind,
            "name": name,
            "environment_id": environment_id,
            "environment_name": environment_name,
            "started_at": started_at,
            "finished_at": finished_at,
            "outcome": outcome,
            "definition": json.loads(definition_json) if definition_json else {},
        }

    @staticmethod
    def _sample_row(sample: RequestSample) -> tuple[object, ...]:
        data = sample.to_dict()
        return tuple(data.get(column) for column in _REQUEST_SAMPLE_COLUMNS)
