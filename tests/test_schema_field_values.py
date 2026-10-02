"""Allowed values on schema fields, plus Back/Forward navigation in the builder."""

import json
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from api_tester.catalog import load_catalog
from api_tester.catalog_builder import CatalogDocument, EndpointDraft
from api_tester.catalog_builder_ui import CatalogBuilderWindow
from api_tester.schema_editor import (
    AllowedValuesWidget,
    FieldDetailsDialog,
    FilterFieldTable,
    PayloadFieldTable,
    values_caption,
)
from api_tester.widgets import button_in_cell

BUNDLED = Path(__file__).resolve().parents[1] / "data" / "api_catalog.json"
ENUMS = {"BookType": ["Agronomic", "Harvest"], "Status": ["Draft", "Active"]}


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("information", "warning", "question"):
        monkeypatch.setattr(
            QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )


@pytest.fixture(scope="module")
def bundled() -> dict:
    return json.loads(BUNDLED.read_text(encoding="utf-8"))


def text_field(**extra) -> dict:
    field = {
        "name": "Title",
        "clr_type": "string",
        "nullable": False,
        "required": True,
        "display_name": "Title",
        "kind": "text",
    }
    field.update(extra)
    return field


def filter_field(**extra) -> dict:
    field = {
        "name": "BookType",
        "display_name": "Book Type",
        "clr_type": "string",
        "data_type": "string",
        "filterable": True,
        "sortable": True,
        "nullable": False,
        "operators": [],
        "lov": [],
    }
    field.update(extra)
    return field


# ------------------------------------------------------- AllowedValuesWidget


def test_any_mode_leaves_a_field_unconstrained(app: QApplication) -> None:
    field = text_field()
    widget = AllowedValuesWidget(field, ENUMS)
    widget.apply_to(field, keep_lov=False)
    assert "enum" not in field
    assert "lov" not in field


def test_enum_mode_stores_a_reference_and_caches_its_members(app: QApplication) -> None:
    field = text_field()
    widget = AllowedValuesWidget(field, ENUMS)
    widget.mode.setCurrentText(AllowedValuesWidget.ENUM)
    widget.enum_combo.setCurrentText("BookType")
    widget.apply_to(field)
    assert field["enum"] == "BookType"
    # The cache keeps the JSON readable without resolving the reference.
    assert field["lov"] == ["Agronomic", "Harvest"]


def test_custom_mode_stores_a_list_and_drops_any_enum(app: QApplication) -> None:
    field = text_field(enum="BookType", lov=["One", "Two"])
    widget = AllowedValuesWidget(field, ENUMS)
    widget.mode.setCurrentText(AllowedValuesWidget.CUSTOM)
    widget.apply_to(field)
    assert "enum" not in field
    assert field["lov"] == ["One", "Two"]


def test_keep_lov_preserves_the_key_for_shapes_that_require_it(app: QApplication) -> None:
    field = filter_field()
    widget = AllowedValuesWidget(field, ENUMS)
    widget.apply_to(field, keep_lov=True)
    assert field["lov"] == []


def test_a_widget_reopens_on_the_mode_the_field_is_in(app: QApplication) -> None:
    assert AllowedValuesWidget(text_field(), ENUMS).mode.currentText() == AllowedValuesWidget.ANY
    enum_field = text_field(enum="BookType", lov=["Agronomic"])
    assert AllowedValuesWidget(enum_field, ENUMS).mode.currentText() == AllowedValuesWidget.ENUM
    listed = text_field(lov=["One"])
    assert AllowedValuesWidget(listed, ENUMS).mode.currentText() == AllowedValuesWidget.CUSTOM


def test_caption_names_the_enum_counts_a_list_and_flags_a_dangling_reference() -> None:
    assert values_caption(text_field(), ENUMS) == "Any value"
    assert values_caption(text_field(lov=["a", "b"]), ENUMS) == "2 value(s)"
    assert values_caption(text_field(enum="BookType"), ENUMS) == "BookType"
    assert values_caption(text_field(enum="Ghost"), ENUMS) == "Ghost (missing)"


# ----------------------------------------------------------- details dialog


def test_a_plain_field_edits_values_and_constraints_together(app: QApplication) -> None:
    dialog = FieldDetailsDialog(text_field(), ENUMS)
    assert dialog.values_widget is not None, "allowed values should be offered"
    assert dialog.minimum is not None, "constraints should be offered alongside"
    dialog.values_widget.mode.setCurrentText(AllowedValuesWidget.ENUM)
    dialog.values_widget.enum_combo.setCurrentText("Status")
    dialog.maximum.setText("40")
    result = dialog.result_field()
    assert result["enum"] == "Status"
    assert result["constraints"]["maximum"] == 40


def test_a_plain_field_gains_no_empty_lov(app: QApplication) -> None:
    result = FieldDetailsDialog(text_field(), ENUMS).result_field()
    assert "lov" not in result, "an unconstrained text field must keep its original shape"


def test_an_enum_field_always_keeps_its_lov(app: QApplication) -> None:
    field = text_field(kind="enum", lov=["Draft"])
    result = FieldDetailsDialog(field, ENUMS).result_field()
    assert result["lov"] == ["Draft"]


def test_an_array_constrains_its_elements(app: QApplication) -> None:
    field = text_field(kind="array", item={"kind": "text", "clr_type": "string"})
    dialog = FieldDetailsDialog(field, ENUMS)
    assert dialog.values_widget is None, "an array itself holds no values"
    dialog.item_values.mode.setCurrentText(AllowedValuesWidget.ENUM)
    dialog.item_values.enum_combo.setCurrentText("BookType")
    result = dialog.result_field()
    assert result["item"]["enum"] == "BookType"
    assert "enum" not in result, "the array itself holds no values, its elements do"


def test_an_object_edits_nested_fields_not_values(app: QApplication) -> None:
    field = text_field(kind="object", fields=[])
    dialog = FieldDetailsDialog(field, ENUMS)
    assert dialog.values_widget is None
    assert dialog.minimum is None


def test_the_button_caption_mentions_values_and_constraints(app: QApplication) -> None:
    table = PayloadFieldTable()
    table.enums = ENUMS
    table.append(text_field(enum="BookType", constraints={"maximum": 10}))
    button = button_in_cell(table.table, 0, 6)
    assert button.text() == "BookType, constrained"


def test_an_unconfigured_field_invites_the_user_in(app: QApplication) -> None:
    table = PayloadFieldTable()
    table.enums = ENUMS
    table.append(text_field())
    assert button_in_cell(table.table, 0, 6).text() == "Values, constraints..."


def test_the_details_button_fits_inside_its_row(app: QApplication) -> None:
    """A default QPushButton is exactly the row height, so it overflows the cell."""
    table = PayloadFieldTable()
    table.enums = ENUMS
    table.append(text_field())
    table.resize(1000, 200)
    button = button_in_cell(table.table, 0, 6)
    assert button.sizeHint().height() < table.table.rowHeight(0)


def test_the_filter_row_buttons_fit_inside_their_row(app: QApplication) -> None:
    table = FilterFieldTable()
    table.set_fields(
        [
            {
                "name": "BookType",
                "display": "Book type",
                "data_type": "string",
                "clr_type": "string",
                "filterable": True,
                "sortable": True,
            }
        ],
        ENUMS,
    )
    table.resize(1200, 200)
    for column in (6, 7):
        button = button_in_cell(table.table, 0, column)
        assert button is not None
        assert button.sizeHint().height() < table.table.rowHeight(0)


# ------------------------------------------------------------ filter fields


def test_a_filter_field_serialises_its_enum_beside_the_canonical_keys(
    app: QApplication,
) -> None:
    table = FilterFieldTable()
    table.append(filter_field(enum="BookType", lov=["Agronomic", "Harvest"]))
    stored = table.fields()[0]
    assert stored["enum"] == "BookType"
    for key in ("name", "display_name", "clr_type", "data_type", "sortable",
                "nullable", "operators", "lov"):
        assert key in stored, f"{key} is part of the filter field contract"


def test_a_filter_field_without_an_enum_omits_the_key(app: QApplication) -> None:
    table = FilterFieldTable()
    table.append(filter_field())
    assert "enum" not in table.fields()[0]


# ------------------------------------------------------ load-time resolution


def test_an_enum_reference_resolves_live_across_all_three_schema_kinds(
    app: QApplication, tmp_path: Path
) -> None:
    document = CatalogDocument.load(BUNDLED)
    document.enums["Freshly"] = ["Alpha", "Beta"]

    filter_target = document.filter_schemas["OnFarmAgronomicData"]["fields"][0]
    filter_target.update({"enum": "Freshly", "lov": ["stale"]})
    payload_name = next(iter(document.payload_schemas))
    document.payload_schemas[payload_name]["fields"].append(
        text_field(name="ZValues", enum="Freshly", lov=["stale"])
    )

    path = tmp_path / "catalog.json"
    document.save(path)
    catalog = load_catalog(path)

    resolved = catalog.filter_schema("OnFarmAgronomicData").field(filter_target["name"])
    assert list(resolved.lov) == ["Alpha", "Beta"], "the stale cache must be overridden"
    payload = next(
        item for item in catalog.payload_schema(payload_name)["fields"] if item["name"] == "ZValues"
    )
    assert payload["lov"] == ["Alpha", "Beta"]


def test_resolution_reaches_nested_fields_and_array_items(tmp_path: Path) -> None:
    document = CatalogDocument.load(BUNDLED)
    document.enums["Freshly"] = ["Alpha", "Beta"]
    name = next(iter(document.payload_schemas))
    document.payload_schemas[name]["fields"].append(
        text_field(
            name="ZNested",
            kind="object",
            fields=[
                text_field(name="Inner", enum="Freshly", lov=[]),
                text_field(
                    name="Many",
                    kind="array",
                    item={"kind": "text", "clr_type": "string", "enum": "Freshly", "lov": []},
                ),
            ],
        )
    )
    path = tmp_path / "catalog.json"
    document.save(path)
    catalog = load_catalog(path)

    nested = next(
        item for item in catalog.payload_schema(name)["fields"] if item["name"] == "ZNested"
    )
    assert nested["fields"][0]["lov"] == ["Alpha", "Beta"]
    assert nested["fields"][1]["item"]["lov"] == ["Alpha", "Beta"]


def test_the_bundled_schemas_still_round_trip_losslessly(
    app: QApplication, bundled: dict
) -> None:
    """The field tables must not silently reshape a schema they merely display."""
    mismatches = []
    for key, table_type in (("filter_schemas", FilterFieldTable), ("payload_schemas", PayloadFieldTable)):
        for name, schema in bundled.get(key, {}).items():
            table = table_type()
            for field in schema.get("fields", []):
                table.append(field)
            if table.fields() != schema.get("fields", []):
                mismatches.append(f"{key}:{name}")
    assert mismatches == []


@pytest.mark.parametrize("kind", ["payload", "form", "response"])
def test_field_details_apply_and_save_keep_values_and_constraints(
    app: QApplication,
    quiet_dialogs: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    window = CatalogBuilderWindow()
    window.document.enums.update(ENUMS)
    service = window.document.add_service("Demo")
    service.endpoints.append(
        EndpointDraft(service="Demo", controller="Items", action="Get", method="GET", path="/Items")
    )
    schema = window.document.add_schema(kind, "Authored")
    schema["fields"] = [text_field()]
    window._populate()
    window._select_schema_item(kind, "Authored")

    def edit_details(dialog: FieldDetailsDialog) -> QDialog.DialogCode:
        dialog.values_widget.mode.setCurrentText(AllowedValuesWidget.ENUM)
        dialog.values_widget.enum_combo.setCurrentText("Status")
        dialog.maximum.setText("40")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(FieldDetailsDialog, "exec", edit_details)
    button_in_cell(window.payload_fields.table, 0, 6).click()
    window._apply_payload_schema()
    target = tmp_path / f"{kind}.json"
    assert window._write(target)
    assert not window.dirty

    reloaded = CatalogDocument.load(target)
    field = reloaded.schema(kind, "Authored")["fields"][0]
    assert field["enum"] == "Status"
    assert field["lov"] == ["Draft", "Active"]
    assert field["constraints"] == {"maximum": 40}
    assert getattr(load_catalog(target), f"{kind}_schema")("Authored")["fields"][0] == field

    window.open_catalog(target)
    window._select_schema_item(kind, "Authored")
    assert window.payload_fields.fields() == [field]
    assert button_in_cell(window.payload_fields.table, 0, 6).text() == "Status, constrained"
    window.close()


# ---------------------------------------------------------- enum lifecycle


def document_with_field_references() -> tuple[CatalogDocument, dict, dict]:
    document = CatalogDocument.load(BUNDLED)
    document.enums["Freshly"] = ["Alpha", "Beta"]
    filter_target = document.filter_schemas["OnFarmAgronomicData"]["fields"][0]
    filter_target.update({"enum": "Freshly", "lov": []})
    payload_target = text_field(name="ZValues", enum="Freshly", lov=[])
    next(iter(document.payload_schemas.values()))["fields"].append(payload_target)
    return document, filter_target, payload_target


def test_renaming_an_enum_repoints_every_schema_field() -> None:
    document, filter_target, payload_target = document_with_field_references()
    document.rename_schema("enum", "Freshly", "Renamed")
    assert filter_target["enum"] == "Renamed"
    assert payload_target["enum"] == "Renamed"
    assert document.validate() == []


def test_deleting_an_enum_freezes_its_members_into_each_field() -> None:
    document, filter_target, payload_target = document_with_field_references()
    document.remove_schema("enum", "Freshly")
    assert "enum" not in filter_target
    assert filter_target["lov"] == ["Alpha", "Beta"], "a frozen field keeps offering its values"
    assert payload_target["lov"] == ["Alpha", "Beta"]
    assert document.validate() == []


def test_usage_lists_each_field_once_under_a_readable_path() -> None:
    document, _, _ = document_with_field_references()
    users = document.enum_usage("Freshly")
    assert len(users) == len(set(users)), "a field matched twice must still be listed once"
    assert any(entry.endswith(".ZValues") for entry in users)
    assert not any(".." in entry for entry in users)
    for entry in users:
        kind, _, rest = entry.partition(":")
        assert kind in {"parameter", "filter", "payload", "form"}
        assert rest.count(".") >= 1, "a path names its schema and its field"


def test_a_dangling_field_reference_is_reported() -> None:
    document, filter_target, _ = document_with_field_references()
    filter_target["enum"] = "Ghost"
    problems = document.validate()
    assert any("Ghost" in problem for problem in problems)


# -------------------------------------------------------------- navigation


def visit(window: CatalogBuilderWindow, kind: str, reference) -> None:
    """Mirrors a tree click: record the node, then render it."""
    window._record_history(kind, reference)
    window._show(kind, reference)


def test_back_and_forward_walk_the_visited_nodes(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow()
    window.document = CatalogDocument.load(BUNDLED)
    window._populate()

    name = next(iter(window.document.filter_schemas))
    visit(window, "schemagroup", "filter")
    first = window.editor.currentIndex()
    visit(window, "schema", ("filter", name))
    second = window.editor.currentIndex()
    assert window.back_action.isEnabled()

    window.go_back()
    assert window.editor.currentIndex() == first
    assert window.forward_action.isEnabled()

    window.go_forward()
    assert window.editor.currentIndex() == second
    assert not window.forward_action.isEnabled()
    window.close()


def test_the_breadcrumb_names_the_node_we_would_return_to(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow()
    window.document = CatalogDocument.load(BUNDLED)
    window._populate()

    name = next(iter(window.document.filter_schemas))
    visit(window, "schemagroup", "filter")
    visit(window, "schema", ("filter", name))
    assert window.back_label.text().startswith("<")
    assert name not in window.back_label.text(), "the breadcrumb names the previous node"
    assert window.back_label.isVisible() or not window.isVisible()
    window.close()


def test_a_deleted_node_is_dropped_from_the_history(
    app: QApplication, quiet_dialogs: None
) -> None:
    window = CatalogBuilderWindow()
    window.document = CatalogDocument.load(BUNDLED)
    window._populate()

    name = next(iter(window.document.filter_schemas))
    visit(window, "schema", ("filter", name))
    visit(window, "catalog", None)
    window.document.remove_schema("filter", name)
    window._populate()

    window.go_back()
    assert window.editor.currentIndex() >= 0, "a missing node must not strand the user"
    assert ("schema", ("filter", name)) not in window._history
    window.close()
