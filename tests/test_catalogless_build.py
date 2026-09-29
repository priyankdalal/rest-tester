"""The shell must survive a build that ships without an API catalog.

``RestTester.spec`` deliberately omits ``data/api_catalog.json``. Loading it
at construction time used to be unguarded, so a catalog-less executable died
with ``FileNotFoundError`` before the window existed - leaving the user no way
to reach Settings or the Catalog Builder to supply one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from api_tester import main as main_module
from api_tester import theme


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, "Light")
    return app


@pytest.fixture
def catalog_less_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points every writable path at a temp dir and removes the catalog."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(main_module, "ROOT", tmp_path)
    monkeypatch.setattr(main_module, "CATALOG_PATH", data / "api_catalog.json")
    monkeypatch.setattr(main_module, "SETTINGS_PATH", data / "settings.json")
    monkeypatch.setattr(main_module, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", data / "workspace.db")
    return tmp_path


def test_the_window_opens_without_a_catalog(
    qt_app: QApplication, catalog_less_workspace: Path
) -> None:
    window = main_module.MainWindow()
    try:
        assert window.catalog.services == ()
        assert window.services == ()
        # The user must be told why, and it must be reported exactly once.
        assert "FileNotFoundError" in window._startup_catalog_error
    finally:
        window.close()


def test_the_catalog_label_explains_the_missing_file(
    qt_app: QApplication, catalog_less_workspace: Path
) -> None:
    window = main_module.MainWindow()
    try:
        window._refresh_catalog_label()
        text = window.catalog_path_label.text()
        assert "No catalog loaded" in text
        assert "Catalog Builder" in text
    finally:
        window.close()


def test_no_modal_interrupts_a_catalog_less_start(
    qt_app: QApplication, catalog_less_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The missing catalog is reported inline, never with a dialog.

    A modal had to be dismissed before the user could see either pane, which
    is worse than a workspace that explains itself.
    """
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(
            main_module.QMessageBox,
            name,
            lambda *a, _n=name, **k: pytest.fail(f"QMessageBox.{_n} was shown"),
        )
    window = main_module.MainWindow()
    try:
        assert window.services == ()
    finally:
        window.close()


def test_the_explorer_and_workspace_state_the_missing_catalog(
    qt_app: QApplication, catalog_less_workspace: Path
) -> None:
    window = main_module.MainWindow()
    try:
        window.show()
        qt_app.processEvents()

        tree_state = window.endpoint_tree_empty_state
        assert tree_state.isVisible()
        assert tree_state.title_label.text() == "No APIs to show"

        workspace = window.workspace_empty_state
        assert workspace.isVisible()
        assert workspace.title_label.text() == "No API catalog"
        assert (
            workspace.guidance_label.text()
            == "Please go to Settings to create or select a catalog."
        )
        # The guidance names a button, so the button must be there.
        assert workspace.action_button.isVisible()
        assert workspace.action_button.text() == "Settings"
    finally:
        window.close()


def test_the_workspace_button_opens_settings(
    qt_app: QApplication, catalog_less_workspace: Path
) -> None:
    window = main_module.MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window.workspace_empty_state.action_button.click()
        qt_app.processEvents()
        settings_index = window.workspace_tabs.indexOf(window.settings_page)
        assert window.workspace_tabs.currentIndex() == settings_index
        # The rail highlight must follow, not be left on API Explorer.
        assert window.navigation.currentRow() == settings_index
    finally:
        window.close()


def test_a_catalog_is_still_loaded_when_present(
    qt_app: QApplication, catalog_less_workspace: Path
) -> None:
    """The fallback must not mask a catalog that is actually there."""
    import json
    import shutil

    real = Path(__file__).resolve().parent.parent / "data" / "api_catalog.json"
    if not real.is_file():
        pytest.skip("no catalog in the working tree to load")
    shutil.copyfile(real, main_module.CATALOG_PATH)

    window = main_module.MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        assert window._startup_catalog_error == ""
        assert len(window.catalog.services) == len(
            json.loads(real.read_text(encoding="utf-8"))["services"]
        )
        assert window.catalog.services
        # A loaded catalog must leave both panes uncovered.
        assert not window.workspace_empty_state.isVisible()
        assert not window.endpoint_tree_empty_state.isVisible()
    finally:
        window.close()


def test_a_filtered_out_catalog_says_so_instead(
    qt_app: QApplication, catalog_less_workspace: Path
) -> None:
    """Filtering hides rows rather than removing them, so the placeholder
    cannot rely on the model's row count alone."""
    import shutil

    real = Path(__file__).resolve().parent.parent / "data" / "api_catalog.json"
    if not real.is_file():
        pytest.skip("no catalog in the working tree to load")
    shutil.copyfile(real, main_module.CATALOG_PATH)

    window = main_module.MainWindow()
    try:
        window.show()
        qt_app.processEvents()
        window.endpoint_search.setText("zzz-no-endpoint-matches-this-zzz")
        qt_app.processEvents()
        state = window.endpoint_tree_empty_state
        assert state.isVisible()
        assert state.title_label.text() == "No matching endpoints"
        # The workspace is only for a missing catalog, not an empty filter.
        assert not window.workspace_empty_state.isVisible()

        window.endpoint_search.setText("")
        qt_app.processEvents()
        assert not state.isVisible()
    finally:
        window.close()


def test_the_spec_does_not_bundle_the_catalog() -> None:
    """Guards the intent of this build: the catalog is supplied by the user."""
    spec = (
        Path(__file__).resolve().parent.parent / "RestTester.spec"
    ).read_text(encoding="utf-8")
    active = [
        line
        for line in spec.splitlines()
        if "api_catalog.json" in line and not line.strip().startswith("#")
    ]
    assert not active, f"spec bundles the catalog again: {active}"
