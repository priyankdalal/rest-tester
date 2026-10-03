"""The "About Rest Tester" dialog opened from the header's ? button.

Everything shown here is generic product information: what the application
does, its version, the keyboard shortcuts, the runtime it is running on, its
own license and the licenses of the components it ships with.
"""

from __future__ import annotations

import platform
import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFont, QFontDatabase
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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

from .branding import (
    APP_COPYRIGHT,
    APP_DESCRIPTION,
    APP_LICENSE_ID,
    APP_LICENSE_NAME,
    APP_LICENSE_URL,
    APP_NAME,
    APP_TAGLINE,
    APP_VERSION,
)
from .icons import app_pixmap

LICENSE_PATH = Path(__file__).resolve().parent / "assets" / "LICENSE.txt"

FEATURES: tuple[tuple[str, str], ...] = (
    ("API Explorer", "Browse the catalog, build schema-aware requests, send and verify responses."),
    ("Test Suites", "Group requests into regression cases with assertions, variables and history."),
    ("Data Runner", "Drive one endpoint from a CSV file with column mapping and validation."),
    ("Load Studio", "Staged load tests with live charts, thresholds, findings and PDF reports."),
    ("Catalog Builder", "Scan source projects to generate and refine the endpoint catalog."),
    ("Environments", "Per-environment base URLs, credentials and authentication strategies."),
)

SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Ctrl+Enter", "Send and verify the current request (API Explorer)"),
    ("Ctrl+L", "Focus the endpoint search (API Explorer)"),
    ("Ctrl+K", "Ask AI: describe a request in plain words"),
    ("Alt+Left / Alt+Right", "Back / forward (Catalog Builder)"),
    ("Enter", "Pick the first suggestion in a searchable selector"),
    ("Esc", "Restore a searchable selector to its current value"),
)


@dataclass(frozen=True)
class Component:
    name: str
    distribution: str | None
    license: str
    purpose: str


#: Components the packaged application ships with. ``distribution`` is the
#: installed package used to look up the running version.
THIRD_PARTY: tuple[Component, ...] = (
    Component("Python", None, "PSF-2.0", "Runtime"),
    Component("PyQt6", "PyQt6", "GPL-3.0-only", "Qt bindings for the user interface"),
    Component("Qt 6", "PyQt6-Qt6", "LGPL-3.0-only", "User-interface framework"),
    Component("Requests", "requests", "Apache-2.0", "HTTP client"),
    Component("urllib3", "urllib3", "MIT", "HTTP transport"),
    Component("certifi", "certifi", "MPL-2.0", "CA certificate bundle"),
    Component("idna", "idna", "BSD-3-Clause", "Internationalised domain names"),
    Component("charset-normalizer", "charset-normalizer", "MIT", "Response encoding detection"),
    Component("MSAL", "msal", "MIT", "Microsoft identity platform sign-in"),
    Component("MSAL Extensions", "msal-extensions", "MIT", "Persistent token cache"),
    Component("PyJWT", "PyJWT", "MIT", "JSON Web Token signing"),
    Component("cryptography", "cryptography", "Apache-2.0 OR BSD-3-Clause", "Key and signature support"),
    Component("cffi", "cffi", "MIT-0", "C bindings used by cryptography"),
)


def component_version(component: Component) -> str:
    if component.distribution is None:
        return platform.python_version()
    if component.distribution == "PyQt6":
        return PYQT_VERSION_STR
    if component.distribution == "PyQt6-Qt6":
        return QT_VERSION_STR
    try:
        return metadata.version(component.distribution)
    except metadata.PackageNotFoundError:
        # Frozen builds may omit package metadata; the component is still bundled.
        return "bundled"


def license_text() -> str:
    try:
        return LICENSE_PATH.read_text(encoding="utf-8")
    except OSError:
        return f"{APP_LICENSE_NAME}\n\nThe full text is available at {APP_LICENSE_URL}"


def run_mode() -> str:
    return "Packaged executable" if getattr(sys, "frozen", False) else "Python source"


def system_details(data_dir: Path | None = None, catalog_summary: str = "") -> list[tuple[str, str]]:
    rows = [
        ("Application", f"{APP_NAME} {APP_VERSION}"),
        ("License", APP_LICENSE_ID),
        ("Run mode", run_mode()),
        ("Python", f"{platform.python_version()} ({platform.python_implementation()})"),
        ("PyQt / Qt", f"{PYQT_VERSION_STR} / {QT_VERSION_STR}"),
        ("Operating system", platform.platform()),
        ("Architecture", platform.machine() or "unknown"),
    ]
    platform_name = QApplication.platformName() if QApplication.instance() else ""
    if platform_name:
        rows.append(("Display platform", platform_name))
    if catalog_summary:
        rows.append(("Catalog", catalog_summary))
    if data_dir is not None:
        rows.append(("Data folder", str(data_dir)))
    return rows


def system_details_text(data_dir: Path | None = None, catalog_summary: str = "") -> str:
    return "\n".join(f"{key}: {value}" for key, value in system_details(data_dir, catalog_summary))


class AboutDialog(QDialog):
    """Product details, shortcuts, runtime information and licenses."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        data_dir: Path | None = None,
        catalog_summary: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("aboutDialog")
        self.setWindowTitle(f"About {APP_NAME}")
        self.setMinimumSize(640, 560)
        self.resize(780, 680)
        self.data_dir = data_dir
        self.catalog_summary = catalog_summary

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)
        layout.addLayout(self._build_hero())

        self.tabs = QTabWidget()
        self.tabs.setObjectName("aboutTabs")
        self.tabs.addTab(self._build_overview_tab(), "Overview")
        self.tabs.addTab(self._build_shortcuts_tab(), "Shortcuts")
        self.tabs.addTab(self._build_system_tab(), "System")
        self.tabs.addTab(self._build_licenses_tab(), "Licenses")
        layout.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)

    # -- sections -------------------------------------------------------

    def _build_hero(self) -> QHBoxLayout:
        hero = QHBoxLayout()
        hero.setSpacing(16)
        logo = QLabel()
        logo.setObjectName("aboutLogo")
        logo.setPixmap(app_pixmap(72))
        logo.setFixedSize(72, 72)
        hero.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.title_label = QLabel(APP_NAME)
        self.title_label.setObjectName("aboutTitle")
        text.addWidget(self.title_label)
        self.version_label = QLabel(f"Version {APP_VERSION}")
        self.version_label.setObjectName("aboutVersion")
        self.version_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(self.version_label)
        tagline = QLabel(APP_TAGLINE)
        tagline.setObjectName("aboutTagline")
        tagline.setWordWrap(True)
        text.addWidget(tagline)
        self.license_badge = QLabel(f"Free and open source · {APP_LICENSE_ID}")
        self.license_badge.setObjectName("aboutLicenseBadge")
        text.addWidget(self.license_badge, 0, Qt.AlignmentFlag.AlignLeft)
        hero.addLayout(text, 1)
        return hero

    def _build_overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        description = QLabel(APP_DESCRIPTION)
        description.setWordWrap(True)
        layout.addWidget(description)

        grid = QFormLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        for name, summary in FEATURES:
            name_label = QLabel(name)
            name_label.setObjectName("aboutFeatureName")
            summary_label = QLabel(summary)
            summary_label.setWordWrap(True)
            grid.addRow(name_label, summary_label)
        layout.addLayout(grid)
        layout.addStretch(1)

        privacy = QLabel(
            "Your requests, environments, credentials and run history stay on this machine. "
            f"{APP_NAME} sends traffic only to the APIs and sign-in providers you configure."
        )
        privacy.setObjectName("aboutNote")
        privacy.setWordWrap(True)
        layout.addWidget(privacy)
        copyright_label = QLabel(f"{APP_COPYRIGHT}. Distributed under the {APP_LICENSE_NAME}.")
        copyright_label.setObjectName("aboutFootnote")
        copyright_label.setWordWrap(True)
        layout.addWidget(copyright_label)
        return page

    def _build_shortcuts_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        self.shortcuts_table = self._table(("Shortcut", "Action"), SHORTCUTS)
        layout.addWidget(self.shortcuts_table)
        hint = QLabel(
            "Environment settings apply to API Explorer, Test Suites, Data Runner and Load Studio. "
            "Use the arrow on Send to choose between sending and send & verify."
        )
        hint.setObjectName("aboutNote")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return page

    def _build_system_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        self.system_table = self._table(
            ("Item", "Value"), system_details(self.data_dir, self.catalog_summary)
        )
        layout.addWidget(self.system_table, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        if self.data_dir is not None:
            self.open_data_button = QPushButton("Open data folder")
            self.open_data_button.clicked.connect(self._open_data_folder)
            row.addWidget(self.open_data_button)
        self.copy_button = QPushButton("Copy details")
        self.copy_button.setToolTip("Copies these details for a bug report")
        self.copy_button.clicked.connect(self.copy_system_details)
        row.addWidget(self.copy_button)
        layout.addLayout(row)
        return page

    def _build_licenses_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        summary = QLabel(
            f"{APP_NAME} is free software: you can redistribute it and/or modify it under the "
            f"terms of the {APP_LICENSE_NAME}. It is distributed in the hope that it will be "
            "useful, but WITHOUT ANY WARRANTY; without even the implied warranty of "
            "MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        self.license_view = QPlainTextEdit(license_text())
        self.license_view.setObjectName("aboutLicenseText")
        self.license_view.setReadOnly(True)
        self.license_view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.license_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.license_view, 3)

        heading = QLabel("Third-party components")
        heading_font = QFont(heading.font())
        heading_font.setBold(True)
        heading.setFont(heading_font)
        layout.addWidget(heading)
        self.components_table = self._table(
            ("Component", "Version", "License", "Used for"),
            [(c.name, component_version(c), c.license, c.purpose) for c in THIRD_PARTY],
        )
        layout.addWidget(self.components_table, 2)

        link_row = QHBoxLayout()
        link_row.addStretch(1)
        self.license_link_button = QPushButton("View license online")
        self.license_link_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(APP_LICENSE_URL))
        )
        link_row.addWidget(self.license_link_button)
        layout.addLayout(link_row)
        return page

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _table(headers: tuple[str, ...], rows) -> QTableWidget:
        rows = list(rows)
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(list(headers))
        table.verticalHeader().hide()
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                table.setItem(row, column, QTableWidgetItem(str(value)))
        header = table.horizontalHeader()
        for column in range(len(headers) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
        return table

    def system_details_text(self) -> str:
        return system_details_text(self.data_dir, self.catalog_summary)

    def copy_system_details(self) -> None:
        QApplication.clipboard().setText(self.system_details_text())
        self.copy_button.setText("Copied")

    def _open_data_folder(self) -> None:
        if self.data_dir is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.data_dir)))
