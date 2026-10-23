"""Transactional guided editors for generic endpoint and service evidence."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from .scanners.openapi import load_document
from .security import fingerprint, safe_text, sanitize_contract


class SecurityMetadataDialog(QDialog):
    """Cancel leaves the caller's metadata and imported snapshot untouched."""

    def __init__(self, metadata: dict, parent=None, *, service: bool = False) -> None:
        super().__init__(parent)
        self._original = copy.deepcopy(metadata)
        self._working = copy.deepcopy(metadata)
        self._result = copy.deepcopy(metadata)
        self._service = service
        self.setWindowTitle("Service security evidence" if service else "Endpoint security metadata")
        self.resize(700, 560)
        layout = QVBoxLayout(self)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        form = QFormLayout()
        layout.addLayout(form)
        self.authentication = QComboBox()
        self.authentication.addItems(["Unknown / inherited", "Authentication required", "Anonymous permitted"])
        if metadata.get("require_authentication") is True:
            self.authentication.setCurrentIndex(1)
        elif metadata.get("require_authentication") is False:
            self.authentication.setCurrentIndex(2)
        self.security_override = QCheckBox("Explicit security override (otherwise inherited)")
        self.security_override.setChecked("security" in metadata)
        self.security = QPlainTextEdit(json.dumps(metadata.get("security", []), indent=2))
        self.security.setEnabled(self.security_override.isChecked())
        self.security_override.toggled.connect(self.security.setEnabled)
        self.sensitive_fields = QPlainTextEdit(
            "\n".join(str(v) for v in metadata.get("sensitive_response_fields", []))
        )
        if service:
            self.source = QLineEdit()
            self.source.setReadOnly(True)
            self.version = QLineEdit()
            self.version.setReadOnly(True)
            self.source_fingerprint = QLineEdit()
            self.source_fingerprint.setReadOnly(True)
            form.addRow("Source contract", self.source)
            form.addRow("Version", self.version)
            form.addRow("Source fingerprint", self.source_fingerprint)
            row = QHBoxLayout()
            self.import_button = QPushButton("Import OpenAPI contract…")
            self.remove_button = QPushButton("Remove source evidence")
            row.addWidget(self.import_button)
            row.addWidget(self.remove_button)
            layout.addLayout(row)
            self.import_button.clicked.connect(self._import_contract)
            self.remove_button.clicked.connect(self._remove_evidence)
            self.schemes = QPlainTextEdit()
            self.schemes.setReadOnly(True)
            form.addRow("Imported security schemes", self.schemes)
            self._refresh_source()
            note = QLabel("Import deliberately replaces the source snapshot. Remove clears only source evidence. "
                          "Other metadata is preserved. Schemes reflect the imported contract.")
        else:
            form.addRow("Authentication expectation", self.authentication)
            form.addRow(self.security_override)
            form.addRow("OpenAPI security array", self.security)
            form.addRow("Sensitive response fields", self.sensitive_fields)
            note = QLabel(
                'Security is an array of requirement objects: [{"bearer": [], "key": []}] means AND; '
                '[{"bearer": []}, {"key": []}] means OR. [] explicitly permits anonymous access; '
                'uncheck to inherit. Enter sensitive field names one per line or comma-separated.'
            )
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(note)
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _refresh_source(self) -> None:
        document = self._working.get("document") or {}
        self.source.setText(safe_text(self._working.get("source_file", "")))
        self.source_fingerprint.setText(safe_text(self._working.get("source_fingerprint", "")))
        self.version.setText(str(document.get("openapi") or document.get("swagger") or "No source evidence"))
        schemes = (document.get("components") or {}).get("securitySchemes", document.get("securityDefinitions", {}))
        self.schemes.setPlainText(json.dumps(schemes, ensure_ascii=False, indent=2))

    def _import_contract(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Import security contract", "", "OpenAPI contracts (*.json *.yaml *.yml)",
        )
        if not filename:
            return
        try:
            document = load_document(Path(filename))
            if not document.get("openapi") and not document.get("swagger"):
                raise ValueError("The document must declare an OpenAPI or Swagger version.")
            if not isinstance(document.get("paths"), dict):
                raise ValueError("The contract must contain a paths object.")
            document = sanitize_contract(document)
            self._working.update(document=document, source_file=filename,
                                 source_fingerprint=fingerprint(document))
            self._refresh_source()
            self.error.clear()
        except Exception as exc:
            self.error.setText(safe_text(f"Contract import failed: {exc}"))

    def _remove_evidence(self) -> None:
        for key in ("document", "source_file", "source_fingerprint"):
            self._working.pop(key, None)
        self._refresh_source()

    def accept(self) -> None:
        try:
            result = copy.deepcopy(self._working)
            if not self._service:
                index = self.authentication.currentIndex()
                if index == 0:
                    result.pop("require_authentication", None)
                else:
                    result["require_authentication"] = index == 1
                if self.security_override.isChecked():
                    security = json.loads(self.security.toPlainText())
                    if not isinstance(security, list) or any(
                        not isinstance(requirement, dict) or any(
                            not isinstance(name, str) or not name.strip() or
                            not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes)
                            for name, scopes in requirement.items()
                        ) for requirement in security
                    ):
                        raise ValueError("Security must be an array of objects mapping scheme names to string arrays.")
                    result["security"] = security
                else:
                    result.pop("security", None)
                field_text = self.sensitive_fields.toPlainText().strip()
                if field_text.startswith("["):
                    fields = json.loads(field_text)
                    if not isinstance(fields, list) or any(not isinstance(field, str) for field in fields):
                        raise ValueError("Sensitive response fields must be an array of field-name strings.")
                    fields = [field.strip() for field in fields if field.strip()]
                else:
                    fields = [field.strip() for field in field_text.replace(",", "\n").splitlines()
                              if field.strip()]
                if fields:
                    result["sensitive_response_fields"] = list(dict.fromkeys(fields))
                else:
                    result.pop("sensitive_response_fields", None)
            # Existing source evidence is not rewritten by unrelated form edits.
            self._result = result
            super().accept()
        except Exception as exc:
            self.error.setText(safe_text(f"Invalid security metadata: {exc}"))

    def result_metadata(self) -> dict:
        return copy.deepcopy(self._result if self.result() == QDialog.DialogCode.Accepted else self._original)
