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


def test_authentication_row_keeps_selector_status_and_edit_on_one_band(window):
    """The environment toolbar was replaced by a single authentication line.

    Selector, connection pill and edit button must share one row and one
    height so the band reads as a single control strip.
    """
    assert window.request_authentication.count() > 0
    assert window.connection_indicator.objectName() == "connectionIndicator"
    assert window.connection_indicator.property("state") in {
        "connected",
        "disconnected",
    }
    assert window.edit_environment_button.objectName() == "editEnvironmentButton"

    row = window.request_authentication.parentWidget().layout()
    widgets = [
        row.itemAt(index).widget() for index in range(row.count())
    ]
    assert window.request_authentication in widgets
    assert window.connection_indicator in widgets
    assert window.edit_environment_button in widgets
    assert widgets.index(window.request_authentication) < widgets.index(
        window.connection_indicator
    )
    assert widgets.index(window.connection_indicator) < widgets.index(
        window.edit_environment_button
    )
    assert window.connection_indicator.height() == max(
        window.request_authentication.sizeHint().height(),
        window.edit_environment_button.sizeHint().height(),
    )


def test_shell_workspace_pages_expose_compact_title_hierarchy(window):
    from api_tester.widgets import ElidingLabel

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
        # Descriptions must stay readable when the pane narrows: either they
        # wrap, or they are ElidingLabels that keep the full text in the tooltip.
        if isinstance(label, ElidingLabel):
            assert label.full_text()
            assert label.toolTip() == label.full_text()
        else:
            assert label.wordWrap()


def test_explorer_rows_keep_service_counts_and_semantic_method_data(window):
    from PyQt6.QtCore import Qt

    from api_tester.main import EndpointTreeRow

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
    assert endpoint.data(0, 258) in {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
    }
    # Endpoint rows are rendered by a widget, so the item itself carries no text.
    assert endpoint.text(0) == ""
    assert endpoint.text(1) == ""
    row = tree.itemWidget(endpoint, 0)
    assert isinstance(row, EndpointTreeRow)
    assert row.method.text() == endpoint.data(0, 258)
    assert row.method.property("method") == endpoint.data(0, 258).upper()
    assert row.action.full_text()
    assert row.path.full_text().startswith("/")
    assert endpoint.toolTip(0).endswith(
        f"{endpoint.data(0, 258)} {row.path.full_text()}"
    )
    assert row.action.full_text() in endpoint.toolTip(0)
    assert service.textAlignment(1) == int(Qt.AlignmentFlag.AlignCenter)
    assert service.font(0).bold()
    assert controller.font(0).bold()


def test_favorite_selection_has_explicit_state_cues(window):
    endpoint = next(iter(window.endpoints_by_id.values()))
    item = window.endpoint_items[endpoint.id]
    before = item.toolTip(0)
    window.endpoint_tree.setCurrentItem(item)
    window._toggle_favorite()

    assert item.data(0, 260) is True
    assert window.favorite_button.property("favorite") is True
    assert window.favorite_button.text() == "Favorited"
    # Marking a favorite must annotate the tooltip, not replace its description.
    assert item.toolTip(0) == f"Favorite endpoint\n{before}"


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
