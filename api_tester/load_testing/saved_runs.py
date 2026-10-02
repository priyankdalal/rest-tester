"""Reopening persisted Load Studio runs.

:func:`load_saved_run` reads everything a run left in a
:class:`~api_tester.execution.persistence.RunStore` file into a plain
:class:`LoadedRun` (no Qt), and :class:`SavedRunPickerDialog` lets the user
choose one run when a file holds several.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from api_tester.execution.persistence import RunStore, store_file_size
from api_tester.load_testing.engine import LoadRunSummary
from api_tester.load_testing.run_stats import snapshots_from_samples


class SavedRunError(Exception):
    """A file that cannot be opened as a Load Studio run store."""


@dataclass
class LoadedRun:
    path: Path
    run: dict[str, Any]
    definition: dict[str, Any]
    summary: LoadRunSummary | None
    snapshots: list[dict[str, Any]]
    final_snapshot: dict[str, Any]
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    #: ``True`` when metrics were rebuilt from request samples (older files).
    reconstructed: bool = False

    @property
    def run_id(self) -> str:
        return str(self.run.get("run_id", ""))

    @property
    def outcome(self) -> str:
        if self.summary is not None:
            return self.summary.outcome
        return str(self.run.get("outcome") or "INCOMPLETE")


def format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def format_timestamp(value: str | None) -> str:
    """``2025-05-01T10:15:00+00:00`` → ``2025-05-01 10:15`` in local time."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M")


def run_duration_seconds(run: dict[str, Any]) -> float | None:
    try:
        started = datetime.fromisoformat(str(run.get("started_at")))
        finished = datetime.fromisoformat(str(run.get("finished_at")))
    except (TypeError, ValueError):
        return None
    return max(0.0, (finished - started).total_seconds())


def _open_store(path: Path) -> RunStore:
    if not path.is_file():
        raise SavedRunError(f"{path} does not exist.")
    try:
        return RunStore(path)
    except sqlite3.DatabaseError as exc:
        raise SavedRunError(f"{path.name} is not a Rest Tester run file ({exc}).") from exc


def list_saved_runs(path: str | Path) -> list[dict[str, Any]]:
    """The load-test runs in ``path``, most recent first."""
    store = _open_store(Path(path))
    try:
        return store.list_runs(kind="load_test")
    except sqlite3.DatabaseError as exc:
        raise SavedRunError(f"{Path(path).name} is not a Rest Tester run file ({exc}).") from exc
    finally:
        store.close()


def load_saved_run(path: str | Path, run_id: str) -> LoadedRun:
    path = Path(path)
    store = _open_store(path)
    try:
        run = store.get_run(run_id)
        if run is None or run.get("kind") != "load_test":
            raise SavedRunError(f"Run {run_id} is not a load test in {path.name}.")
        report = store.get_report(run_id) or {}
        snapshots = store.list_snapshots(run_id)
        final_snapshot = dict(report.get("final_snapshot") or {})
        reconstructed = False
        if not snapshots:
            rebuilt, rebuilt_final = snapshots_from_samples(store.list_samples(run_id))
            snapshots = rebuilt
            final_snapshot = final_snapshot or rebuilt_final
            reconstructed = bool(rebuilt)
        if not final_snapshot and snapshots:
            final_snapshot = dict(snapshots[-1])
        summary = LoadRunSummary.from_dict(report["summary"]) if report.get("summary") else None
        return LoadedRun(
            path=path,
            run=run,
            definition=dict(run.get("definition") or {}),
            summary=summary,
            snapshots=snapshots,
            final_snapshot=final_snapshot,
            errors=store.list_errors(run_id),
            warnings=list(report.get("warnings") or []),
            reconstructed=reconstructed,
        )
    except sqlite3.DatabaseError as exc:
        raise SavedRunError(f"{path.name} is not a Rest Tester run file ({exc}).") from exc
    finally:
        store.close()


def describe_store(path: str | Path) -> str:
    """``"1.2 MB"`` for an existing file, ``"new file"`` otherwise."""
    file_path = Path(path)
    if not file_path.is_file():
        return "new file"
    return format_bytes(store_file_size(file_path))


class SavedRunPickerDialog(QDialog):
    """Lists the load-test runs in one store file; the selected run is :attr:`selected_run_id`."""

    COLUMNS = ("Started", "Scenario", "Environment", "Outcome", "Peak users", "Duration")

    def __init__(self, path: str | Path, runs: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("savedRunPickerDialog")
        self.setWindowTitle("Open saved load run")
        self.resize(760, 380)
        self._runs = runs
        layout = QVBoxLayout(self)
        intro = QLabel(f"{Path(path).name} holds {len(runs)} load runs. Choose one to open.")
        intro.setToolTip(str(path))
        layout.addWidget(intro)

        self.table = QTableWidget(len(runs), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for row, run in enumerate(runs):
            definition = run.get("definition") or {}
            duration = run_duration_seconds(run)
            cells = (
                format_timestamp(run.get("started_at")),
                str(run.get("name") or definition.get("endpoint_id") or run.get("run_id")),
                str(run.get("environment_name") or "—"),
                str(run.get("outcome") or "INCOMPLETE"),
                str(definition.get("peak_users", "—")),
                f"{duration:.0f}s" if duration is not None else "—",
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, run.get("run_id"))
                    item.setToolTip(str(run.get("run_id")))
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        if runs:
            self.table.selectRow(0)
        self.table.doubleClicked.connect(lambda _index: self.accept())
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Open | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def selected_run_id(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)
