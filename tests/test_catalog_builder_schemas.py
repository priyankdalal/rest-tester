"""Schema authoring: the CRUD model, the editor widgets, and the builder pages."""

import json
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QComboBox, QInputDialog, QMessageBox

from api_tester.catalog import load_catalog
from api_tester.catalog_builder import (
    FILTER_OPERATORS,
    PAYLOAD_KINDS,
    SCHEMA_KINDS,
    CatalogDocument,
    EndpointDraft,
    ServiceDraft,
    display_name_for,
    new_filter_field,
    new_filter_schema,
    new_payload_field,
    new_payload_schema,
    new_response_schema,
    operators_for,
)
from api_tester.catalog_builder_ui import CatalogBuilderWindow
from api_tester.schema_editor import EnumValueEditor, FilterFieldTable, PayloadFieldTable


BUNDLED = Path(__file__).resolve().parents[1] / "data" / "api_catalog.json"


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    )
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
    )
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )


def document_with_schemas() -> CatalogDocument:
    document = CatalogDocument()
    service = ServiceDraft(name="Demo", default_base_url="https://localhost:7001")
    service.endpoints.append(
        EndpointDraft(
            service="Demo",
            controller="Brand",
            action="GetBrands",
            method="GET",
            path="/Brand",
            filter_entity="Brand",
        )
    )
    service.endpoints.append(
        EndpointDraft(
            service="Demo",
            controller="Brand",
            action="CreateBrand",
            method="POST",
            path="/Brand",
            payload_schema="BrandPayload",
        )
    )
    service.endpoints.append(
        EndpointDraft(
            service="Demo",
            controller="Brand",
            action="GetBrand",
            method="GET",
            path="/Brand/{id}",
            parameters=[
                {"name": "id", "source": "path", "type": "integer", "required": True}
            ],
            response_schema="BrandResponse",
        )
    )
    document.services.append(service)
    document.filter_schemas["Brand"] = new_filter_schema("Brand")
    document.payload_schemas["BrandPayload"] = new_payload_schema("BrandPayload")
    document.response_schemas["BrandResponse"] = new_response_schema("BrandResponse")
    return document


# --------------------------------------------------------------- vocabulary


def test_filter_operators_match_the_generator_table() -> None:
    """The builder mirrors the extractor's table so frozen builds work without
    tools/; if they drift, authored filters stop matching generated ones."""
    try:
        import tools.schema_extractor as extractor
    except Exception as exc:
        pytest.skip(f"tools.schema_extractor is not importable yet: {exc}")
    mirrored = {key: [dict(item) for item in value] for key, value in FILTER_OPERATORS.items()}
    assert mirrored == extractor.SUPPORTED_OPERATIONS


def test_operators_for_returns_copies_so_editing_one_field_is_isolated() -> None:
    first = operators_for("Text")
    first[0]["value"] = "mutated"
    assert operators_for("Text")[0]["value"] != "mutated"


def test_display_name_splits_pascal_case_but_leaves_single_words() -> None:
    assert display_name_for("TrialLocationId") == "Trial Location Id"
    assert display_name_for("Id") == "Id"


def test_new_filter_field_carries_the_operators_for_its_data_type() -> None:
    field = new_filter_field("StartDate", "Date")
    assert [operator["value"] for operator in field["operators"]] == [
        operator["value"] for operator in FILTER_OPERATORS["Date"]
    ]


# ---------------------------------------------------------------- document


def test_schema_usage_lists_the_endpoints_that_reference_a_schema() -> None:
    document = document_with_schemas()
    assert len(document.schema_usage("filter", "Brand")) == 1
    assert len(document.schema_usage("payload", "BrandPayload")) == 1
    assert len(document.schema_usage("response", "BrandResponse")) == 1
    assert document.schema_usage("form", "BrandPayload") == []


def test_renaming_a_schema_repoints_every_endpoint() -> None:
    document = document_with_schemas()
    document.rename_schema("filter", "Brand", "BrandEntity")
    assert "Brand" not in document.filter_schemas
    assert document.services[0].endpoints[0].filter_entity == "BrandEntity"


def test_removing_a_schema_clears_the_references_instead_of_dangling() -> None:
    document = document_with_schemas()
    document.remove_schema("payload", "BrandPayload")
    assert document.services[0].endpoints[1].payload_schema is None
    assert document.validate() == []


def test_removing_a_response_schema_clears_the_reference_instead_of_dangling() -> None:
    document = document_with_schemas()
    document.remove_schema("response", "BrandResponse")
    assert document.services[0].endpoints[2].response_schema is None
    assert document.validate() == []


def test_duplicating_a_schema_deep_copies_the_fields() -> None:
    document = document_with_schemas()
    document.filter_schemas["Brand"]["fields"].append(new_filter_field("Name", "Text"))
    document.duplicate_schema("filter", "Brand", "BrandCopy")
    document.filter_schemas["BrandCopy"]["fields"][-1]["name"] = "Renamed"
    assert document.filter_schemas["Brand"]["fields"][-1]["name"] == "Name"


def test_adding_a_schema_twice_is_rejected() -> None:
    document = document_with_schemas()
    with pytest.raises(Exception):
        document.add_schema("filter", "Brand")


def test_validation_reports_a_reference_to_a_missing_schema() -> None:
    document = document_with_schemas()
    document.services[0].endpoints[0].filter_entity = "Nope"
    problems = document.validate()
    assert any("missing filter entity 'Nope'" in problem for problem in problems)


def test_validation_reports_a_missing_response_schema_reference() -> None:
    document = document_with_schemas()
    document.services[0].endpoints[2].response_schema = "Nope"
    problems = document.validate()
    assert any("missing response schema 'Nope'" in problem for problem in problems)


def test_validation_reports_an_unsupported_filter_data_type() -> None:
    document = document_with_schemas()
    field = new_filter_field("Name", "Text")
    field["data_type"] = "Blob"
    document.filter_schemas["Brand"]["fields"].append(field)
    assert any("Blob" in problem for problem in document.validate())


def test_the_bundled_catalog_has_no_schema_problems() -> None:
    assert CatalogDocument.load(BUNDLED).validate() == []


# ------------------------------------------------------------ editor widgets


def test_filter_field_table_round_trips_every_bundled_filter_schema(app: QApplication) -> None:
    catalog = json.loads(BUNDLED.read_text(encoding="utf-8"))
    table = FilterFieldTable()
    for name, schema in catalog["filter_schemas"].items():
        table.set_fields(schema.get("fields", []), catalog.get("enums", {}))
        assert table.fields() == schema.get("fields", []), name


def test_payload_field_table_round_trips_every_bundled_payload_schema(
    app: QApplication,
) -> None:
    catalog = json.loads(BUNDLED.read_text(encoding="utf-8"))
    table = PayloadFieldTable()
    for name, schema in catalog["payload_schemas"].items():
        table.set_fields(schema.get("fields", []), catalog.get("enums", {}))
        assert table.fields() == schema.get("fields", []), name


def test_payload_field_table_round_trips_every_bundled_form_schema(app: QApplication) -> None:
    catalog = json.loads(BUNDLED.read_text(encoding="utf-8"))
    table = PayloadFieldTable()
    for name, schema in catalog["form_schemas"].items():
        table.set_fields(schema.get("fields", []), catalog.get("enums", {}))
        assert table.fields() == schema.get("fields", []), name


def test_payload_field_table_round_trips_a_response_schema(app: QApplication) -> None:
    table = PayloadFieldTable()
    schema = new_response_schema("BrandResponse")
    table.set_fields(schema["fields"], {})
    assert table.fields() == schema["fields"]


def test_changing_a_filter_data_type_resets_the_operators(app: QApplication) -> None:
    table = FilterFieldTable()
    table.set_fields([new_filter_field("Name", "Text")], {})
    table.table.cellWidget(0, 2).setCurrentText("Date")
    assert [operator["value"] for operator in table.fields()[0]["operators"]] == [
        operator["value"] for operator in FILTER_OPERATORS["Date"]
    ]


def test_changing_a_data_type_keeps_a_hand_written_clr_type(app: QApplication) -> None:
    table = FilterFieldTable()
    field = new_filter_field("Code", "Text")
    field["clr_type"] = "BrandCode"
    table.set_fields([field], {})
    table.table.cellWidget(0, 2).setCurrentText("Number")
    assert table.fields()[0]["clr_type"] == "BrandCode"


def test_deleting_a_row_keeps_the_remaining_operators_with_their_fields(
    app: QApplication,
) -> None:
    """Side data is keyed by a row token, not the row index, so deletion must
    not shift operators onto a neighbouring field."""
    table = FilterFieldTable()
    table.set_fields(
        [
            new_filter_field("A", "Text"),
            new_filter_field("B", "Date"),
            new_filter_field("C", "Flag"),
        ],
        {},
    )
    table.table.selectRow(0)
    table._remove_selected()
    fields = table.fields()
    assert [field["name"] for field in fields] == ["B", "C"]
    assert fields[0]["data_type"] == "Date"
    assert [operator["value"] for operator in fields[1]["operators"]] == [
        operator["value"] for operator in FILTER_OPERATORS["Flag"]
    ]


def test_enum_value_editor_round_trips_values(app: QApplication) -> None:
    editor = EnumValueEditor()
    editor.set_values(["Agronomic", "Harvest"])
    assert editor.values() == ["Agronomic", "Harvest"]


def test_payload_field_table_drops_constraints_when_they_are_blank(
    app: QApplication,
) -> None:
    table = PayloadFieldTable()
    table.set_fields([new_payload_field("Name", "text")], {})
    assert "constraints" not in table.fields()[0]


def test_every_payload_kind_survives_a_round_trip(app: QApplication) -> None:
    table = PayloadFieldTable()
    fields = [new_payload_field(f"F{index}", kind) for index, kind in enumerate(PAYLOAD_KINDS)]
    table.set_fields(fields, {})
    assert [field["kind"] for field in table.fields()] == list(PAYLOAD_KINDS)


# ------------------------------------------------------------- builder pages


def test_the_tree_renders_a_schemas_branch_with_every_store(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow(BUNDLED)
    root = window._schema_root
    assert root.childCount() == len(SCHEMA_KINDS)
    assert root.child(0).text(1) == str(len(window.document.filter_schemas))


def test_selecting_a_filter_schema_opens_its_page_with_the_fields(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow(BUNDLED)
    window._select_schema_item("filter", "Brand")
    assert window.filter_schema_name.text() == "Brand"
    assert window.filter_fields.fields() == window.document.schema("filter", "Brand")["fields"]


def test_applying_a_filter_schema_writes_the_edited_fields_back(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow(BUNDLED)
    window._select_schema_item("filter", "Brand")
    before = len(window.document.schema("filter", "Brand")["fields"])
    window.filter_fields.append(new_filter_field("Extra", "Date"))
    window._apply_filter_schema()
    assert len(window.document.schema("filter", "Brand")["fields"]) == before + 1


def test_form_schemas_stay_without_a_kind_key_after_editing(
    app: QApplication, quiet_dialogs: None
) -> None:
    """builders.py distinguishes form from payload schemas by that key."""
    window = CatalogBuilderWindow(BUNDLED)
    name = next(iter(window.document.form_schemas))
    window._select_schema_item("form", name)
    window._apply_payload_schema()
    assert "kind" not in window.document.schema("form", name)


def test_payload_schemas_keep_their_object_kind_after_editing(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow(BUNDLED)
    window._select_schema_item("payload", "BrandPayload")
    window._apply_payload_schema()
    assert window.document.schema("payload", "BrandPayload")["kind"] == "object"


def test_response_schemas_keep_their_object_kind_after_editing(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow()
    window.document = document_with_schemas()
    window._populate()
    window._select_schema_item("response", "BrandResponse")
    window._apply_payload_schema()
    assert window.document.schema("response", "BrandResponse")["kind"] == "object"


def test_the_endpoint_payload_schema_picker_is_closed_to_typing(
    app: QApplication, quiet_dialogs: None
) -> None:
    """A typed name would become a dangling reference the tester ignores."""
    window = CatalogBuilderWindow(BUNDLED)
    assert not window.endpoint_payload_schema.isEditable()
    assert not window.endpoint_form_schema.isEditable()
    assert not window.endpoint_response_schema.isEditable()
    assert not window.endpoint_filter_entity.isEditable()


def test_the_payload_type_box_suggests_values_but_stays_free_text(
    app: QApplication, quiet_dialogs: None
) -> None:
    """payload_type holds a CLR type such as JsonPatchDocument<Brand>, not a
    schema name, so it cannot be a closed list."""
    window = CatalogBuilderWindow(BUNDLED)
    assert isinstance(window.endpoint_payload_type, QComboBox)
    assert window.endpoint_payload_type.isEditable()
    endpoint = next(e for e in window.document.iter_endpoints() if e.payload_type)
    window._select_endpoint_item(endpoint.id)
    assert window.endpoint_payload_type.currentText() == endpoint.payload_type
    window.endpoint_payload_type.setCurrentText("List<Custom>")
    window._apply_endpoint()
    assert endpoint.payload_type == "List<Custom>"


def test_creating_a_schema_from_the_endpoint_page_selects_it(
    app: QApplication, quiet_dialogs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    window = CatalogBuilderWindow(BUNDLED)
    endpoint = next(e for e in window.document.iter_endpoints() if e.method == "POST")
    window._select_endpoint_item(endpoint.id)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("BrandNewPayload", True)))
    window._create_schema_for_endpoint("payload", window.endpoint_payload_schema)
    assert "BrandNewPayload" in window.document.payload_schemas
    assert window.endpoint_payload_schema.currentText() == "BrandNewPayload"
    window._apply_endpoint()
    assert endpoint.payload_schema == "BrandNewPayload"


def test_response_schema_crud_flows_through_generic_dispatch() -> None:
    document = document_with_schemas()
    created = document.add_schema("response", "DetectedResponse")
    assert created["name"] == "DetectedResponse"
    document.duplicate_schema("response", "DetectedResponse", "DetectedResponseCopy")
    assert "DetectedResponseCopy" in document.response_schemas
    repointed = document.rename_schema("response", "BrandResponse", "BrandReply")
    assert repointed == 1
    assert document.services[0].endpoints[2].response_schema == "BrandReply"
    cleared = document.remove_schema("response", "BrandReply")
    assert cleared == 1
    assert document.services[0].endpoints[2].response_schema is None


def test_prune_unused_schemas_removes_unreferenced_response_schemas() -> None:
    document = document_with_schemas()
    document.response_schemas["UnusedResponse"] = new_response_schema("UnusedResponse")
    removed = document.prune_unused_schemas()
    assert removed >= 1
    assert "UnusedResponse" not in document.response_schemas


def test_an_authored_schema_survives_a_save_and_loads_in_the_tester(
    app: QApplication, quiet_dialogs: None, tmp_path: Path
) -> None:
    window = CatalogBuilderWindow(BUNDLED)
    window.document.add_schema("filter", "Authored")
    window.document.filter_schemas["Authored"]["fields"] = [
        new_filter_field("Name", "Text"),
        new_filter_field("Created", "Date"),
    ]
    target = tmp_path / "catalog.json"
    window.document.save(target)
    catalog = load_catalog(target)
    schema = catalog.filter_schema("Authored")
    assert [field.name for field in schema.fields] == ["Name", "Created"]
    assert schema.fields[1].operators
