"""Usage strip and on-demand details for one Ask AI generation."""

import json
from dataclasses import asdict

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QVBoxLayout,
)
from .. import theme
from ..icons import icon

from .usage import CallUsage, token_text, usage_total


class UsageStrip(QFrame):
    def __init__(self, parent=None, *, compact: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("aiUsageCompact" if compact else "settingsCard")
        self.calls: list[CallUsage] = []
        self.connection = ""
        self.provider = ""
        layout = QHBoxLayout(self)
        if compact:
            layout.setContentsMargins(0, 0, 0, 0)
        self.metrics: dict[str, QLabel] = {}
        for name in ("Input tokens", "Output tokens", "Total tokens", "Time / calls"):
            column = QVBoxLayout()
            title = QLabel(name)
            title.setProperty("pageDescription", True)
            column.addWidget(title)
            value = QLabel()
            value.setProperty("sectionTitle", True)
            column.addWidget(value)
            if compact:
                for label in (title, value):
                    label.setParent(self)
                    label.hide()
            else:
                layout.addLayout(column, 1)
            self.metrics[name] = value
        self.details_button = QPushButton("Token details" if compact else "Usage details")
        self.details_button.setCursor(Qt.CursorShape.PointingHandCursor)
        if compact:
            self.details_button.setIcon(icon("info-circle", theme.TEXT_MUTED, 16))
            self.details_button.setObjectName("aiTextAction")
        self.details_button.clicked.connect(self.show_details)
        layout.addWidget(self.details_button)
        self.setToolTip("Usage for this generation, including repairs. Not context-window utilization.")
        self.hide()

    def show_usage(self, calls: list[CallUsage], connection: str, provider: str) -> None:
        self.calls = list(calls)
        self.connection = connection
        self.provider = provider
        for name, field in (("Input tokens", "input_tokens"), ("Output tokens", "output_tokens"),
                            ("Total tokens", "total_tokens")):
            self.metrics[name].setText(token_text(usage_total(calls, field)))
        seconds = sum(call.duration_ms for call in calls) / 1000
        self.metrics["Time / calls"].setText(f"{seconds:.1f} s / {len(calls)}")
        self.show()

    def show_details(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("AI token usage - this plan")
        dialog.resize(820, 480)
        layout = QVBoxLayout(dialog)
        title = QLabel("Usage for this plan")
        title.setProperty("pageTitle", True)
        layout.addWidget(title)
        subtitle = QLabel(self.connection)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        summary = QLabel(
            f"Input: {token_text(usage_total(self.calls, 'input_tokens'))}   |   "
            f"Output: {token_text(usage_total(self.calls, 'output_tokens'))}   |   "
            f"Total: {token_text(usage_total(self.calls, 'total_tokens'))}"
        )
        layout.addWidget(summary)
        table = QTableWidget(len(self.calls), 7)
        table.setHorizontalHeaderLabels(["Call", "Purpose", "Input", "Output", "Total", "Time", "Status"])
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        for row, call in enumerate(self.calls):
            values = [str(row + 1), call.purpose, token_text(call.input_tokens),
                      token_text(call.output_tokens), token_text(call.total_tokens),
                      f"{call.duration_ms / 1000:.1f} s", call.status]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(f"{value}\nModel: {call.model}")
                table.setItem(row, column, item)
        layout.addWidget(table, 1)
        note = QLabel(
            ("Local provider: no cloud API charge. Hardware/electricity costs are not estimated."
             if self.provider == "ollama" else
             "Hosted cost is not estimated: model pricing has not been configured. Check provider billing.")
            + "\nOnly this generation is included; connection checks and previous prompts are excluded."
            + "\nNot reported means usage was unavailable; totals are not invented. Time is model-call wall time."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        footer = QHBoxLayout()
        copy = QPushButton("Copy usage")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(json.dumps({
            "connection": self.connection, "provider": self.provider,
            "calls": [asdict(call) for call in self.calls],
            "input_tokens": usage_total(self.calls, "input_tokens"),
            "output_tokens": usage_total(self.calls, "output_tokens"),
            "total_tokens": usage_total(self.calls, "total_tokens"),
        }, indent=2)))
        footer.addWidget(copy)
        footer.addStretch()
        done = QPushButton("Done")
        done.clicked.connect(dialog.accept)
        footer.addWidget(done)
        layout.addLayout(footer)
        dialog.exec()
