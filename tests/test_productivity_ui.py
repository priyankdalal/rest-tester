from __future__ import annotations

import pytest

from api_tester.saved_requests import RequestCollection, SavedRequest


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
    value.deleteLater()


def _first_endpoint(window):
    return next(iter(window.endpoints_by_id.values()))


def test_frozen_application_uses_executable_directory(monkeypatch, tmp_path) -> None:
    import sys

    from api_tester.main import application_root

    executable = tmp_path / "RestTester.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert application_root() == tmp_path


def test_frozen_application_reads_bundled_resources(monkeypatch, tmp_path) -> None:
    import sys

    from api_tester.main import application_resource_root

    resource_root = tmp_path / "_internal"
    monkeypatch.setattr(sys, "_MEIPASS", str(resource_root), raising=False)
    assert application_resource_root() == resource_root


def test_navigation_exposes_all_redesigned_workspaces(window) -> None:
    assert [
        window.navigation.item(index).text()
        for index in range(window.navigation.count())
    ] == [
        "API Explorer",
        "Test Suites",
        "Data Runner",
        "Load Studio",
        "Saved Requests",
        "Collections",
        "Environments",
        "Settings",
    ]


def test_navigation_uses_distinct_semantic_icons(window) -> None:
    icons = [
        window.navigation.item(index).icon()
        for index in range(window.navigation.count())
    ]
    assert all(not icon.isNull() for icon in icons)
    assert len({icon.cacheKey() for icon in icons}) == window.navigation.count()


def test_navigation_rows_align_one_to_one_with_workspace_tabs(window) -> None:
    """Regression test: each rail row must select the workspace tab in the
    same position — a rail item added out of step with workspace_tabs.addTab()
    shifts every entry below it by one."""
    assert window.navigation.count() == window.workspace_tabs.count()
    for row in range(window.navigation.count()):
        window.navigation.setCurrentRow(row)
        assert window.workspace_tabs.currentIndex() == row


def test_navigation_never_shows_a_horizontal_scrollbar(window) -> None:
    from PyQt6.QtCore import Qt

    assert (
        window.navigation.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )


def test_endpoint_content_casts_shadow_from_its_left_edge(window) -> None:
    from PyQt6.QtCore import Qt

    assert window.endpoint_explorer.graphicsEffect() is None
    assert window.endpoint_splitter_handle is window.main_splitter.handle(1)
    assert window.endpoint_splitter_handle.objectName() == "endpointExplorerHandle"
    assert window.endpoint_splitter_handle.graphicsEffect() is None
    assert window.endpoint_content_area.graphicsEffect() is None
    shadow = window.endpoint_content_inset_shadow
    assert shadow.objectName() == "leftInsetShadow"
    assert shadow.width() == 12
    assert shadow.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    top_shadow = window.endpoint_content_top_inset_shadow
    assert top_shadow.objectName() == "topInsetShadow"
    assert top_shadow.height() == 12
    assert top_shadow.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents
    )


def test_endpoint_accordion_aligns_with_top_level_content(window, qt_app) -> None:
    window.resize(1325, 900)
    window.show()
    qt_app.processEvents()

    identity_left = window.endpoint_method.mapTo(
        window.endpoint_content_area, window.endpoint_method.rect().topLeft()
    ).x()
    accordion_left = window.request_section.mapTo(
        window.endpoint_content_area, window.request_section.rect().topLeft()
    ).x()
    assert accordion_left == identity_left


def test_suite_detail_uses_matching_inset_shadow(window) -> None:
    from PyQt6.QtCore import Qt

    assert window.suite_tab.suite_navigator.graphicsEffect() is None
    assert window.suite_tab.detail_tabs.graphicsEffect() is None
    left_shadow = window.suite_tab.suite_detail_left_shadow
    top_shadow = window.suite_tab.suite_detail_top_shadow
    assert left_shadow.objectName() == "leftInsetShadow"
    assert top_shadow.objectName() == "topInsetShadow"
    assert left_shadow.width() == 12
    assert top_shadow.height() == 12
    assert left_shadow.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents
    )
    assert top_shadow.testAttribute(
        Qt.WidgetAttribute.WA_TransparentForMouseEvents
    )
    margins = window.suite_tab.suite_detail_shell.layout().contentsMargins()
    assert margins.top() == 8


def test_suite_tabs_align_with_inset_outer_edge(window, qt_app) -> None:
    window.resize(1325, 900)
    window.show()
    window.workspace_tabs.setCurrentWidget(window.suite_tab)
    qt_app.processEvents()

    tab = window.suite_tab
    shell_left = tab.suite_detail_shell.mapTo(
        tab, tab.suite_detail_shell.rect().topLeft()
    ).x()
    first_tab_left = tab.detail_tabs.tabBar().mapTo(
        tab, tab.detail_tabs.tabBar().tabRect(0).topLeft()
    ).x()
    assert first_tab_left == shell_left
    assert not tab.detail_tabs.tabBar().usesScrollButtons()


def test_suite_action_bar_uses_subtle_surface(window) -> None:
    from PyQt6.QtWidgets import QSizePolicy

    assert window.suite_tab.suite_action_bar.objectName() == "suiteActionBar"
    assert (
        window.suite_tab.suite_action_bar.sizePolicy().verticalPolicy()
        == QSizePolicy.Policy.Fixed
    )
    margins = window.suite_tab.suite_action_bar.layout().contentsMargins()
    assert margins.left() == 10
    assert margins.top() == 8


def test_suite_page_and_forms_use_shared_hierarchy(window) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QFormLayout, QLabel

    titles = [
        label.text()
        for label in window.suite_tab.findChildren(QLabel)
        if label.property("pageTitle")
    ]
    assert titles == ["Test Suites"]
    for form in window.suite_tab.findChildren(QFormLayout):
        assert form.horizontalSpacing() == 16
        assert form.verticalSpacing() == 8
        assert form.labelAlignment() & Qt.AlignmentFlag.AlignLeft


def test_suite_workspace_preserves_usable_responsive_panes(window) -> None:
    splitter = window.suite_tab.suite_workspace_splitter
    assert not splitter.childrenCollapsible()
    assert window.suite_tab.suite_navigator.minimumWidth() == 280
    assert window.suite_tab.suite_detail_shell.minimumWidth() == 520
    assert all(
        splitter.isCollapsible(index) is False
        for index in range(splitter.count())
    )


def test_suite_progress_uses_compact_status_surface(window) -> None:
    from PyQt6.QtWidgets import QSizePolicy

    tab = window.suite_tab
    assert tab.progress_panel.objectName() == "suiteProgressPanel"
    assert (
        tab.progress_panel.sizePolicy().verticalPolicy()
        == QSizePolicy.Policy.Fixed
    )
    assert tab.progress.objectName() == "suiteRunProgress"
    assert not tab.progress.isTextVisible()
    assert tab.progress.height() == 10
    assert tab.progress_status.text() == "Running test suite"
    assert tab.progress_count.text() == "0 of 0 cases"
    assert tab.run_button.property("accent")
    assert tab.stop_button.property("danger")


def test_suite_toolbar_actions_use_theme_aware_icons(window) -> None:
    from api_tester import theme
    from PyQt6.QtWidgets import QApplication

    tab = window.suite_tab
    assert set(tab.suite_toolbar_buttons) == {
        "new",
        "open",
        "save",
        "save-as",
        "play",
        "stop",
        "export",
    }
    assert all(
        not button.icon().isNull()
        for button in tab.suite_toolbar_buttons.values()
    )

    app = QApplication.instance()
    theme.apply_theme(app, "Light")
    tab.refresh_theme()
    light_keys = {
        name: button.icon().cacheKey()
        for name, button in tab.suite_toolbar_buttons.items()
    }
    theme.apply_theme(app, "Dark")
    tab.refresh_theme()
    assert any(
        button.icon().cacheKey() != light_keys[name]
        for name, button in tab.suite_toolbar_buttons.items()
    )
    theme.apply_theme(app, "Light")
    tab.refresh_theme()


def test_case_navigator_uses_theme_aware_icon_actions(window) -> None:
    from api_tester import theme
    from PyQt6.QtWidgets import QApplication, QSizePolicy

    tab = window.suite_tab
    assert set(tab.case_action_buttons) == {
        "add",
        "duplicate",
        "trash",
        "move-up",
        "move-down",
    }
    buttons = list(tab.case_action_buttons.values())
    assert all(button.text() == "" for button in buttons)
    assert all(button.toolTip() for button in buttons)
    assert all(button.accessibleName() == button.toolTip() for button in buttons)
    assert all(button.height() == 34 for button in buttons)
    assert all(
        button.sizePolicy().horizontalPolicy()
        == QSizePolicy.Policy.Expanding
        for button in buttons
    )
    QApplication.processEvents()
    widths = [button.width() for button in buttons]
    assert max(widths) - min(widths) <= 1
    assert tab.case_action_buttons["trash"].property("danger") is True

    app = QApplication.instance()
    theme.apply_theme(app, "Light")
    tab.refresh_theme()
    light_keys = {
        name: button.icon().cacheKey()
        for name, button in tab.case_action_buttons.items()
    }
    assert all(not button.icon().isNull() for button in buttons)
    theme.apply_theme(app, "Dark")
    tab.refresh_theme()
    assert any(
        button.icon().cacheKey() != light_keys[name]
        for name, button in tab.case_action_buttons.items()
    )
    theme.apply_theme(app, "Light")
    tab.refresh_theme()


def test_suite_variable_actions_are_equal_width_theme_aware_icons(window) -> None:
    from api_tester import theme
    from PyQt6.QtWidgets import QApplication, QSizePolicy

    tab = window.suite_tab
    assert set(tab.variable_action_buttons) == {"add", "trash"}
    buttons = list(tab.variable_action_buttons.values())
    assert all(button.text() == "" for button in buttons)
    assert all(button.toolTip() for button in buttons)
    assert all(button.accessibleName() == button.toolTip() for button in buttons)
    assert all(button.height() == 34 for button in buttons)
    assert all(
        button.sizePolicy().horizontalPolicy()
        == QSizePolicy.Policy.Expanding
        for button in buttons
    )
    QApplication.processEvents()
    widths = [button.width() for button in buttons]
    assert max(widths) - min(widths) <= 1
    assert tab.variable_action_buttons["trash"].property("danger") is True

    app = QApplication.instance()
    theme.apply_theme(app, "Light")
    tab.refresh_theme()
    light_keys = {
        name: button.icon().cacheKey()
        for name, button in tab.variable_action_buttons.items()
    }
    assert all(not button.icon().isNull() for button in buttons)
    theme.apply_theme(app, "Dark")
    tab.refresh_theme()
    assert any(
        button.icon().cacheKey() != light_keys[name]
        for name, button in tab.variable_action_buttons.items()
    )
    theme.apply_theme(app, "Light")
    tab.refresh_theme()


def test_request_builder_icons_preserve_text_policy_and_placement(window) -> None:
    from api_tester import theme
    from PyQt6.QtWidgets import QApplication

    groups = (
        (
            window.request_editor.path_seed_button,
            window.request_editor.query_seed_button,
            window.query_builder.action_buttons,
            window.sort_builder.action_buttons,
        ),
        (
            window.suite_tab.request_editor.path_seed_button,
            window.suite_tab.request_editor.query_seed_button,
            window.suite_tab.query_builder.action_buttons,
            window.suite_tab.sort_builder.action_buttons,
        ),
    )
    buttons = [
        button
        for path_seed, query_seed, query_actions, sort_actions in groups
        for button in (
            path_seed,
            query_seed,
            *query_actions.values(),
            *sort_actions.values(),
        )
    ]
    labels = [button.text() for button in buttons]
    policies = [
        (
            button.sizePolicy().horizontalPolicy(),
            button.sizePolicy().verticalPolicy(),
        )
        for button in buttons
    ]
    placements = (
        window.query_builder.layout().indexOf(window.query_builder.table),
        window.sort_builder.layout().indexOf(window.sort_builder.table),
        window.suite_tab.query_builder.layout().indexOf(
            window.suite_tab.query_builder.table
        ),
        window.suite_tab.sort_builder.layout().indexOf(
            window.suite_tab.sort_builder.table
        ),
    )

    app = QApplication.instance()
    theme.apply_theme(app, "Light")
    window.request_editor.refresh_theme()
    window.query_builder.refresh_theme()
    window.sort_builder.refresh_theme()
    window.suite_tab.refresh_theme()
    light_keys = [button.icon().cacheKey() for button in buttons]
    assert all(not button.icon().isNull() for button in buttons)

    theme.apply_theme(app, "Dark")
    window.request_editor.refresh_theme()
    window.query_builder.refresh_theme()
    window.sort_builder.refresh_theme()
    window.suite_tab.refresh_theme()
    assert [button.text() for button in buttons] == labels
    assert [
        (
            button.sizePolicy().horizontalPolicy(),
            button.sizePolicy().verticalPolicy(),
        )
        for button in buttons
    ] == policies
    assert placements == (
        window.query_builder.layout().indexOf(window.query_builder.table),
        window.sort_builder.layout().indexOf(window.sort_builder.table),
        window.suite_tab.query_builder.layout().indexOf(
            window.suite_tab.query_builder.table
        ),
        window.suite_tab.sort_builder.layout().indexOf(
            window.suite_tab.sort_builder.table
        ),
    )
    assert [button.icon().cacheKey() for button in buttons] != light_keys
    theme.apply_theme(app, "Light")
    window.request_editor.refresh_theme()
    window.query_builder.refresh_theme()
    window.sort_builder.refresh_theme()
    window.suite_tab.refresh_theme()


def test_payload_seed_icon_preserves_placement_and_expand_policy(window) -> None:
    from api_tester import theme
    from api_tester.icons import icon
    from PyQt6.QtWidgets import QApplication

    button = window.payload_seed_button
    policy = (
        button.sizePolicy().horizontalPolicy(),
        button.sizePolicy().verticalPolicy(),
    )
    parent_layout = window.payload_seed_layout
    placement = parent_layout.indexOf(button)

    assert button.text() == ""
    assert button.toolTip() == "Generate or seed request payload data"
    assert button.accessibleName() == button.toolTip()

    app = QApplication.instance()
    theme.apply_theme(app, "Light")
    button.setIcon(icon("seed", theme.TEXT, 18))
    light_key = button.icon().cacheKey()
    theme.apply_theme(app, "Dark")
    button.setIcon(icon("seed", theme.TEXT, 18))
    assert button.icon().cacheKey() != light_key
    assert placement == parent_layout.indexOf(button)
    assert policy == (
        button.sizePolicy().horizontalPolicy(),
        button.sizePolicy().verticalPolicy(),
    )
    theme.apply_theme(app, "Light")


def test_payload_field_page_actions_preserve_policy_and_placement(window) -> None:
    from api_tester import theme
    from PyQt6.QtWidgets import QApplication

    groups = (
        (window.request_editor.payload_fields_buttons, window.request_editor.payload_fields_layout),
        (
            window.suite_tab.request_editor.payload_fields_buttons,
            window.suite_tab.request_editor.payload_fields_layout,
        ),
    )
    buttons = [button for actions, _layout in groups for button in actions.values()]
    policies = [
        (
            button.sizePolicy().horizontalPolicy(),
            button.sizePolicy().verticalPolicy(),
        )
        for button in buttons
    ]
    placements = [
        layout.indexOf(button)
        for actions, layout in groups
        for button in actions.values()
    ]
    assert all(button.text() == "Seed fields" for button in buttons)
    assert all(button.toolTip() for button in buttons)
    assert all(button.accessibleName() == button.toolTip() for button in buttons)

    app = QApplication.instance()
    theme.apply_theme(app, "Light")
    window._theme_changed("Light")
    light_keys = [button.icon().cacheKey() for button in buttons]
    theme.apply_theme(app, "Dark")
    window._theme_changed("Dark")
    assert [button.icon().cacheKey() for button in buttons] != light_keys
    assert policies == [
        (
            button.sizePolicy().horizontalPolicy(),
            button.sizePolicy().verticalPolicy(),
        )
        for button in buttons
    ]
    assert placements == [
        layout.indexOf(button)
        for actions, layout in groups
        for button in actions.values()
    ]
    window._theme_changed("Light")


def test_theme_toggle_applies_theme_immediately(window) -> None:
    from api_tester import theme
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    window._theme_changed("Light")
    assert theme.ACTIVE_TOKENS["BACKGROUND"] == theme.LIGHT_TOKENS["BACKGROUND"]
    assert theme.LIGHT_TOKENS["BACKGROUND"] in app.styleSheet()

    # The toggle flips to the opposite palette rather than offering a list.
    window.theme_toggle_button.click()
    assert window.app_settings.theme == "Dark"
    assert theme.ACTIVE_TOKENS["BACKGROUND"] == theme.DARK_TOKENS["BACKGROUND"]
    assert theme.DARK_TOKENS["BACKGROUND"] in app.styleSheet()

    window.theme_toggle_button.click()
    assert window.app_settings.theme == "Light"
    assert theme.ACTIVE_TOKENS["BACKGROUND"] == theme.LIGHT_TOKENS["BACKGROUND"]


def test_endpoint_actions_use_header_hierarchy(window) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QWidget

    header = window.endpoint_header_layout
    assert header.indexOf(window.save_request_button) >= 0
    assert header.indexOf(window.add_to_suite_button) >= 0
    assert header.indexOf(window.send_button) >= 0
    assert header.indexOf(window.endpoint_more_actions) >= 0
    assert window.send_button.text() == "Send"
    assert window.send_button.property("accent")
    assert window.endpoint_more_actions.minimumWidth() >= 76
    assert (
        window.endpoint_more_actions.toolButtonStyle()
        == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    )
    assert [
        action.text()
        for action in window.endpoint_more_actions.menu().actions()
        if not action.isSeparator()
    ] == [
        "Generate Python Request",
        "Generate cURL",
        "Copy URL",
        "Open Source File",
    ]
    ordered_controls = [
        window.send_button,
        window.save_request_button,
        window.add_to_suite_button,
        window.favorite_button,
        window.endpoint_more_actions,
    ]
    assert all(not control.icon().isNull() for control in ordered_controls)
    assert [header.indexOf(control) for control in ordered_controls] == sorted(
        header.indexOf(control) for control in ordered_controls
    )
    assert window.findChild(QWidget, "requestActionBar") is None


def test_suite_expected_result_uses_scrollable_accordion_sections(window) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QGraphicsDropShadowEffect

    tab = window.suite_tab
    expected = tab.expected_result_scroll
    result = window.suite_tab.case_result_accordion
    assert expected.widgetResizable()
    assert (
        expected.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert (
        expected.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    assert tab.assertions_section.is_expanded()
    assert tab.assertions_section.summary() == "0 assertions"
    assert not tab.baseline_section.is_expanded()
    assert tab.baseline_section.summary() == "Optional"
    assert not tab.captures_section.is_expanded()
    assert tab.captures_section.summary() == "0 variables"
    assert result.widgetResizable()
    assert tab.result_summary_section.is_expanded()
    assert tab.result_summary_section.summary() == "Not run"
    assert tab.result_response_section.is_expanded()
    assert tab.result_response_section.summary() == "No response"
    shadow = tab.result_summary_section.graphicsEffect()
    assert isinstance(shadow, QGraphicsDropShadowEffect)
    assert shadow.blurRadius() == 14
    assert shadow.offset().x() == 0
    assert shadow.offset().y() == 4
    assert result.content_layout.spacing() == 14


def test_suite_expected_accordion_toggles_without_resizer(window, qt_app) -> None:
    section = window.suite_tab.baseline_section
    section.header.click()
    qt_app.processEvents()
    assert section.is_expanded()
    assert not section.body.isHidden()
    section.header.click()
    qt_app.processEvents()
    assert not section.is_expanded()
    assert not section.body.isVisible()


def test_run_analysis_uses_scrollable_accordion_sections(window) -> None:
    from PyQt6.QtCore import Qt

    visualizer = window.suite_tab.visualizer
    accordion = visualizer.analysis_accordion
    assert accordion.widgetResizable()
    assert (
        accordion.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert visualizer.timing_section.is_expanded()
    assert visualizer.breakdown_section.is_expanded()


def test_suite_analysis_and_assertion_cells_follow_dark_theme(
    window, qt_app
) -> None:
    from types import SimpleNamespace

    from PyQt6.QtGui import QPixmap

    from api_tester import theme
    from api_tester.suite import Assertion

    try:
        theme.apply_theme(qt_app, "Dark")
        window.suite_tab.refresh_theme()

        window.suite_tab._append_assertion_row(
            Assertion(kind="json_equals", path="id", value="1")
        )
        editable = window.suite_tab.assertions.item(0, 3)
        assert editable.background().color().name() == theme.SURFACE

        kind = window.suite_tab.assertions.cellWidget(0, 1)
        kind.setCurrentIndex(kind.findData("json_exists"))
        disabled = window.suite_tab.assertions.item(0, 3)
        assert disabled.background().color().name() == theme.SURFACE_ALT

        chart = window.suite_tab.visualizer.chart
        chart.set_results(
            [
                SimpleNamespace(
                    elapsed_ms=120,
                    skipped=False,
                    outcome="PASS",
                )
            ]
        )
        chart.resize(500, 240)
        pixmap = QPixmap(chart.size())
        chart.render(pixmap)
        assert pixmap.toImage().pixelColor(1, 1).name() == theme.SURFACE
    finally:
        theme.apply_theme(qt_app, "Light")
        window.suite_tab.refresh_theme()


def test_suite_endpoint_row_keeps_single_line_height_when_window_grows(
    window, qt_app
) -> None:
    from PyQt6.QtWidgets import QSizePolicy

    endpoint = window.suite_tab.case_endpoint
    expected_height = endpoint.height()

    assert (
        endpoint.sizePolicy().verticalPolicy()
        == QSizePolicy.Policy.Fixed
    )
    assert endpoint.minimumHeight() == expected_height
    assert endpoint.maximumHeight() == expected_height

    window.resize(1260, 700)
    window.show()
    qt_app.processEvents()
    compact_height = endpoint.height()
    window.resize(1260, 1000)
    qt_app.processEvents()

    assert compact_height == expected_height
    assert endpoint.height() == expected_height


def test_run_analysis_tables_scroll_horizontally(window) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QAbstractItemView, QHeaderView

    visualizer = window.suite_tab.visualizer
    for table in (visualizer.slowest, visualizer.by_service, visualizer.failures):
        assert (
            table.horizontalScrollBarPolicy()
            == Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        assert (
            table.horizontalScrollMode()
            == QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        assert not table.horizontalHeader().stretchLastSection()
        assert all(
            table.horizontalHeader().sectionResizeMode(column)
            == QHeaderView.ResizeMode.Interactive
            for column in range(table.columnCount())
        )


def test_path_parameters_are_compulsory_and_query_parameters_are_toggleable(
    window,
) -> None:
    from PyQt6.QtCore import Qt

    endpoint = next(
        endpoint
        for endpoint in window.endpoints_by_id.values()
        if any(parameter.source == "path" for parameter in endpoint.parameters)
        and any(
            parameter.source == "query"
            and parameter.name not in {"Filter", "Sort"}
            for parameter in endpoint.parameters
        )
    )
    window.endpoint_tree.setCurrentItem(window.endpoint_items[endpoint.id])

    path_row = next(
        row
        for row, parameter in enumerate(window.request_editor._path_parameter_list)
        if parameter.source == "path"
    )
    query_row = next(
        row
        for row, parameter in enumerate(window.request_editor._query_parameter_list)
        if parameter.source == "query"
    )
    path_item = window.request_editor.path_parameters.item(path_row, 0)
    query_item = window.request_editor.query_parameters.item(query_row, 0)

    assert path_item.data(Qt.ItemDataRole.CheckStateRole) is None
    assert not path_item.flags() & Qt.ItemFlag.ItemIsEditable
    path_parameter = window.request_editor._path_parameter_list[path_row]
    assert f"path:{path_parameter.name}" in window._current_values()
    assert f"enabled:path:{path_parameter.name}" not in window._current_values()
    assert query_item.flags() & Qt.ItemFlag.ItemIsUserCheckable

    query_item.setCheckState(Qt.CheckState.Unchecked)
    values = window._current_values()
    query_parameter = window.request_editor._query_parameter_list[query_row]
    assert (
        values[f"enabled:query:{query_parameter.name}"]
        == "false"
    )


def test_suite_preserves_unchecked_query_parameters(window) -> None:
    from PyQt6.QtCore import Qt

    endpoint = next(
        endpoint
        for endpoint in window.endpoints_by_id.values()
        if any(
            parameter.source == "query"
            and parameter.name not in {"Filter", "Sort"}
            for parameter in endpoint.parameters
        )
    )
    window.suite_tab.add_case_for_endpoint(endpoint, {}, None, "Toggle query")
    row = next(
        index
        for index, parameter in enumerate(window.suite_tab.request_editor._query_parameter_list)
        if parameter.source == "query"
    )
    parameter = window.suite_tab.request_editor._query_parameter_list[row]
    source_item = window.suite_tab.request_editor.query_parameters.item(row, 0)
    source_item.setCheckState(Qt.CheckState.Unchecked)

    assert window.suite_tab.current_case is not None
    assert (
        window.suite_tab.current_case.values[
            f"enabled:query:{parameter.name}"
        ]
        == "false"
    )


def test_parameter_rows_are_tall_enough_for_their_cell_widgets(window) -> None:
    from api_tester.viewers import FilePicker, ValuePicker

    needed = max(
        FilePicker().minimumSizeHint().height(),
        ValuePicker(["a"]).minimumSizeHint().height(),
    )
    for editor in (window.request_editor, window.suite_tab.request_editor):
        for table in (editor.path_parameters, editor.query_parameters):
            assert table.verticalHeader().defaultSectionSize() >= needed


def test_saved_request_can_be_reopened_in_explorer(window) -> None:
    endpoint = _first_endpoint(window)
    request = SavedRequest(
        endpoint_id=endpoint.id,
        name="Saved smoke test",
        values={},
        payload={"saved": True},
        expected_status="201",
    )
    window.saved_request_store.upsert_request(request)
    window._open_saved_request(request.id)
    assert window.current_endpoint.id == endpoint.id
    assert window.expected_status.currentText() == "201"
    assert '"saved": true' in window.payload.toPlainText().lower()
    assert window.navigation.currentRow() == 0


def test_collection_materializes_ordered_suite_cases(window) -> None:
    endpoint = _first_endpoint(window)
    first = SavedRequest(endpoint.id, "First")
    second = SavedRequest(endpoint.id, "Second")
    window.saved_request_store.upsert_request(first)
    window.saved_request_store.upsert_request(second)
    collection = RequestCollection("Workflow", [second.id, first.id])
    window.saved_request_store.upsert_collection(collection)
    cases = window._collection_cases(collection.request_ids)
    assert [case.name for case in cases] == ["Second", "First"]


def test_navigation_follows_programmatic_workspace_changes(window) -> None:
    window.workspace_tabs.setCurrentWidget(window.suite_tab)
    suite_index = window.workspace_tabs.indexOf(window.suite_tab)
    assert window.navigation.currentRow() == suite_index

    window.workspace_tabs.setCurrentIndex(0)
    assert window.navigation.currentRow() == 0


def test_window_never_demands_more_width_than_a_common_display(window) -> None:
    window.resize(1600, 950)
    window.show()
    assert window.minimumWidth() <= 1280
    window.resize(1200, 760)
    assert window.width() == 1200


def test_builder_rows_use_icon_delete_buttons_with_readable_height(window) -> None:
    from PyQt6.QtWidgets import QPushButton

    schema = next(
        schema
        for schema in (
            window.catalog.filter_schema(endpoint.filter_entity)
            for endpoint in window.endpoints_by_id.values()
        )
        if schema is not None and schema.fields and schema.sortable_fields
    )
    window.query_builder.set_schema(schema)
    window.query_builder.add_condition()
    assert window.query_builder.table.rowHeight(0) >= 32
    actions = window.query_builder.table.cellWidget(0, 4)
    buttons = actions.findChildren(QPushButton)
    remove = next(b for b in buttons if b.objectName() == "rowRemoveButton")
    assert remove.text() == ""
    assert not remove.icon().isNull()
    seed = next(b for b in buttons if b.objectName() == "rowButton")
    assert seed.text() == "Seed"

    window.sort_builder.set_schema(schema)
    window.sort_builder.add_entry()
    assert window.sort_builder.table.rowHeight(0) >= 32
    sort_remove = window.sort_builder.table.cellWidget(0, 3).findChildren(QPushButton)[0]
    assert sort_remove.objectName() == "rowRemoveButton"
    assert not sort_remove.icon().isNull()
