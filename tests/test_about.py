"""The About dialog behind the header's ? button."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from api_tester import about
from api_tester.about import THIRD_PARTY, AboutDialog, component_version, license_text
from api_tester.branding import APP_LICENSE_ID, APP_NAME, APP_VERSION

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _table_rows(table) -> list[list[str]]:
    return [
        [table.item(row, column).text() for column in range(table.columnCount())]
        for row in range(table.rowCount())
    ]


def test_version_is_semantic() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)


def test_license_file_is_the_bundled_gpl3_text() -> None:
    root_license = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert root_license == about.LICENSE_PATH.read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in root_license
    assert "Version 3, 29 June 2007" in root_license
    assert license_text() == root_license
    assert APP_LICENSE_ID == "GPL-3.0-or-later"


def test_spec_bundles_the_assets_folder_holding_the_license() -> None:
    spec = (ROOT / "RestTester.spec").read_text(encoding="utf-8")
    assert '"api_tester" / "assets"' in spec
    assert about.LICENSE_PATH.parent.name == "assets"


def test_missing_license_file_falls_back_to_a_link(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(about, "LICENSE_PATH", tmp_path / "missing.txt")
    assert "gnu.org/licenses/gpl-3.0" in license_text()


def test_components_report_a_version() -> None:
    names = {component.name for component in THIRD_PARTY}
    assert {"Python", "PyQt6", "Qt 6", "Requests", "MSAL", "PyJWT", "cryptography"} <= names
    for component in THIRD_PARTY:
        assert component_version(component)
        assert component.license


def test_dialog_shows_brand_version_and_all_sections(app: QApplication) -> None:
    dialog = AboutDialog(data_dir=Path("C:/data"), catalog_summary="Demo · 2 services · 5 endpoints")
    assert dialog.windowTitle() == f"About {APP_NAME}"
    assert dialog.title_label.text() == APP_NAME
    assert APP_VERSION in dialog.version_label.text()
    assert APP_LICENSE_ID in dialog.license_badge.text()
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        "Overview",
        "Shortcuts",
        "System",
        "Licenses",
    ]
    assert ["Ctrl+Enter", "Send and verify the current request (API Explorer)"] in _table_rows(
        dialog.shortcuts_table
    )
    system = dict(_table_rows(dialog.system_table))
    assert system["Application"] == f"{APP_NAME} {APP_VERSION}"
    assert system["Catalog"] == "Demo · 2 services · 5 endpoints"
    assert system["Data folder"] == str(Path("C:/data"))
    assert dialog.components_table.rowCount() == len(THIRD_PARTY)
    assert "GNU GENERAL PUBLIC LICENSE" in dialog.license_view.toPlainText()


def test_dialog_is_generic_product_branding(app: QApplication) -> None:
    dialog = AboutDialog()
    texts = [dialog.findChild(QDialog).windowTitle() if dialog.findChild(QDialog) else ""]
    from PyQt6.QtWidgets import QLabel

    texts += [label.text() for label in dialog.findChildren(QLabel)]
    texts.append(dialog.system_details_text())
    assert not any("trialwyze" in text.casefold() for text in texts)
    assert not hasattr(dialog, "open_data_button")  # only offered when a folder is known


def test_copy_details_puts_system_info_on_the_clipboard(app: QApplication) -> None:
    dialog = AboutDialog(catalog_summary="No catalog loaded")
    dialog.copy_button.click()
    copied = QApplication.clipboard().text()
    assert copied.startswith(f"Application: {APP_NAME} {APP_VERSION}")
    assert "Catalog: No catalog loaded" in copied
    assert dialog.copy_button.text() == "Copied"


@pytest.fixture
def window(app, monkeypatch, tmp_path):
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
        )
    value = main_module.MainWindow()
    yield value
    value.close()
    value.deleteLater()


def test_help_button_opens_the_about_dialog(window, monkeypatch) -> None:
    shown: list[AboutDialog] = []
    monkeypatch.setattr(AboutDialog, "exec", lambda self: shown.append(self) or 0)
    window.help_button.click()
    assert len(shown) == 1
    system = dict(_table_rows(shown[0].system_table))
    assert system["Catalog"] == window._catalog_summary()
    assert window.navigation_version.text() == f"v{APP_VERSION}"
    assert "About" in window.help_button.toolTip()
