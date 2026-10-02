"""The auth inspector: what is being sent, and what happened recently.

Deliberately shows credential values unmasked. It is opened explicitly by
the user, renders from memory, and never writes what it shows to disk -
the redaction rules that protect exported cURL, run history, and suite
captures are untouched by anything here.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import auth_inspect, theme
from .auth_log import activity_log
from .icons import icon
from .widgets import attach_table_empty_state, form_caption


def _elapsed(seconds: float) -> str:
    if seconds < 0:
        return f"expired {_elapsed(-seconds)} ago"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


class AuthInspectorDialog(QDialog):
    """Live credential values, decoded claims, and the activity log."""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._visible_entries: list = []
        self._log = getattr(settings.manager, "activity_log", None)
        if self._log is None:
            self._log = activity_log
        self.setWindowTitle("Authentication inspector")
        self.setMinimumSize(820, 560)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        self.tabs.addTab(self._build_current_tab(), "Current credential")
        self.tabs.addTab(self._build_log_tab(), "Activity log")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._refresh_profiles()
        self._refresh_log()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_log)
        self._timer.start(2000)
        self.refresh_theme()

    # ---------------------------------------------------------- current

    def _build_current_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        picker = QHBoxLayout()
        picker.addWidget(QLabel("Profile"))
        self.profile_picker = QComboBox()
        self.profile_picker.currentIndexChanged.connect(lambda *_: self._inspect())
        picker.addWidget(self.profile_picker, 1)
        self.inspect_button = QPushButton("Inspect")
        self.inspect_button.setToolTip(
            "Resolve this profile's credential now. Never opens a browser."
        )
        self.inspect_button.clicked.connect(self._inspect)
        picker.addWidget(self.inspect_button)
        layout.addLayout(picker)

        self.status_label = QLabel("-")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addWidget(form_caption("Values sent with each request"))
        self.values_table = QTableWidget(0, 3)
        self.values_table.setHorizontalHeaderLabels(["Placement", "Name", "Value"])
        self.values_table.verticalHeader().setVisible(False)
        self.values_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        header = self.values_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.values_table, 1)
        attach_table_empty_state(
            self.values_table,
            icon_name="eye",
            title="No credential values yet",
            guidance="Sign in or send a request, then the headers, query values and cookies applied to it appear here.",
        )

        actions = QHBoxLayout()
        self.copy_button = QPushButton("Copy value")
        self.copy_button.setToolTip("Copy the selected credential value")
        self.copy_button.clicked.connect(self._copy_value)
        actions.addWidget(self.copy_button)
        actions.addStretch()
        layout.addLayout(actions)

        layout.addWidget(form_caption("Parameters and decoded claims"))
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        layout.addWidget(self.detail, 1)
        return page

    def _refresh_profiles(self) -> None:
        current = self.profile_picker.currentData()
        self.profile_picker.blockSignals(True)
        self.profile_picker.clear()
        for profile in self._settings.profiles.values():
            self.profile_picker.addItem(
                profile.name or profile.id, profile.id
            )
        if current:
            index = self.profile_picker.findData(current)
            if index >= 0:
                self.profile_picker.setCurrentIndex(index)
        self.profile_picker.blockSignals(False)
        self._inspect()

    def _selected_profile(self):
        profile_id = self.profile_picker.currentData()
        return self._settings.profiles.get(profile_id) if profile_id else None

    def _inspect(self) -> None:
        self.values_table.setRowCount(0)
        self.detail.setPlainText("")
        profile = self._selected_profile()
        if profile is None:
            self.status_label.setText("No authentication profiles are configured.")
            return
        snapshot = auth_inspect.inspect(
            self._settings.manager, self._settings.environment_id, profile
        )
        self._show_snapshot(snapshot)

    def _show_snapshot(self, snapshot: auth_inspect.CredentialSnapshot) -> None:
        state = f"{snapshot.method_label} - {snapshot.status}"
        if snapshot.error:
            state += f"\n{snapshot.error}"
        elif snapshot.per_request:
            state += (
                "\nThis method signs each request, so the value below was "
                "computed for a sample request and will differ per call."
            )
        self.status_label.setText(state)
        self.status_label.setStyleSheet(
            f"color: {theme.FAIL};" if snapshot.error else f"color: {theme.TEXT_MUTED};"
        )

        self.values_table.setRowCount(len(snapshot.values))
        for row, value in enumerate(snapshot.values):
            self.values_table.setItem(row, 0, QTableWidgetItem(value.placement))
            self.values_table.setItem(row, 1, QTableWidgetItem(value.name))
            self.values_table.setItem(row, 2, QTableWidgetItem(value.value))

        lines: list[str] = []
        if snapshot.config:
            lines.append("Parameters")
            lines.extend(f"  {label}: {text}" for label, text in snapshot.config)
        claims = snapshot.claims
        if claims is not None:
            remaining = claims.expires_in()
            lines.append("")
            lines.append("Decoded token (signature not verified)")
            if claims.subject:
                lines.append(f"  subject: {claims.subject}")
            if claims.scopes:
                lines.append(f"  scopes: {', '.join(claims.scopes)}")
            if claims.roles:
                lines.append(f"  roles: {', '.join(claims.roles)}")
            if remaining is not None:
                lines.append(f"  expires in: {_elapsed(remaining)}")
            lines.append("")
            lines.append("  header: " + repr(claims.header))
            lines.append("  payload: " + repr(claims.payload))
        self.detail.setPlainText("\n".join(lines))

    def _copy_value(self) -> None:
        row = self.values_table.currentRow()
        item = self.values_table.item(row, 2) if row >= 0 else None
        if item is not None:
            QApplication.clipboard().setText(item.text())

    # -------------------------------------------------------------- log

    def _build_log_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        controls = QHBoxLayout()
        self.follow_box = QCheckBox("Follow new events")
        self.follow_box.setChecked(True)
        controls.addWidget(self.follow_box)
        controls.addStretch()
        self.clear_log_button = QPushButton("Clear log")
        self.clear_log_button.clicked.connect(self._clear_log)
        controls.addWidget(self.clear_log_button)
        self.copy_log_button = QPushButton("Copy log")
        self.copy_log_button.setToolTip(
            "Copy the whole log as text. Safe to paste into a bug report - "
            "it contains no credential values."
        )
        self.copy_log_button.clicked.connect(self._copy_log)
        controls.addWidget(self.copy_log_button)
        layout.addLayout(controls)

        self.log_table = QTableWidget(0, 6)
        self.log_table.setHorizontalHeaderLabels(
            ["Time", "Event", "Profile", "Method", "Status", "Detail"]
        )
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.log_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.log_table.horizontalHeader()
        for column in range(5):
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.log_table, 1)
        attach_table_empty_state(
            self.log_table,
            icon_name="verify",
            title="No authorization activity yet",
            guidance="Sign in, refresh a token or send an authorized request and each step is traced here.",
        )

        self.log_detail = QPlainTextEdit()
        self.log_detail.setReadOnly(True)
        self.log_detail.setMaximumHeight(150)
        self.log_table.itemSelectionChanged.connect(self._show_log_detail)
        layout.addWidget(self.log_detail)

        self.log_caption = QLabel()
        self.log_caption.setProperty("fieldCaption", True)
        self.log_caption.setWordWrap(True)
        self.log_caption.setText(
            "Event metadata only - credential values are never written to this log."
        )
        layout.addWidget(self.log_caption)
        return page

    def _refresh_log(self) -> None:
        if not self.follow_box.isChecked():
            return
        entries = self._log.entries()
        self._visible_entries = entries
        self.log_table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            cells = (
                entry.when,
                entry.event,
                entry.profile,
                entry.method,
                str(entry.status or ""),
                entry.summary(),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if not entry.ok:
                    item.setForeground(QColor(theme.FAIL))
                self.log_table.setItem(row, column, item)

    def _entry_lines(self, entry) -> list[str]:
        """The full, readable form of one event."""
        lines = [
            f"time:        {entry.when}",
            f"event:       {entry.event}",
            f"profile:     {entry.profile}",
            f"method:      {entry.method}",
        ]
        if entry.environment:
            lines.append(f"environment: {entry.environment}")
        if entry.http_method or entry.url:
            lines.append(f"request:     {entry.http_method} {entry.url}".rstrip())
        if entry.status:
            lines.append(f"status:      {entry.status}")
        if entry.duration_ms:
            lines.append(f"duration:    {entry.duration_ms:.0f} ms")
        if entry.sent_fields:
            lines.append(f"sent fields: {', '.join(entry.sent_fields)}")
        if entry.header_names:
            lines.append(f"headers:     {', '.join(entry.header_names)}")
        if entry.query_names:
            lines.append(f"query:       {', '.join(entry.query_names)}")
        if entry.detail:
            lines.append(f"detail:      {entry.detail}")
        lines.append(f"outcome:     {'ok' if entry.ok else 'failed'}")
        return lines

    def _show_log_detail(self) -> None:
        row = self.log_table.currentRow()
        entries = self._visible_entries
        if 0 <= row < len(entries):
            self.log_detail.setPlainText("\n".join(self._entry_lines(entries[row])))
        else:
            self.log_detail.setPlainText("")

    def _copy_log(self) -> None:
        text = "\n\n".join(
            "\n".join(self._entry_lines(entry)) for entry in self._visible_entries
        )
        QApplication.clipboard().setText(text)

    def _clear_log(self) -> None:
        self._log.clear()
        self._refresh_log()

    # ------------------------------------------------------------ theme

    def refresh_theme(self) -> None:
        self.inspect_button.setIcon(icon("verify", theme.TEXT, 16))
        self.copy_button.setIcon(icon("copy", theme.TEXT, 16))
        self.clear_log_button.setIcon(icon("clear", theme.TEXT, 16))
        self.copy_log_button.setIcon(icon("copy", theme.TEXT, 16))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        self._timer.stop()
        super().closeEvent(event)
