from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    value = main_module.MainWindow()
    yield value
    value.close()


def test_environment_toolbar_exposes_grouped_shell_targets(window):
    assert window.environment_toolbar.objectName() == "environmentToolbar"
    assert window.environment_toolbar.property("shellRegion") == "environment"
    assert window.environment_selector.objectName() == "activeEnvironmentSelector"
    assert window.service_selector.objectName() == "activeServiceSelector"
    assert window.base_url_label.objectName() == "environmentBaseUrl"
    assert window.base_url_label.property("monospace") is True
    assert window.edit_environment_button.objectName() == "editEnvironmentButton"
    assert window.edit_environment_button.property("ghost") is True
    assert window.environment_banner.objectName() == "connectionBadge"
    assert window.environment_banner.property("shellRegion") == "environmentStatus"


def test_shell_workspace_pages_expose_compact_title_hierarchy(window):
    explorer_title = window.findChild(type(window.endpoint_title), "apiExplorerPageTitle")
    explorer_description = window.findChild(
        type(window.endpoint_title), "apiExplorerPageDescription"
    )
    settings_title = window.findChild(type(window.endpoint_title), "settingsPageTitle")
    settings_description = window.findChild(
        type(window.endpoint_title), "settingsPageDescription"
    )
    environment_title = window.findChild(
        type(window.endpoint_title), "environmentPageTitle"
    )
    environment_description = window.findChild(
        type(window.endpoint_title), "environmentPageDescription"
    )

    for label in (
        explorer_title,
        settings_title,
        environment_title,
    ):
        assert label is not None
        assert label.property("pageTitle") is True
        assert label.property("workspaceTitle") is True
    for label in (
        explorer_description,
        settings_description,
        environment_description,
    ):
        assert label is not None
        assert label.property("pageDescription") is True
        assert label.wordWrap()


def test_explorer_rows_keep_service_counts_and_semantic_method_data(window):
    from PyQt6.QtCore import Qt

    tree = window.endpoint_tree
    assert tree.objectName() == "endpointTree"
    assert tree.headerItem().text(0) == "Action / path"
    assert tree.headerItem().text(1) == "Method / count"

    service = tree.topLevelItem(0)
    assert service.data(0, 259) == "service"
    assert int(service.text(1)) == service.data(1, 259)
    controller = service.child(0)
    endpoint = controller.child(0)
    assert controller.data(0, 259) == "controller"
    assert endpoint.data(0, 259) == "endpoint"
    assert endpoint.data(1, 258) in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
    assert endpoint.text(0).count("\n") == 1
    assert endpoint.text(1) == endpoint.data(1, 258)
    assert endpoint.textAlignment(1) == int(Qt.AlignmentFlag.AlignCenter)
    assert service.font(0).bold()
    assert controller.font(0).bold()


def test_favorite_selection_has_explicit_state_cues(window):
    endpoint = next(iter(window.endpoints_by_id.values()))
    item = window.endpoint_items[endpoint.id]
    window.endpoint_tree.setCurrentItem(item)
    window._toggle_favorite()

    assert item.data(0, 260) is True
    assert window.favorite_button.property("favorite") is True
    assert window.favorite_button.text() == "Favorited"


def test_splitter_shrink_preserves_marked_suite_pane_minimums(window):
    suite = window.suite_tab

    assert suite.suite_navigator.property("preserveMinimumWidth") is True
    assert suite.suite_detail_shell.property("preserveMinimumWidth") is True
    assert suite.suite_navigator.minimumWidth() == 280
    assert suite.suite_detail_shell.minimumWidth() == 520

    window._allow_splitter_shrink()

    assert suite.suite_navigator.minimumWidth() == 280
    assert suite.suite_detail_shell.minimumWidth() == 520
    assert window.endpoint_explorer.minimumWidth() == 0
