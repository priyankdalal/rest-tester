"""Options dialog shown before a Load Studio report is exported to PDF."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from api_tester.load_testing.pdf_export import BASE_URL_MODES, PAGE_SIZES, SECTIONS, PdfExportOptions


class PdfExportDialog(QDialog):
    """Collects :class:`PdfExportOptions`; at least one section must stay selected."""

    def __init__(self, *, samples_available: bool, run_label: str = "", author: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pdfExportDialog")
        self.setWindowTitle("Export report to PDF")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        self._has_samples = samples_available

        if run_label:
            title = QLabel(run_label)
            title.setObjectName("loadTestingSectionTitle")
            title.setWordWrap(True)
            layout.addWidget(title)
        self.samples_banner = QLabel(
            "This run was persisted: the report includes latency buckets, HTTP status codes, error signatures and scheduling delay."
            if samples_available
            else "This run was not persisted, so the report is reduced: latency buckets, HTTP status codes, "
            "error signatures and scheduling delay are not available. Enable “Persist this run” for a full report."
        )
        self.samples_banner.setObjectName("pdfExportInfo" if samples_available else "pdfExportWarning")
        self.samples_banner.setWordWrap(True)
        layout.addWidget(self.samples_banner)

        sections_box = QGroupBox("Sections")
        grid = QGridLayout(sections_box)
        self.section_checks: dict[str, QCheckBox] = {}
        for index, (key, label) in enumerate(SECTIONS):
            check = QCheckBox(label.replace("&", "&&"))
            check.setChecked(True)
            check.toggled.connect(self._update_state)
            self.section_checks[key] = check
            grid.addWidget(check, index // 2, index % 2)
        self.error_samples_check = QCheckBox("Include raw error samples (up to 50)")
        self.error_samples_check.setEnabled(samples_available)
        self.error_samples_check.setToolTip(
            "Lists the first failed requests with status, category and message."
            if samples_available
            else "Only available for persisted runs."
        )
        grid.addWidget(self.error_samples_check, (len(SECTIONS) + 1) // 2, 0, 1, 2)
        layout.addWidget(sections_box)

        form = QFormLayout()
        self.page_size_combo = QComboBox()
        self.page_size_combo.addItems(list(PAGE_SIZES))
        form.addRow("Page size", self.page_size_combo)
        self.base_url_combo = QComboBox()
        for key, label in BASE_URL_MODES:
            self.base_url_combo.addItem(label, key)
        self.base_url_combo.setToolTip("Credentials and query strings are always removed from the URL.")
        form.addRow("Base URL", self.base_url_combo)
        self.author_edit = QLineEdit(author)
        self.author_edit.setPlaceholderText("Optional — shown in the footer")
        form.addRow("Prepared by", self.author_edit)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Optional notes added to the appendix")
        self.notes_edit.setFixedHeight(72)
        form.addRow("Notes", self.notes_edit)
        layout.addLayout(form)

        self.open_after_check = QCheckBox("Open the PDF after export")
        self.open_after_check.setChecked(True)
        layout.addWidget(self.open_after_check)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.export_button = self.buttons.addButton("Export…", QDialogButtonBox.ButtonRole.AcceptRole)
        self.export_button.setObjectName("primaryButton")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._update_state()

    def _update_state(self) -> None:
        self.export_button.setEnabled(any(check.isChecked() for check in self.section_checks.values()))
        errors_selected = self.section_checks["errors"].isChecked()
        self.error_samples_check.setEnabled(errors_selected and self._has_samples)

    @property
    def open_after(self) -> bool:
        return self.open_after_check.isChecked()

    def options(self) -> PdfExportOptions:
        return PdfExportOptions(
            sections=frozenset(key for key, check in self.section_checks.items() if check.isChecked()),
            page_size=self.page_size_combo.currentText(),
            base_url_mode=str(self.base_url_combo.currentData()),
            author=self.author_edit.text().strip(),
            notes=self.notes_edit.toPlainText(),
            include_error_samples=self.error_samples_check.isEnabled() and self.error_samples_check.isChecked(),
        )
