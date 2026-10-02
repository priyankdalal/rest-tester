"""Enum-constrained parameters: authoring in the builder, dropdowns in the tester."""

import json
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox

from api_tester.catalog import Parameter, load_catalog
from api_tester.catalog_builder import CatalogDocument, EndpointDraft, ServiceDraft
from api_tester.catalog_builder_ui import ParameterTable, ParameterValuesDialog
from api_tester.seeding import seed_parameter
from api_tester.viewers import ValuePicker
from api_tester.widgets import button_in_cell


BUNDLED = Path(__file__).resolve().parents[1] / "data" / "api_catalog.json"
ENUMS = {"Status": ["Draft", "Active"], "BookType": ["Agronomic", "Harvest"]}


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("information", "warning", "question"):
        monkeypatch.setattr(
            QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )


@pytest.fixture
def window(app, quiet_dialogs, monkeypatch, tmp_path):
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    value = main_module.MainWindow()
    yield value
    value.close()
    value.deleteLater()


def document_with_a_parameter(parameter: dict) -> CatalogDocument:
    document = CatalogDocument()
    service = ServiceDraft(name="Demo", default_base_url="https://localhost:7001")
    service.endpoints.append(
        EndpointDraft(
            service="Demo",
            controller="Book",
            action="GetBooks",
            method="GET",
            path="/Book",
            parameters=[parameter],
        )
    )
    document.services.append(service)
    document.enums.update({key: list(value) for key, value in ENUMS.items()})
    return document


def row_key(table: ParameterTable, row: int) -> int:
    return table.table.item(row, 0).data(Qt.ItemDataRole.UserRole)


# ------------------------------------------------------------------- model


def test_an_enum_reference_resolves_to_its_members_on_load(tmp_path: Path) -> None:
    document = document_with_a_parameter(
        {"name": "BookType", "source": "query", "type": "string", "enum": "BookType"}
    )
    target = tmp_path / "catalog.json"
    document.save(target)
    parameter = load_catalog(target).services[0].endpoints[0].parameters[0]
    assert parameter.values == ("Agronomic", "Harvest")
    assert parameter.enum == "BookType"


def test_an_enum_reference_stays_live_so_editing_the_enum_updates_the_parameter(
    tmp_path: Path,
) -> None:
    document = document_with_a_parameter(
        {"name": "BookType", "source": "query", "type": "string", "enum": "BookType"}
    )
    document.enums["BookType"].append("Trial")
    target = tmp_path / "catalog.json"
    document.save(target)
    parameter = load_catalog(target).services[0].endpoints[0].parameters[0]
    assert parameter.values == ("Agronomic", "Harvest", "Trial")


def test_a_custom_list_is_used_verbatim(tmp_path: Path) -> None:
    document = document_with_a_parameter(
        {"name": "Mode", "source": "query", "type": "string", "values": ["a", "b"]}
    )
    target = tmp_path / "catalog.json"
    document.save(target)
    parameter = load_catalog(target).services[0].endpoints[0].parameters[0]
    assert parameter.values == ("a", "b")
    assert parameter.enum is None


def test_a_parameter_without_values_is_unconstrained() -> None:
    parameter = Parameter.from_dict(
        {"name": "Fields", "source": "query", "type": "string"}, ENUMS
    )
    assert parameter.values == ()
    assert parameter.enum is None


def test_a_dangling_enum_reference_resolves_to_no_values_and_is_reported() -> None:
    document = document_with_a_parameter(
        {"name": "BookType", "source": "query", "type": "string", "enum": "Nope"}
    )
    assert any("missing enum 'Nope'" in problem for problem in document.validate())


def test_renaming_an_enum_repoints_the_parameters_that_use_it() -> None:
    document = document_with_a_parameter(
        {"name": "BookType", "source": "query", "type": "string", "enum": "BookType"}
    )
    assert document.rename_schema("enum", "BookType", "BookKind") == 1
    assert document.services[0].endpoints[0].parameters[0]["enum"] == "BookKind"
    assert document.validate() == []


def test_deleting_an_enum_freezes_its_members_onto_the_parameter() -> None:
    """Clearing the reference outright would silently widen the parameter back
    to free text, losing the choice the author made."""
    document = document_with_a_parameter(
        {"name": "BookType", "source": "query", "type": "string", "enum": "BookType"}
    )
    assert document.remove_schema("enum", "BookType") == 1
    parameter = document.services[0].endpoints[0].parameters[0]
    assert "enum" not in parameter
    assert parameter["values"] == ["Agronomic", "Harvest"]
    assert document.validate() == []


def test_enum_usage_lists_parameters_as_well_as_fields() -> None:
    document = document_with_a_parameter(
        {"name": "BookType", "source": "query", "type": "string", "enum": "BookType"}
    )
    assert any(user.startswith("parameter:") for user in document.enum_usage("BookType"))


# ----------------------------------------------------------------- seeding


def test_seeding_a_constrained_parameter_uses_the_first_allowed_value() -> None:
    parameter = Parameter.from_dict(
        {"name": "BookType", "source": "query", "type": "string", "enum": "BookType"},
        ENUMS,
    )
    assert seed_parameter(parameter) == "Agronomic"


def test_seeding_ignores_a_sample_outside_the_allowed_values() -> None:
    """A generated sample would only ever produce a 400."""
    parameter = Parameter.from_dict(
        {
            "name": "BookType",
            "source": "query",
            "type": "string",
            "sample": "test-BookType",
            "enum": "BookType",
        },
        ENUMS,
    )
    assert seed_parameter(parameter) == "Agronomic"


def test_seeding_keeps_a_sample_that_is_one_of_the_allowed_values() -> None:
    parameter = Parameter.from_dict(
        {
            "name": "BookType",
            "source": "query",
            "type": "string",
            "sample": "Harvest",
            "enum": "BookType",
        },
        ENUMS,
    )
    assert seed_parameter(parameter) == "Harvest"


def test_seeding_an_unconstrained_parameter_is_unchanged() -> None:
    parameter = Parameter.from_dict(
        {"name": "Name", "source": "query", "type": "string", "sample": "kept"}, ENUMS
    )
    assert seed_parameter(parameter) == "kept"


# ------------------------------------------------------------ builder table


def test_the_parameter_table_has_a_values_column(app: QApplication) -> None:
    table = ParameterTable()
    headers = [
        table.table.horizontalHeaderItem(index).text()
        for index in range(table.table.columnCount())
    ]
    assert headers == ["Name", "Source", "Type", "Required", "Values", "Sample"]


def test_a_parameter_without_values_round_trips_unchanged(app: QApplication) -> None:
    original = [
        {"name": "id", "source": "path", "type": "integer", "required": True, "sample": 1}
    ]
    table = ParameterTable()
    table.set_enums(ENUMS)
    table.set_parameters(original)
    assert table.parameters() == original


def test_every_bundled_parameter_round_trips_unchanged(app: QApplication) -> None:
    """The table fills in a missing sample, but must not otherwise alter a
    parameter it merely displayed."""
    catalog = json.loads(BUNDLED.read_text(encoding="utf-8"))
    table = ParameterTable()
    table.set_enums(catalog.get("enums", {}))
    for service in catalog["services"]:
        for endpoint in service["endpoints"]:
            table.set_parameters(endpoint["parameters"])
            produced = [
                {key: value for key, value in item.items() if key != "sample"}
                for item in table.parameters()
            ]
            expected = [
                {key: value for key, value in item.items() if key != "sample"}
                for item in endpoint["parameters"]
            ]
            assert produced == expected, endpoint["id"]


def test_choosing_an_enum_serialises_a_reference_not_a_copy(app: QApplication) -> None:
    table = ParameterTable()
    table.set_enums(ENUMS)
    table.set_parameters([{"name": "BookType", "source": "query", "type": "string"}])
    table._values[row_key(table, 0)] = {"enum": "BookType", "values": None}
    parameter = table.parameters()[0]
    assert parameter["enum"] == "BookType"
    assert "values" not in parameter


def test_choosing_a_custom_list_serialises_the_values(app: QApplication) -> None:
    table = ParameterTable()
    table.set_enums(ENUMS)
    table.set_parameters([{"name": "Mode", "source": "query", "type": "string"}])
    table._values[row_key(table, 0)] = {"enum": None, "values": ["a", "b"]}
    parameter = table.parameters()[0]
    assert parameter["values"] == ["a", "b"]
    assert "enum" not in parameter


def test_the_values_button_summarises_the_choice(app: QApplication) -> None:
    table = ParameterTable()
    table.set_enums(ENUMS)
    table.set_parameters([{"name": "BookType", "source": "query", "type": "string"}])
    key = row_key(table, 0)
    assert button_in_cell(table.table, 0, 4).text() == "Any value..."
    table._values[key] = {"enum": "BookType", "values": None}
    table._refresh_values_button(key)
    assert button_in_cell(table.table, 0, 4).text() == "BookType"
    table._values[key] = {"enum": None, "values": ["a", "b"]}
    table._refresh_values_button(key)
    assert button_in_cell(table.table, 0, 4).text() == "2 values..."


def test_the_button_flags_an_enum_that_no_longer_exists(app: QApplication) -> None:
    table = ParameterTable()
    table.set_enums(ENUMS)
    table.set_parameters(
        [{"name": "BookType", "source": "query", "type": "string", "enum": "Gone"}]
    )
    assert button_in_cell(table.table, 0, 4).text() == "Gone (missing)"


def test_the_values_button_fits_inside_its_row(app: QApplication) -> None:
    """A default QPushButton is exactly the row height, so it overflows the cell."""
    table = ParameterTable()
    table.set_parameters([{"name": "BookType", "source": "query", "type": "string"}])
    table.resize(900, 200)
    button = button_in_cell(table.table, 0, 4)
    assert button.sizeHint().height() < table.table.rowHeight(0)


def test_deleting_a_row_keeps_the_values_with_their_own_parameters(
    app: QApplication,
) -> None:
    """Value lists are keyed by a row token, so removing a row must not shift
    one parameter's values onto its neighbour."""
    table = ParameterTable()
    table.set_enums(ENUMS)
    table.set_parameters(
        [
            {"name": "A", "source": "query", "type": "string"},
            {"name": "B", "source": "query", "type": "string", "enum": "BookType"},
            {"name": "C", "source": "query", "type": "string", "values": ["x"]},
        ]
    )
    table.table.selectRow(0)
    table._remove_selected()
    parameters = table.parameters()
    assert [item["name"] for item in parameters] == ["B", "C"]
    assert parameters[0]["enum"] == "BookType"
    assert parameters[1]["values"] == ["x"]


def test_the_values_dialog_reports_the_mode_it_was_opened_with(app: QApplication) -> None:
    dialog = ParameterValuesDialog(
        {"name": "BookType", "enum": "BookType"}, ENUMS
    )
    assert dialog.mode.currentText() == dialog.ENUM
    assert dialog.result_values() == {"enum": "BookType", "values": None}

    dialog = ParameterValuesDialog({"name": "Mode", "values": ["a"]}, ENUMS)
    assert dialog.mode.currentText() == dialog.CUSTOM
    assert dialog.result_values() == {"enum": None, "values": ["a"]}

    dialog = ParameterValuesDialog({"name": "Free"}, ENUMS)
    assert dialog.mode.currentText() == dialog.ANY
    assert dialog.result_values() == {"enum": None, "values": None}


def test_the_values_dialog_keeps_an_enum_it_cannot_resolve(app: QApplication) -> None:
    dialog = ParameterValuesDialog({"name": "BookType", "enum": "Gone"}, ENUMS)
    assert dialog.result_values()["enum"] == "Gone"


# ----------------------------------------------------------- request tester


def catalog_with_a_constrained_query_parameter(tmp_path: Path) -> tuple[Path, str, str]:
    catalog = json.loads(BUNDLED.read_text(encoding="utf-8"))
    for service in catalog["services"]:
        for endpoint in service["endpoints"]:
            for parameter in endpoint["parameters"]:
                if parameter["source"] == "query" and parameter["name"] not in (
                    "Filter",
                    "Sort",
                ):
                    parameter["enum"] = "BookTypeDataEnum"
                    target = tmp_path / "catalog.json"
                    target.write_text(json.dumps(catalog), encoding="utf-8")
                    return target, endpoint["id"], parameter["name"]
    raise AssertionError("the bundled catalog has no plain query parameter")


def test_a_constrained_parameter_renders_a_dropdown_in_the_request_grid(
    window, tmp_path: Path
) -> None:
    path, endpoint_id, name = catalog_with_a_constrained_query_parameter(tmp_path)
    assert window._load_catalog_from(path)
    item = window.endpoint_items[endpoint_id]
    item.setSelected(True)
    window._endpoint_selected()
    row = next(
        index for index, item in enumerate(window.request_editor._query_parameter_list) if item.name == name
    )
    picker = window.request_editor.query_parameters.cellWidget(row, 3)
    assert isinstance(picker, ValuePicker)
    assert [
        picker.combo.itemText(index) for index in range(picker.combo.count())
    ] == ["", "Agronomic", "Harvest"]


def test_the_dropdown_value_reaches_the_request(
    window, tmp_path: Path
) -> None:
    path, endpoint_id, name = catalog_with_a_constrained_query_parameter(tmp_path)
    assert window._load_catalog_from(path)
    window.endpoint_items[endpoint_id].setSelected(True)
    window._endpoint_selected()
    row = next(
        index for index, item in enumerate(window.request_editor._query_parameter_list) if item.name == name
    )
    window.request_editor.query_parameters.cellWidget(row, 3).setText("Harvest")
    assert window._current_values()[f"query:{name}"] == "Harvest"


def test_seeding_updates_the_dropdown_and_not_just_the_hidden_cell(
    window, tmp_path: Path
) -> None:
    """The cell is covered by the widget, so writing only the cell would leave
    the user looking at a stale value."""
    path, endpoint_id, name = catalog_with_a_constrained_query_parameter(tmp_path)
    assert window._load_catalog_from(path)
    window.endpoint_items[endpoint_id].setSelected(True)
    window._endpoint_selected()
    row = next(
        index for index, item in enumerate(window.request_editor._query_parameter_list) if item.name == name
    )
    window._seed_parameters()
    assert window.request_editor.query_parameters.cellWidget(row, 3).text() == "Agronomic"
    assert window.request_editor.query_parameters.item(row, 3).text() == "Agronomic"


def test_an_unconstrained_parameter_still_uses_a_plain_cell(
    window, tmp_path: Path
) -> None:
    path, endpoint_id, _ = catalog_with_a_constrained_query_parameter(tmp_path)
    assert window._load_catalog_from(path)
    window.endpoint_items[endpoint_id].setSelected(True)
    window._endpoint_selected()
    row = next(
        index for index, item in enumerate(window.request_editor._query_parameter_list)
        if not item.values and item.name not in window.request_editor._builder_parameters
    )
    assert window.request_editor.query_parameters.cellWidget(row, 3) is None
    assert window.request_editor.query_parameters.item(row, 3).flags() & Qt.ItemFlag.ItemIsEditable


def test_the_picker_stays_editable_so_invalid_values_can_be_tested(
    app: QApplication,
) -> None:
    picker = ValuePicker(["Agronomic"])
    picker.setText("NotAMember")
    assert picker.text() == "NotAMember"
