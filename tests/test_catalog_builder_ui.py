"""GUI coverage for the Catalog Builder, the tabular environment editor, and
the Settings catalog loader."""

import json
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QMessageBox, QPushButton

from api_tester import environment_ui, main as main_module
from api_tester.catalog_builder import CatalogDocument, EndpointDraft, ServiceDraft, new_response_schema
from api_tester.catalog_builder_ui import CatalogBuilderWindow
from api_tester.environment import AppSettings
from api_tester.scanners.registry import scan_project
from api_tester.widgets import KeyValueTable


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
        )


# ------------------------------------------------------------------ widgets


def test_key_value_table_round_trips_pairs(app: QApplication) -> None:
    table = KeyValueTable("Header", "Value")
    table.set_pairs({"Authorization": "Bearer abc", "x-api-key": "key"})
    assert table.pairs() == {"Authorization": "Bearer abc", "x-api-key": "key"}


def test_key_value_table_adds_and_deletes_rows(app: QApplication) -> None:
    table = KeyValueTable("Header", "Value")
    table.set_pairs({"a": "1", "b": "2"})
    table.add_row("c", "3")
    assert table.pairs() == {"a": "1", "b": "2", "c": "3"}
    table.table.selectRow(0)
    table.delete_selected()
    assert "a" not in table.pairs()


def test_key_value_table_ignores_rows_without_a_key(app: QApplication) -> None:
    table = KeyValueTable("Header", "Value")
    table.set_pairs({"a": "1"})
    table.add_row("", "orphan")
    assert table.pairs() == {"a": "1"}


# -------------------------------------------------------------- environment


def test_environment_toolbar_is_gone() -> None:
    assert not hasattr(environment_ui, "EnvironmentToolbar")


def test_environment_editor_stores_credentials_as_custom_headers(
    app: QApplication,
) -> None:
    settings = AppSettings.from_dict({}, {"Demo": "https://localhost:7001"})
    editor = environment_ui.EnvironmentEditor(settings.active, ["Demo"])
    editor.custom_headers.set_pairs(
        {"Authorization": "Bearer token-123", "x-api-key": "key-456"}
    )
    editor.apply()
    assert settings.active.custom_headers["Authorization"] == "Bearer token-123"
    assert settings.active.access_token == "Bearer token-123"
    assert settings.active.api_key == "key-456"


def test_environment_credentials_survive_a_settings_round_trip() -> None:
    settings = AppSettings.from_dict({}, {"Demo": "https://localhost:7001"})
    settings.active.custom_headers["x-api-key"] = "key-456"
    reloaded = AppSettings.from_dict(settings.to_dict(), {"Demo": ""})
    assert reloaded.active.api_key == "key-456"


# ---------------------------------------------------------- catalog builder


def test_builder_scans_a_project_and_saves_a_loadable_catalog(
    app: QApplication, tmp_path: Path, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow()
    window.document = CatalogDocument()
    window.document.add_service("Demo", default_base_url="https://localhost:7001")
    scanned = scan_project(FIXTURES / "fastapi_app", "Demo", "fastapi")
    window.document.merge_scan("Demo", scanned)
    assert scanned.endpoints

    target = tmp_path / "built.json"
    window.document.save(target)

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["services"][0]["name"] == "Demo"
    catalog = main_module.load_catalog(target)
    assert catalog.services[0].endpoints


def test_builder_tree_renders_service_module_and_endpoint_levels(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow()
    window.document = CatalogDocument()
    window.document.add_service("Demo")
    window.document.merge_scan(
        "Demo", scan_project(FIXTURES / "fastapi_app", "Demo", "fastapi")
    )
    window._populate()
    root = window.tree.topLevelItem(0)
    assert root is not None
    assert root.childCount() >= 1
    assert root.child(0).childCount() >= 1


def test_builder_tree_renders_a_response_schema_group(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow()
    window.document = CatalogDocument()
    window.document.response_schemas["BrandResponse"] = new_response_schema("BrandResponse")
    window._populate()
    labels = [window._schema_root.child(index).text(0) for index in range(window._schema_root.childCount())]
    assert "Response schemas" in labels


# --------------------------------------------------------- settings loading


def test_main_window_loads_a_different_catalog_and_re_renders(
    app: QApplication, tmp_path: Path, quiet_dialogs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.MainWindow, "_save_settings", lambda self: None)
    window = main_module.MainWindow()
    bundled_services = len(window.services)
    assert bundled_services > 1

    source = json.loads(main_module.CATALOG_PATH.read_text(encoding="utf-8"))
    trimmed = dict(source, services=source["services"][:1])
    target = tmp_path / "trimmed.json"
    target.write_text(json.dumps(trimmed), encoding="utf-8")

    assert window._load_catalog_from(target) is True
    assert len(window.services) == 1
    assert window.app_settings.catalog_path == str(target)
    assert set(window.base_url_inputs) == {window.services[0].name}
    assert window.suite_tab.endpoints.keys() == window.endpoints_by_id.keys()
    assert window.endpoint_tree.topLevelItemCount() == 1

    assert window._load_catalog_from(main_module.CATALOG_PATH, True) is True
    assert len(window.services) == bundled_services
    assert window.app_settings.catalog_path == ""
    window.close()


def test_main_window_rejects_an_unreadable_catalog(
    app: QApplication, tmp_path: Path, quiet_dialogs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.MainWindow, "_save_settings", lambda self: None)
    window = main_module.MainWindow()
    before = len(window.services)
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert window._load_catalog_from(broken) is False
    assert len(window.services) == before
    window.close()


def test_startup_catalog_path_falls_back_to_the_bundled_catalog(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_module.MainWindow, "_save_settings", lambda self: None)
    window = main_module.MainWindow()
    assert window._startup_catalog_path({}) == main_module.CATALOG_PATH
    missing = tmp_path / "gone.json"
    assert window._startup_catalog_path({"catalog_path": str(missing)}) == (
        main_module.CATALOG_PATH
    )
    window.close()


def test_endpoint_response_schema_picker_loads_and_applies(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow()
    document = CatalogDocument()
    document.response_schemas["BrandResponse"] = new_response_schema("BrandResponse")
    service = ServiceDraft(name="Demo")
    endpoint = EndpointDraft(
        service="Demo",
        controller="Brand",
        action="GetBrand",
        method="GET",
        path="/Brand/{id}",
        response_schema="BrandResponse",
    )
    service.endpoints.append(endpoint)
    document.services.append(service)
    window.document = document
    window._populate()

    window._select_endpoint_item(endpoint.id)
    assert window.endpoint_response_schema.currentText() == "BrandResponse"
    window.endpoint_response_schema.setCurrentIndex(0)
    window._apply_endpoint()
    assert endpoint.response_schema is None


def test_response_schema_autodetect_button_updates_the_picker(
    app: QApplication, quiet_dialogs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = CatalogBuilderWindow()
    document = CatalogDocument()
    service = ServiceDraft(name="Demo")
    endpoint = EndpointDraft(
        service="Demo",
        controller="Brand",
        action="GetBrand",
        method="GET",
        path="/Brand/{id}",
    )
    service.endpoints.append(endpoint)
    document.services.append(service)
    window.document = document
    window._populate()
    window._select_endpoint_item(endpoint.id)

    def fake_autodetect(endpoint_id: str) -> str:
        assert endpoint_id == endpoint.id
        window.document.response_schemas["BrandResponse"] = new_response_schema("BrandResponse")
        endpoint.response_schema = "BrandResponse"
        return "BrandResponse"

    monkeypatch.setattr(window.document, "autodetect_response_schema", fake_autodetect)

    button = next(
        child
        for child in window.endpoint_response_schema.parentWidget().findChildren(QPushButton)
        if child.text() == "Auto-detect from source"
    )
    button.click()

    assert window.endpoint_response_schema.currentText() == "BrandResponse"
