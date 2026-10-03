"""Persistent, metadata-only AI call ledger. No prompts, keys or responses."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from .config import AiSettings
from .provider import ChatMessage, LlmError, LlmProvider, LlmResponse, LlmResponseError, ToolSpec

USAGE_PATH = Path(__file__).resolve().parents[2] / "data" / "ai_usage.db"
SESSION_ID = uuid.uuid4().hex


class UsageStore:
    def __init__(self, path: Path = USAGE_PATH) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS ai_calls (
                id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, session_id TEXT NOT NULL,
                generation_id TEXT NOT NULL, connection_id TEXT NOT NULL, connection_name TEXT NOT NULL,
                provider TEXT NOT NULL, model TEXT NOT NULL, kind TEXT NOT NULL,
                purpose TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
                duration_ms INTEGER NOT NULL, status TEXT NOT NULL, plan_ready INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS ai_calls_timestamp ON ai_calls(timestamp)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def record(self, settings: AiSettings, generation_id: str, kind: str, purpose: str,
               response: LlmResponse | None, duration_ms: int, status: str) -> None:
        input_tokens = output_tokens = None
        if response is not None:
            if response.extra.get("input_tokens_reported", True):
                input_tokens = response.input_tokens
            if response.extra.get("output_tokens_reported", True):
                output_tokens = response.output_tokens
        with closing(self._connect()) as db, db:
            db.execute("""INSERT INTO ai_calls
                (timestamp,session_id,generation_id,connection_id,connection_name,provider,model,
                 kind,purpose,input_tokens,output_tokens,duration_ms,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                datetime.now(timezone.utc).isoformat(), SESSION_ID, generation_id,
                settings.active_connection_id, settings.connection_name or settings.provider,
                settings.provider, response.model if response and response.model else settings.planner_model,
                kind, purpose, input_tokens, output_tokens, duration_ms, status,
            ))

    def mark_plan_ready(self, generation_id: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("UPDATE ai_calls SET plan_ready=1 WHERE generation_id=?", (generation_id,))

    def rows(self, *, since: datetime | None = None, session_id: str = "") -> list[dict[str, Any]]:
        conditions, values = [], []
        if since is not None:
            conditions.append("timestamp >= ?")
            values.append(since.astimezone(timezone.utc).isoformat())
        if session_id:
            conditions.append("session_id = ?")
            values.append(session_id)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM ai_calls" + where + " ORDER BY timestamp DESC,id DESC", values
            )]


class TrackedProvider:
    """Record every attempted model call, including failures and discarded results."""

    def __init__(self, provider: LlmProvider, settings: AiSettings, kind: str,
                 store: UsageStore | None = None) -> None:
        self.provider = provider
        self.settings = settings
        self.kind = kind
        self.store = store or UsageStore()
        self.name = provider.name
        self.generation_id = uuid.uuid4().hex
        self.calls = 0

    def complete(self, messages: list[ChatMessage], *, model: str,
                 tools: list[ToolSpec] | None = None, response_schema: dict[str, Any] | None = None,
                 temperature: float = 0.0, max_output_tokens: int = 4000,
                 timeout_seconds: float = 60.0) -> LlmResponse:
        self.calls += 1
        purpose = ("Connection check" if self.kind == "connection" else
                   "Initial plan" if self.calls == 1 else f"Repair / refinement {self.calls - 1}")
        started = perf_counter()
        try:
            response = self.provider.complete(
                messages, model=model, tools=tools, response_schema=response_schema,
                temperature=temperature, max_output_tokens=max_output_tokens,
                timeout_seconds=timeout_seconds,
            )
        except LlmError as exc:
            diagnostics = exc.diagnostics if isinstance(exc, LlmResponseError) else None
            self.store.record(self.settings, self.generation_id, self.kind, purpose, diagnostics,
                              int((perf_counter() - started) * 1000), type(exc).__name__)
            raise
        self.store.record(self.settings, self.generation_id, self.kind, purpose, response,
                          int((perf_counter() - started) * 1000), "Completed")
        return response

    def check_connection(self, model: str, timeout_seconds: float = 10.0):
        return self.provider.check_connection(model, timeout_seconds)
