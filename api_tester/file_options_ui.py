"""Transactional file options dialog; Cancel leaves the upload unchanged."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QMessageBox, QVBoxLayout,
)

from .file_options import resolve_file_options
from .headers_ui import HeadersEditor
from .catalog import Endpoint
from .request_auth import SECRET_HEADER_NAMES


class FileOptionsDialog(QDialog):
    def __init__(
        self, parent, path: str, values: dict[str, str],
        secret_names: frozenset[str] = SECRET_HEADER_NAMES,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("File options")
        self.resize(720, 520)
        self._path = path
        layout = QVBoxLayout(self)
        hint = QLabel(
            "These options apply to this file part, not the whole request. "
            "Leave fields blank for automatic values. Do not set the outer multipart "
            "Content-Type: the HTTP client generates its boundary."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.filename = QLineEdit(values.get("filename", ""))
        self.filename.setPlaceholderText("Local filename (automatic)")
        self.content_type = QLineEdit(values.get("content_type", ""))
        self.content_type.setPlaceholderText("Detect from upload filename, otherwise application/octet-stream")
        form.addRow("Upload filename", self.filename)
        form.addRow("Content type", self.content_type)
        layout.addLayout(form)
        self.detected = QLabel()
        self.detected.setWordWrap(True)
        layout.addWidget(self.detected)
        # Reuse the masked header-value controls without environment/auth layers.
        self.part_headers = HeadersEditor()
        self.part_headers.load(
            Endpoint("file", "", "", "", "POST", ""),
            {key: value for key, value in values.items() if key.startswith("header:")},
        )
        self.part_headers.set_context({}, {}, secrets=secret_names)
        self.part_headers.effective.hide()
        self.part_headers.preview_title.hide()
        self.part_headers.hint.setText(
            "Optional headers for this file part. Content-Type, Content-Disposition and "
            "Content-Length are reserved. Known credential values are masked."
        )
        layout.addWidget(self.part_headers, 1)
        self.filename.textChanged.connect(self._show_detection)
        self.content_type.textChanged.connect(self._show_detection)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._show_detection()

    def values(self) -> dict[str, str]:
        values = {
            "filename": self.filename.text().strip(),
            "content_type": self.content_type.text().strip(),
        }
        headers = self.part_headers.values()
        for key, value in headers.items():
            if key.startswith("header:") and headers.get(f"enabled:{key}") != "false":
                values[key] = value
        return {key: value for key, value in values.items() if value or key.startswith("header:")}

    def _show_detection(self) -> None:
        filename = self.filename.text().strip() or Path(self._path).name
        media_type = self.content_type.text().strip() or (
            mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        self.detected.setText(f"Upload: {filename or '(choose a file)'} | Content type: {media_type}")

    def _accept(self) -> None:
        values = self.values()
        # Variable placeholders are resolved and revalidated at request time.
        literal_values = {
            f"file:upload:{key}": (
                "application/octet-stream" if key == "content_type" and "{{" in value else value
            )
            for key, value in values.items()
        }
        try:
            resolve_file_options("upload", self._path or "upload.bin", literal_values)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid file options", str(exc))
            return
        self.accept()
