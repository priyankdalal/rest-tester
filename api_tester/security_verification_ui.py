"""Guided approval of explicit bounded assertions; no plans or tokens are saved."""

from __future__ import annotations

import json

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QTableWidget, QVBoxLayout,
)

from .security_ui import SecurityReviewDialog
from .security_verification import (
    VerificationCase, VerificationPlan, execute_verification, validate_plan,
)


_TEMPLATE = {
    "cases": [{
        "name": "Reviewed controlled health assertion",
        "category": "API8",
        "method": "GET",
        "path": "/health",
        "identity": "",
        "expected_status": [200],
        "expected_headers": {},
        "forbidden_fields": [],
        "repeat": 1,
        "fixture": "",
    }],
    "cleanup": [],
}


def _label(text: str) -> QLabel:
    label = QLabel()
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setText(text)
    label.setWordWrap(True)
    return label


class SecurityVerificationDialog(QDialog):
    """Author a plan in memory and confirm its exact scope before execution."""

    def __init__(self, parent=None, *, store=None) -> None:
        super().__init__(parent)
        self._store = store
        self.setWindowTitle("Controlled API Security Verification")
        self.resize(920, 850)
        layout = QVBoxLayout(self)
        layout.addWidget(_label(
            "Opt-in active verification sends real requests only after approval. Use only "
            "authorized, controlled fixtures. No exploit payloads, brute force, SSRF payloads "
            "or downstream faults are generated. Results cover only authored assertions, "
            "not vulnerability absence or compliance. Credentials are memory-only; response "
            "bodies and plans are never exported or saved."
        ))
        form = QFormLayout()
        self.origin = QLineEdit()
        self.origin.setPlaceholderText("https://your-approved-test-origin.example")
        form.addRow("Exact approved origin", self.origin)
        self.approval = QCheckBox("I am authorized to test this exact origin and have reviewed every case.")
        form.addRow(self.approval)
        self.mutations = QCheckBox("Allow reviewed mutations / bounded uploads")
        self.risks = QCheckBox("I acknowledge mutation risks and have authored fixture cleanup.")
        form.addRow(self.mutations)
        form.addRow(self.risks)
        self.budget = QSpinBox()
        self.budget.setRange(1, 50)
        self.budget.setValue(20)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.1, 30)
        self.timeout.setValue(10)
        self.interval = QSpinBox()
        self.interval.setRange(100, 60000)
        self.interval.setValue(250)
        form.addRow("Total request budget (includes reserved cleanup)", self.budget)
        form.addRow("Timeout per request (seconds)", self.timeout)
        form.addRow("Minimum request interval (milliseconds)", self.interval)
        layout.addLayout(form)
        layout.addWidget(_label(
            "Named identities (optional): header values are masked and memory-only. "
            "Use separate explicitly authorized owner / denied-role identities as needed. "
            "An empty case identity means no credentials. Credential headers belong here, "
            "never in case JSON, query strings or the approved origin."
        ))
        self.identities = QTableWidget(0, 3)
        self.identities.setHorizontalHeaderLabels(["Identity name", "Header name", "Masked header value"])
        self.identities.horizontalHeader().setStretchLastSection(True)
        self.identities.setMaximumHeight(150)
        layout.addWidget(self.identities)
        identity_buttons = QHBoxLayout()
        add_identity = QPushButton("Add identity header")
        remove_identity = QPushButton("Remove selected header")
        add_identity.clicked.connect(lambda: self.add_identity())
        remove_identity.clicked.connect(self.remove_identity)
        identity_buttons.addWidget(add_identity)
        identity_buttons.addWidget(remove_identity)
        identity_buttons.addStretch()
        layout.addLayout(identity_buttons)
        layout.addWidget(_label(
            "Author JSON with cases and cleanup lists. Each case: name (reviewed description), "
            "category API1–API10, method, origin-relative path, optional identity, headers, "
            "json_body, expected_status (list), expected_headers (exact values), forbidden_fields "
            "(JSON property names, checked recursively), repeat (1–10), fixture. Optional "
            "upload_path / upload_field sends one local file ≤1 MiB. Every case needs an "
            "explicit assertion; HTTP 200 alone is not a category-wide pass. API6 business "
            "flow, API7 URL-policy and API10 downstream cases require a controlled fixture "
            "description. Mutations additionally require both gates and cleanup. No redirects, "
            "environment proxies, session cookies, netrc or ambient authentication are used. "
            "Cleanup runs even on cancellation and failures."
        ))
        self.cases = QPlainTextEdit()
        self.cases.setPlainText(json.dumps(_TEMPLATE, indent=2))
        layout.addWidget(self.cases, 1)
        buttons = QHBoxLayout()
        self.validate_button = QPushButton("Validate plan (no requests)")
        self.start_button = QPushButton("Review and start…")
        close = QPushButton("Close")
        self.validate_button.clicked.connect(self.validate)
        self.start_button.clicked.connect(self.execute)
        close.clicked.connect(self.reject)
        buttons.addWidget(self.validate_button)
        buttons.addWidget(self.start_button)
        buttons.addStretch()
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.status = _label("No requests have been sent.")
        layout.addWidget(self.status)

    def add_identity(self, name: str = "", header: str = "", value: str = "") -> None:
        if self.identities.rowCount() >= 40:
            return
        row = self.identities.rowCount()
        self.identities.insertRow(row)
        for col, text in enumerate((name, header, value)):
            editor = QLineEdit(text)
            editor.setMaxLength(8192 if col == 2 else 100)
            if col == 2:
                editor.setEchoMode(QLineEdit.EchoMode.Password)
            self.identities.setCellWidget(row, col, editor)

    def remove_identity(self) -> None:
        row = self.identities.currentRow()
        if row >= 0:
            self.identities.cellWidget(row, 2).clear()
            self.identities.removeRow(row)

    def _clear_credentials(self) -> None:
        for row in range(self.identities.rowCount()):
            self.identities.cellWidget(row, 2).clear()
        self.identities.setRowCount(0)

    def build_plan(self) -> VerificationPlan:
        """Read widgets without writing to any workspace or configuration store."""
        if len(self.cases.toPlainText()) > 512 * 1024:
            raise ValueError("Plan JSON exceeds the editor limit.")
        try:
            data = json.loads(self.cases.toPlainText())
            if not isinstance(data, dict) or set(data) != {"cases", "cleanup"}:
                raise ValueError
            if any(not isinstance(data[key], list) for key in ("cases", "cleanup")):
                raise ValueError
            cases = [VerificationCase(**item) for item in data["cases"]]
            cleanup = [VerificationCase(**item) for item in data["cleanup"]]
        except (TypeError, ValueError, RecursionError):
            raise ValueError("Invalid plan JSON: use cases / cleanup arrays and the documented case fields.") from None
        identities: dict[str, dict[str, str]] = {}
        for row in range(self.identities.rowCount()):
            name, header, value = [
                self.identities.cellWidget(row, col).text() for col in range(3)
            ]
            if not name or not header or not value:
                raise ValueError("Identity rows require a name, header and masked value.")
            identity = identities.setdefault(name, {})
            if header.lower() in {key.lower() for key in identity}:
                raise ValueError("Duplicate identity header.")
            identity[header] = value
        return validate_plan(VerificationPlan(
            approved_origin=self.origin.text(), approved=self.approval.isChecked(),
            cases=cases, cleanup=cleanup, identities=identities,
            max_requests=self.budget.value(), timeout=self.timeout.value(),
            min_interval_ms=self.interval.value(),
            allow_mutations=self.mutations.isChecked(), acknowledged_risks=self.risks.isChecked(),
        ))

    def validate(self) -> None:
        try:
            plan = self.build_plan()
        except (ValueError, TypeError, UnicodeError):
            self.status.setText(
                "Plan rejected. Check origin approval, JSON fields, identities, assertions, "
                "bounds and mutation / cleanup gates. No requests sent."
            )
            return
        self.status.setText(
            f"Valid bounded plan for {plan.approved_origin}; "
            f"{sum(c.repeat for c in plan.cases + plan.cleanup)} requests including cleanup. "
            "No requests sent."
        )

    def execute(self) -> None:
        try:
            plan = self.build_plan()
        except (ValueError, TypeError, UnicodeError):
            self.validate()
            return
        main = sum(case.repeat for case in plan.cases)
        cleanup = sum(case.repeat for case in plan.cleanup)
        mutations = sum(case.repeat for case in plan.cases + plan.cleanup
                        if case.method not in {"GET", "HEAD", "OPTIONS"} or case.upload_path)
        answer = QMessageBox.question(
            self, "Final authorization confirmation",
            f"Send only the reviewed controlled assertions to:\n{plan.approved_origin}\n\n"
            f"Assertions: {main}; reserved cleanup: {cleanup}; mutating requests: {mutations}.\n"
            f"Budget: {plan.max_requests}; timeout: {plan.timeout:g}s; interval: {plan.min_interval_ms}ms.\n"
            "Cleanup runs even if cancelled. No exploit or category-wide security verdict is produced.\n\n"
            "Confirm you are authorized and these fixtures are safe to execute.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            review = SecurityReviewDialog(
                lambda cancel: execute_verification(plan, cancel), self, store=self._store,
            )
            review.exec()
        finally:
            plan.identities.clear()
            self._clear_credentials()
        self.status.setText("Controlled verification finished. In-memory identity values cleared.")

    def done(self, result: int) -> None:
        self._clear_credentials()
        super().done(result)
