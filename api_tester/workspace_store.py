"""The workspace database: run history, suite history, and saved requests.

These three used to be flat files - two append-only ``.jsonl`` logs and a
whole-document ``.json`` rewrite. All three had the same problems:

* the history logs grew without bound, and reading one endpoint's history
  meant parsing every line ever written;
* a single malformed line hid the history for every endpoint;
* rewriting the saved-requests document in place could lose every
  collection if the process died mid-write.

One SQLite file fixes all three: indexed lookups, per-row durability, and
cheap deletes so retention is actually enforceable.

Per-execution results are deliberately **not** stored here. The Data Runner
and Load Testing Studio keep writing one
:class:`~api_tester.execution.persistence.RunStore` database per run, so a
run stays a single portable artifact the user can archive, share, and load
back into the app. Retention never touches those files.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Union

#: History older than this is removed when the database is opened. Saved
#: requests and collections are user-authored, so they are never expired.
RETENTION_DAYS = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id TEXT NOT NULL,
    passed INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    elapsed_ms REAL,
    url TEXT,
    environment TEXT,
    timestamp TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_history_endpoint
    ON run_history(endpoint_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_run_history_timestamp
    ON run_history(timestamp);

CREATE TABLE IF NOT EXISTS suite_history(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suite TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    total INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0,
    failed INTEGER DEFAULT 0,
    errored INTEGER DEFAULT 0,
    skipped INTEGER DEFAULT 0,
    duration_ms REAL DEFAULT 0,
    cleanup_failed INTEGER DEFAULT 0,
    cleanup_errored INTEGER DEFAULT 0,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_suite_history_suite
    ON suite_history(suite, recorded_at);
CREATE INDEX IF NOT EXISTS idx_suite_history_recorded
    ON suite_history(recorded_at);

CREATE TABLE IF NOT EXISTS saved_requests(
    id TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL,
    name TEXT NOT NULL,
    values_json TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT,
    expected_status TEXT DEFAULT '200-299',
    authentication TEXT DEFAULT 'inherit',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS request_collections(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT,
    updated_at TEXT
);

-- A collection is an ordered list that may repeat a request, so the
-- position is part of the key rather than the request id.
CREATE TABLE IF NOT EXISTS collection_items(
    collection_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    PRIMARY KEY (collection_id, position)
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


class WorkspaceStore:
    """The single database behind run history, suite history, and saved requests.

    One connection guarded by a lock, matching
    :class:`~api_tester.execution.persistence.RunStore`: suite runs and the
    request worker write from Qt worker threads, so ``check_same_thread`` is
    disabled and every statement is serialised here instead.
    """

    def __init__(
        self, path: Union[str, Path], *, retention_days: int = RETENTION_DAYS
    ) -> None:
        self.path = Path(path)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self.initialize()
        self.apply_retention()

    def initialize(self) -> None:
        with self._lock:
            self._connection.executescript(_SCHEMA)
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    # ---------------------------------------------------------- retention

    def apply_retention(self, *, days: Optional[int] = None) -> int:
        """Deletes history older than the retention window.

        Only the two history tables are swept. Saved requests and
        collections are user-authored and never expire, and per-execution
        run databases live in their own files this never opens.
        """
        limit = self.retention_days if days is None else days
        if limit <= 0:
            return 0
        cutoff = _cutoff(limit)
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM run_history WHERE timestamp < ?", (cutoff,)
            )
            removed = cursor.rowcount or 0
            cursor = self._connection.execute(
                "DELETE FROM suite_history WHERE recorded_at < ?", (cutoff,)
            )
            removed += cursor.rowcount or 0
            self._connection.commit()
        return removed

    # -------------------------------------------------------- run history

    def append_run_history(
        self,
        endpoint_id: str,
        *,
        passed: bool = False,
        status_code: Optional[int] = None,
        elapsed_ms: float = 0.0,
        url: str = "",
        environment: str = "",
        timestamp: str = "",
    ) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO run_history(endpoint_id, passed, status_code,"
                " elapsed_ms, url, environment, timestamp)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    str(endpoint_id),
                    1 if passed else 0,
                    status_code,
                    elapsed_ms,
                    url,
                    environment,
                    timestamp or _now(),
                ),
            )
            self._connection.commit()

    def endpoint_history(self, endpoint_id: str, *, limit: int = 100) -> list[dict]:
        """The newest ``limit`` entries for one endpoint, newest first.

        Indexed on ``(endpoint_id, timestamp)``, so this no longer depends
        on how much total history exists.
        """
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM run_history WHERE endpoint_id = ?"
                " ORDER BY timestamp DESC, id DESC LIMIT ?",
                (str(endpoint_id), int(limit)),
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def run_history_count(self) -> int:
        with self._lock:
            return int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM run_history"
                ).fetchone()[0]
            )

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        return {
            "endpoint_id": row["endpoint_id"],
            "passed": bool(row["passed"]),
            "status_code": row["status_code"],
            "elapsed_ms": row["elapsed_ms"],
            "url": row["url"] or "",
            "environment": row["environment"] or "",
            "timestamp": row["timestamp"] or "",
        }

    # ------------------------------------------------------ suite history

    def append_suite_history(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._connection.execute(
                "INSERT INTO suite_history(suite, started_at, finished_at, total,"
                " passed, failed, errored, skipped, duration_ms, cleanup_failed,"
                " cleanup_errored, recorded_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(record.get("suite") or ""),
                    record.get("started_at") or "",
                    record.get("finished_at") or "",
                    int(record.get("total") or 0),
                    int(record.get("passed") or 0),
                    int(record.get("failed") or 0),
                    int(record.get("errored") or 0),
                    int(record.get("skipped") or 0),
                    float(record.get("duration_ms") or 0.0),
                    int(record.get("cleanup_failed") or 0),
                    int(record.get("cleanup_errored") or 0),
                    str(record.get("recorded_at") or _now()),
                ),
            )
            self._connection.commit()

    def suite_history(self, *, suite: str = "", limit: int = 200) -> list[dict]:
        """Recent suite runs, newest first, optionally for one suite."""
        query = "SELECT * FROM suite_history"
        parameters: list[Any] = []
        if suite:
            query += " WHERE suite = ?"
            parameters.append(suite)
        query += " ORDER BY recorded_at DESC, id DESC LIMIT ?"
        parameters.append(int(limit))
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------ saved requests

    def load_saved_requests(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM saved_requests ORDER BY name COLLATE NOCASE"
            ).fetchall()
        records = []
        for row in rows:
            records.append(
                {
                    "id": row["id"],
                    "endpoint_id": row["endpoint_id"],
                    "name": row["name"],
                    "values": json.loads(row["values_json"] or "{}"),
                    "payload": json.loads(row["payload_json"])
                    if row["payload_json"] is not None
                    else None,
                    "expected_status": row["expected_status"] or "200-299",
                    "authentication": row["authentication"] or "inherit",
                    "created_at": row["created_at"] or "",
                    "updated_at": row["updated_at"] or "",
                }
            )
        return records

    def upsert_saved_request(self, record: dict[str, Any]) -> None:
        payload = record.get("payload")
        with self._lock:
            self._connection.execute(
                "INSERT INTO saved_requests(id, endpoint_id, name, values_json,"
                " payload_json, expected_status, authentication, created_at,"
                " updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " endpoint_id=excluded.endpoint_id, name=excluded.name,"
                " values_json=excluded.values_json,"
                " payload_json=excluded.payload_json,"
                " expected_status=excluded.expected_status,"
                " authentication=excluded.authentication,"
                " updated_at=excluded.updated_at",
                (
                    str(record["id"]),
                    str(record.get("endpoint_id") or ""),
                    str(record.get("name") or ""),
                    json.dumps(record.get("values") or {}, ensure_ascii=False),
                    None if payload is None else json.dumps(payload, ensure_ascii=False),
                    str(record.get("expected_status") or "200-299"),
                    str(record.get("authentication") or "inherit"),
                    str(record.get("created_at") or _now()),
                    str(record.get("updated_at") or _now()),
                ),
            )
            self._connection.commit()

    def delete_saved_request(self, request_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM saved_requests WHERE id = ?", (str(request_id),)
            )
            self._connection.execute(
                "DELETE FROM collection_items WHERE request_id = ?", (str(request_id),)
            )
            self._connection.commit()
        self._compact_collections()

    # --------------------------------------------------------- collections

    def load_collections(self) -> list[dict]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM request_collections ORDER BY name COLLATE NOCASE"
            ).fetchall()
            items = self._connection.execute(
                "SELECT collection_id, request_id FROM collection_items"
                " ORDER BY collection_id, position"
            ).fetchall()
        ordered: dict[str, list[str]] = {}
        for item in items:
            ordered.setdefault(item["collection_id"], []).append(item["request_id"])
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "request_ids": ordered.get(row["id"], []),
                "created_at": row["created_at"] or "",
                "updated_at": row["updated_at"] or "",
            }
            for row in rows
        ]

    def upsert_collection(self, record: dict[str, Any]) -> None:
        collection_id = str(record["id"])
        request_ids = [str(item) for item in record.get("request_ids") or []]
        with self._lock:
            self._connection.execute(
                "INSERT INTO request_collections(id, name, created_at, updated_at)"
                " VALUES(?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                " updated_at=excluded.updated_at",
                (
                    collection_id,
                    str(record.get("name") or "Collection"),
                    str(record.get("created_at") or _now()),
                    str(record.get("updated_at") or _now()),
                ),
            )
            # Rewriting the membership wholesale keeps position contiguous
            # and preserves duplicates, which a keyed upsert could not.
            self._connection.execute(
                "DELETE FROM collection_items WHERE collection_id = ?",
                (collection_id,),
            )
            self._connection.executemany(
                "INSERT INTO collection_items(collection_id, position, request_id)"
                " VALUES(?, ?, ?)",
                [
                    (collection_id, position, request_id)
                    for position, request_id in enumerate(request_ids)
                ],
            )
            self._connection.commit()

    def delete_collection(self, collection_id: str) -> None:
        with self._lock:
            self._connection.execute(
                "DELETE FROM request_collections WHERE id = ?", (str(collection_id),)
            )
            self._connection.execute(
                "DELETE FROM collection_items WHERE collection_id = ?",
                (str(collection_id),),
            )
            self._connection.commit()

    def _compact_collections(self) -> None:
        """Closes positional gaps left by deleting a request."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT collection_id, request_id FROM collection_items"
                " ORDER BY collection_id, position"
            ).fetchall()
            ordered: dict[str, list[str]] = {}
            for row in rows:
                ordered.setdefault(row["collection_id"], []).append(row["request_id"])
            self._connection.execute("DELETE FROM collection_items")
            self._connection.executemany(
                "INSERT INTO collection_items(collection_id, position, request_id)"
                " VALUES(?, ?, ?)",
                [
                    (collection_id, position, request_id)
                    for collection_id, request_ids in ordered.items()
                    for position, request_id in enumerate(request_ids)
                ],
            )
            self._connection.commit()


def as_store(source: Union["WorkspaceStore", str, Path]) -> "WorkspaceStore":
    """Accepts an open store or a database path.

    Lets the UI share one store while tests and one-off tooling can still
    name a throwaway file.
    """
    if isinstance(source, WorkspaceStore):
        return source
    return WorkspaceStore(source)


# ------------------------------------------------------------- migration


def migrate_legacy_files(
    store: WorkspaceStore,
    *,
    run_history: Optional[Path] = None,
    suite_history: Optional[Path] = None,
    saved_requests: Optional[Path] = None,
) -> dict[str, int]:
    """Imports the pre-SQLite files once, then renames them to ``.migrated``.

    The originals are renamed rather than deleted so a bad import can be
    rolled back by hand. A file that is already renamed is skipped, which
    makes this safe to call on every start.
    """
    imported = {"run_history": 0, "suite_history": 0, "saved_requests": 0}

    if run_history is not None and run_history.exists():
        for record in _read_jsonl(run_history):
            store.append_run_history(
                str(record.get("endpoint_id") or ""),
                passed=bool(record.get("passed")),
                status_code=record.get("status_code"),
                elapsed_ms=float(record.get("elapsed_ms") or 0.0),
                url=str(record.get("url") or ""),
                environment=str(record.get("environment") or ""),
                timestamp=str(record.get("timestamp") or _now()),
            )
            imported["run_history"] += 1
        _retire(run_history)

    if suite_history is not None and suite_history.exists():
        for record in _read_jsonl(suite_history):
            payload = dict(record)
            # Legacy rows have no recorded_at; the run's own finish time is
            # the honest basis for retention rather than the import time.
            payload["recorded_at"] = str(
                record.get("finished_at") or record.get("started_at") or _now()
            )
            store.append_suite_history(payload)
            imported["suite_history"] += 1
        _retire(suite_history)

    if saved_requests is not None and saved_requests.exists():
        try:
            document = json.loads(saved_requests.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = {}
        for record in document.get("requests", []) or []:
            if record.get("id"):
                store.upsert_saved_request(record)
                imported["saved_requests"] += 1
        for record in document.get("collections", []) or []:
            if record.get("id"):
                store.upsert_collection(record)
        _retire(saved_requests)

    return imported


def _read_jsonl(path: Path) -> list[dict]:
    """Reads a JSONL log, skipping lines that were never valid JSON."""
    records: list[dict] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        return []
    return records


def _retire(path: Path) -> None:
    target = path.with_suffix(path.suffix + ".migrated")
    try:
        if target.exists():
            target.unlink()
        path.rename(target)
    except OSError:
        pass
