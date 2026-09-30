"""Environment controls shared by explorer requests and suite runs."""

from __future__ import annotations

import json

import pytest


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_ssl_verification_is_enabled_by_default(qt_app, monkeypatch, tmp_path) -> None:
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    window = main_module.MainWindow()
    try:
        assert window.verify_ssl.isChecked()
        assert window._environment()["verify_ssl"] is True
        assert window.request_timeout.value() == 30
        assert window._environment()["request_timeout"] == 30
    finally:
        window.close()


def test_local_dev_can_disable_and_persist_ssl_verification(
    qt_app, monkeypatch, tmp_path
) -> None:
    import api_tester.main as main_module

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(main_module, "SETTINGS_PATH", settings_path)
    window = main_module.MainWindow()
    try:
        window.verify_ssl.setChecked(False)
        assert window._environment()["verify_ssl"] is False
        window._save_settings()
    finally:
        window.close()

    assert json.loads(settings_path.read_text(encoding="utf-8"))["verify_ssl"] is False

    restored = main_module.MainWindow()
    try:
        assert not restored.verify_ssl.isChecked()
    finally:
        restored.close()


def test_request_timeout_is_shared_and_persisted(qt_app, monkeypatch, tmp_path) -> None:
    import api_tester.main as main_module

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(main_module, "SETTINGS_PATH", settings_path)
    window = main_module.MainWindow()
    try:
        window.request_timeout.setValue(180)
        assert window._environment()["request_timeout"] == 180
        window._save_settings()
    finally:
        window.close()

    assert json.loads(settings_path.read_text(encoding="utf-8"))["request_timeout"] == 180

    restored = main_module.MainWindow()
    try:
        assert restored.request_timeout.value() == 180
    finally:
        restored.close()


def test_endpoint_search_and_method_filter_hide_non_matches(
    qt_app, monkeypatch, tmp_path
) -> None:
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    window = main_module.MainWindow()
    try:
        window.endpoint_search.setText("brand")
        visible = [
            endpoint_id
            for endpoint_id, item in window.endpoint_items.items()
            if not item.isHidden()
        ]
        assert visible
        assert all(
            "brand"
            in " ".join(
                (
                    window.endpoints_by_id[item].service,
                    window.endpoints_by_id[item].controller,
                    window.endpoints_by_id[item].action,
                    window.endpoints_by_id[item].path,
                )
            ).lower()
            for item in visible
        )

        window.method_filter.setCurrentText("GET")
        visible = [
            endpoint_id
            for endpoint_id, item in window.endpoint_items.items()
            if not item.isHidden()
        ]
        assert visible
        assert all(window.endpoints_by_id[item].method == "GET" for item in visible)
    finally:
        window.close()


def test_api_explorer_columns_are_resizable_and_persisted(
    qt_app, monkeypatch, tmp_path
) -> None:
    import api_tester.main as main_module
    from PyQt6.QtWidgets import QHeaderView

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(main_module, "SETTINGS_PATH", settings_path)
    window = main_module.MainWindow()
    try:
        header = window.endpoint_tree.header()
        # The explorer header is hidden: the action/path column absorbs every
        # spare pixel and the method/count column stays a fixed narrow gutter,
        # so neither is dragged by the user.
        assert window.endpoint_tree.isHeaderHidden()
        assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.Stretch
        assert header.sectionResizeMode(1) == QHeaderView.ResizeMode.Fixed
        assert not header.stretchLastSection()
        assert window.endpoint_tree.columnWidth(1) == 52
        window._save_settings()
    finally:
        window.close()

    document = json.loads(settings_path.read_text(encoding="utf-8"))
    assert document["endpoint_column_widths"][1] == 52

    restored = main_module.MainWindow()
    try:
        assert restored.endpoint_tree.columnWidth(1) == 52
    finally:
        restored.close()


def test_request_response_accordions_are_scrollable_and_persisted(
    qt_app, monkeypatch, tmp_path
) -> None:
    import api_tester.main as main_module
    from PyQt6.QtCore import Qt

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(main_module, "SETTINGS_PATH", settings_path)
    window = main_module.MainWindow()
    window.resize(1600, 950)
    window.show()
    qt_app.processEvents()
    try:
        accordion = window.request_response_accordion
        assert accordion.widgetResizable()
        assert (
            accordion.horizontalScrollBarPolicy()
            == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        assert window.request_section.is_expanded()
        assert window.response_section.is_expanded()
        window.response_section.set_expanded(False)
        qt_app.processEvents()
        window._save_settings()
    finally:
        window.close()

    document = json.loads(settings_path.read_text(encoding="utf-8"))
    assert document["accordion_states"] == {
        "endpoint_request": True,
        "endpoint_response": False,
    }

    restored = main_module.MainWindow()
    try:
        assert restored.request_section.is_expanded()
        assert not restored.response_section.is_expanded()
    finally:
        restored.close()


def test_parameters_and_request_builder_are_horizontally_resizable_and_persisted(
    qt_app, monkeypatch, tmp_path
) -> None:
    import api_tester.main as main_module
    from PyQt6.QtCore import Qt

    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(main_module, "SETTINGS_PATH", settings_path)
    window = main_module.MainWindow()
    window.resize(1600, 950)
    window.show()
    qt_app.processEvents()
    try:
        splitter = window.request_builder_splitter
        assert splitter.orientation() == Qt.Orientation.Horizontal
        assert splitter.count() == 2
        assert splitter.handleWidth() >= 7
        splitter.setSizes([500, 700])
        qt_app.processEvents()
        saved_sizes = splitter.sizes()
        assert saved_sizes[0] > 0
        assert saved_sizes[1] > saved_sizes[0]
        window._save_settings()
    finally:
        window.close()

    document = json.loads(settings_path.read_text(encoding="utf-8"))
    assert document["request_builder_sizes"] == saved_sizes

    restored = main_module.MainWindow()
    restored.resize(1600, 950)
    restored.show()
    qt_app.processEvents()
    try:
        restored_sizes = restored.request_builder_splitter.sizes()
        # Panes can now compress, so the restored split is checked by ratio
        # rather than by exact pixels, which depend on the surrounding layout.
        saved_ratio = saved_sizes[0] / sum(saved_sizes)
        restored_ratio = restored_sizes[0] / sum(restored_sizes)
        assert abs(saved_ratio - restored_ratio) < 0.02
    finally:
        restored.close()
