"""Settings usage overview: app-recorded consumption, not provider billing."""

from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .usage_store import SESSION_ID, UsageStore


def totals_text(rows: list[dict], field: str) -> str:
    known = sum(row[field] for row in rows if row[field] is not None)
    unknown = sum(row[field] is None for row in rows)
    if unknown == len(rows) and rows:
        return "Not reported"
    return f"{known:,}" + (f" + {unknown} unreported" if unknown else "")


class UsageDashboard(QWidget):
    def __init__(self, parent=None, *, store: UsageStore | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.rows: list[dict] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        title = QLabel("AI Usage")
        title.setProperty("pageTitle", True)
        layout.addWidget(title)
        hint = QLabel("Recorded usage inside Rest Tester, grouped by connection. "
                      "This is not your provider's account-wide billing or context capacity.")
        hint.setWordWrap(True)
        hint.setProperty("pageDescription", True)
        layout.addWidget(hint)
        toolbar = QHBoxLayout()
        self.period = QComboBox()
        self.period.addItems(["Today", "Last 7 days", "Last 30 days", "This session", "All recorded"])
        self.period.currentIndexChanged.connect(self.refresh)
        toolbar.addWidget(self.period)
        toolbar.addStretch()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        toolbar.addWidget(refresh)
        self.export_button = QPushButton("Export usage")
        self.export_button.clicked.connect(self.export)
        toolbar.addWidget(self.export_button)
        layout.addLayout(toolbar)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setProperty("sectionTitle", True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 7)
        self.table.setObjectName("environmentTable")
        self.table.setHorizontalHeaderLabels(["Connection", "Provider", "Input", "Output", "Total", "Calls", "Ready plans"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)
        note = QLabel(
            "Includes request/suite planning, repairs, and hosted connection checks (including startup checks). "
            "Ollama model-list checks consume no generation tokens and are excluded. Failed calls may have "
            "unreported usage; known totals are shown as partial when necessary.\n"
            "Local providers have no cloud API charge. Hosted cost is not estimated without pricing. "
            "Only metadata is saved—no prompts, API keys, endpoint URLs, or response content. "
            "Historical records remain after deleting a connection. Ready plans counts validated plans, not API runs."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def refresh(self, _index=None) -> None:
        self.status.clear()
        try:
            if self.store is None:
                self.store = UsageStore()
            now = datetime.now().astimezone()
            period = self.period.currentText()
            since = now.replace(hour=0, minute=0, second=0, microsecond=0) if period == "Today" else None
            if period in {"Last 7 days", "Last 30 days"}:
                since = now - timedelta(days=7 if period == "Last 7 days" else 30)
            self.rows = self.store.rows(since=since, session_id=SESSION_ID if period == "This session" else "")
        except (OSError, sqlite3.Error) as exc:
            self.rows = []
            self.table.setRowCount(0)
            self.summary.setText("Usage unavailable")
            self.export_button.setEnabled(False)
            self.status.setText(f"Could not read usage history ({type(exc).__name__}).")
            return
        for row in self.rows:
            row["total_tokens"] = (row["input_tokens"] + row["output_tokens"]
                                   if row["input_tokens"] is not None and row["output_tokens"] is not None else None)
        ready = {row["generation_id"] for row in self.rows if row["plan_ready"]}
        self.summary.setText(f"Total tokens: {totals_text(self.rows, 'total_tokens')}   |   "
                             f"Model calls: {len(self.rows):,}   |   Ready plans: {len(ready):,}")
        groups = defaultdict(list)
        for row in self.rows:
            groups[(row["connection_id"], row["provider"])].append(row)
        self.table.setRowCount(len(groups))
        for index, records in enumerate(groups.values()):
            first = records[0]
            values = [first["connection_name"], first["provider"],
                      totals_text(records, "input_tokens"), totals_text(records, "output_tokens"),
                      totals_text(records, "total_tokens"), str(len(records)),
                      str(len({row["generation_id"] for row in records if row["plan_ready"]}))]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value + "\nModels: " + ", ".join(sorted({row["model"] for row in records})))
                self.table.setItem(index, column, item)
        self.export_button.setEnabled(bool(self.rows))
        if not self.rows:
            self.status.setText("No recorded model calls in this period. Usage recording starts with this version.")

    def export(self) -> None:
        if not self.rows:
            self.status.setText("No usage records to export. Refresh or choose a different period.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export AI usage", "ai-usage.csv", "CSV (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
                writer.writeheader()
                for row in self.rows:
                    # Neutralize user-controlled connection/model names in spreadsheet cells.
                    writer.writerow({key: "'" + value if isinstance(value, str) and
                                     value.lstrip().startswith(("=", "+", "-", "@")) else value
                                     for key, value in row.items()})
        except OSError as exc:
            self.status.setText(f"Could not export usage ({type(exc).__name__}).")
            return
        self.status.setText(f"Exported {len(self.rows):,} model-call records.")
