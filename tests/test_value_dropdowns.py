"""A field that documents its values is edited with a dropdown, not a text box."""

from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QLineEdit, QPlainTextEdit

from api_tester.builders import FilterValueEdit, FormBuilder, PayloadForm, QueryBuilder
from api_tester.catalog import load_catalog


BUNDLED = Path(__file__).resolve().parents[1] / "data" / "api_catalog.json"


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def field(name: str, kind: str = "text", **extra) -> dict:
    payload = {
        "name": name,
        "kind": kind,
        "clr_type": "string",
        "required": True,
        "nullable": False,
        "display_name": name,
    }
    payload.update(extra)
    return payload


FIELDS = [
    field("Plain"),
    field("Listed", lov=["receipt", "substitue"]),
    field("Classic", kind="enum", lov=["A", "B"]),
    field("Counted", kind="integer", clr_type="int", lov=["10", "20"]),
    field("Ranked", kind="number", clr_type="decimal", lov=["1.5", "2.5"]),
    field("Flag", kind="boolean", clr_type="bool", required=False),
    field("Blob", kind="json", lov=["{}"]),
]


def editors(builder) -> dict:
    return {item[2]["name"]: item[1] for item in builder._editors}


def payload_form(app: QApplication) -> PayloadForm:
    builder = PayloadForm()
    builder.set_schema({"kind": "object", "name": "S", "clr_type": "S", "fields": FIELDS})
    return builder


def test_payload_field_actions_are_icon_only_and_theme_aware(app: QApplication) -> None:
    from api_tester import theme
    from PyQt6.QtWidgets import QPushButton

    builder = payload_form(app)
    buttons = [
        button
        for button in builder.container.findChildren(QPushButton)
        if button.property("payloadFieldAction") == "seed"
    ]
    policies = [
        (
            button.sizePolicy().horizontalPolicy(),
            button.sizePolicy().verticalPolicy(),
        )
        for button in buttons
    ]
    assert buttons
    assert all(button.text() == "" for button in buttons)
    assert all(button.toolTip() for button in buttons)
    assert all(button.accessibleName() == button.toolTip() for button in buttons)
    assert all(button.width() == 56 for button in buttons)
    theme.apply_theme(app, "Light")
    builder.refresh_theme()
    light_keys = [button.icon().cacheKey() for button in buttons]
    theme.apply_theme(app, "Dark")
    builder.refresh_theme()
    assert [button.icon().cacheKey() for button in buttons] != light_keys
    assert policies == [
        (
            button.sizePolicy().horizontalPolicy(),
            button.sizePolicy().verticalPolicy(),
        )
        for button in buttons
    ]
    theme.apply_theme(app, "Light")


# ------------------------------------------------------------- payload editor


def test_a_documented_field_renders_a_dropdown_whatever_its_kind(
    app: QApplication,
) -> None:
    widgets = editors(payload_form(app))
    assert isinstance(widgets["Listed"], QComboBox), "a text field with values needs a dropdown"
    assert isinstance(widgets["Classic"], QComboBox)
    assert isinstance(widgets["Counted"], QComboBox), "a spin box cannot offer a choice"
    assert isinstance(widgets["Ranked"], QComboBox)


def test_an_undocumented_field_keeps_its_plain_editor(app: QApplication) -> None:
    widgets = editors(payload_form(app))
    assert isinstance(widgets["Plain"], QLineEdit)
    assert isinstance(widgets["Flag"], QCheckBox)


def test_a_structured_field_is_never_turned_into_a_dropdown(app: QApplication) -> None:
    """A JSON body is not one of a set of scalars, even if the catalog lists one."""
    assert isinstance(editors(payload_form(app))["Blob"], QPlainTextEdit)


def test_the_dropdown_offers_exactly_the_documented_values(app: QApplication) -> None:
    combo = editors(payload_form(app))["Listed"]
    offered = [combo.itemText(index) for index in range(combo.count())]
    assert "receipt" in offered and "substitue" in offered
    assert combo.isEditable(), "an undocumented value must still be sendable"


def test_a_numeric_choice_is_sent_as_a_number(app: QApplication) -> None:
    builder = payload_form(app)
    widgets = editors(builder)
    widgets["Counted"].setCurrentText("20")
    widgets["Ranked"].setCurrentText("2.5")
    body = builder.payload()
    assert body["Counted"] == 20 and isinstance(body["Counted"], int)
    assert body["Ranked"] == 2.5 and isinstance(body["Ranked"], float)


def test_an_undocumented_numeric_value_is_still_sent(app: QApplication) -> None:
    builder = payload_form(app)
    editors(builder)["Counted"].setCurrentText("99")
    assert builder.payload()["Counted"] == 99


def test_an_unparsable_value_reaches_the_api_to_exercise_its_validation(
    app: QApplication,
) -> None:
    builder = payload_form(app)
    editors(builder)["Counted"].setCurrentText("abc")
    assert builder.payload()["Counted"] == "abc"


def test_an_empty_choice_is_omitted_rather_than_sent_as_a_blank(
    app: QApplication,
) -> None:
    builder = payload_form(app)
    editors(builder)["Listed"].setCurrentText("")
    assert builder.payload()["Listed"] is None


def test_seeding_lands_in_the_dropdown(app: QApplication) -> None:
    builder = payload_form(app)
    builder.seed()
    body = builder.payload()
    assert body["Listed"] in {"receipt", "substitue"}, "seeding must not invent an illegal value"
    assert body["Counted"] in {10, 20}
    assert isinstance(body["Counted"], int)


# ---------------------------------------------------------------- form editor


def test_the_multipart_editor_offers_the_same_dropdowns(app: QApplication) -> None:
    builder = FormBuilder()
    builder.set_schema({"fields": FIELDS})
    widgets = editors(builder)
    assert isinstance(widgets["Listed"], QComboBox)
    assert isinstance(widgets["Counted"], QComboBox)
    assert isinstance(widgets["Plain"], QLineEdit)


# --------------------------------------------------------------- query builder


@pytest.fixture(scope="module")
def schema():
    return load_catalog(BUNDLED).filter_schema("OnFarmAgronomicData")


def test_a_listed_filter_field_offers_its_values(app: QApplication, schema) -> None:
    listed = next(item for item in schema.fields if item.lov)
    builder = QueryBuilder()
    builder.set_schema(schema)
    builder.add_condition({"name": listed.name, "operator": "eq", "value": "", "and": True})
    cell = builder.table.cellWidget(0, 3)
    assert isinstance(cell, FilterValueEdit)
    assert cell._listed
    offered = [cell.combo.itemText(index) for index in range(cell.combo.count())]
    assert set(listed.lov).issubset(offered)


def test_choosing_a_value_reaches_the_encoded_filter(app: QApplication, schema) -> None:
    listed = next(item for item in schema.fields if item.lov)
    builder = QueryBuilder()
    builder.set_schema(schema)
    builder.add_condition({"name": listed.name, "operator": "eq", "value": "", "and": True})
    builder.table.cellWidget(0, 3).combo.setCurrentText(listed.lov[0])
    assert builder.filter_string() == f"{listed.name}__eq:={listed.lov[0]}"


def test_an_undocumented_filter_field_stays_a_text_box(app: QApplication, schema) -> None:
    plain = next(item for item in schema.fields if not item.lov)
    builder = QueryBuilder()
    builder.set_schema(schema)
    builder.add_condition({"name": plain.name, "operator": "eq", "value": "x", "and": True})
    cell = builder.table.cellWidget(0, 3)
    assert not cell._listed
    assert cell.text() == "x"


def test_a_range_operator_falls_back_to_free_text(app: QApplication, schema) -> None:
    """A comma-separated range cannot be expressed by picking one item."""
    listed = next(item for item in schema.fields if item.lov)
    builder = QueryBuilder()
    builder.set_schema(schema)
    builder.add_condition({"name": listed.name, "operator": "bt", "value": "1,2", "and": True})
    cell = builder.table.cellWidget(0, 3)
    assert not cell._listed
    assert cell.text() == "1,2"
    assert builder.filter_string() == f"{listed.name}__bt:=1,2"


def test_the_typed_value_survives_switching_between_the_two_modes(
    app: QApplication,
) -> None:
    cell = FilterValueEdit()
    cell.setText("kept")
    cell.set_choices(["a", "b"])
    assert cell.text() == "kept", "switching to a dropdown must not lose the value"
    cell.set_choices([])
    assert cell.text() == "kept", "switching back must not lose it either"


def test_the_mode_does_not_depend_on_the_widget_being_shown(app: QApplication) -> None:
    """A builder on a hidden tab must still report the value the user chose."""
    cell = FilterValueEdit()
    cell.set_choices(["a", "b"])
    cell.combo.setCurrentText("b")
    assert not cell.isVisible()
    assert cell.text() == "b"


def test_a_filter_round_trips_through_its_encoded_string(
    app: QApplication, schema
) -> None:
    listed = next(item for item in schema.fields if item.lov)
    builder = QueryBuilder()
    builder.set_schema(schema)
    builder.add_condition({"name": listed.name, "operator": "eq", "value": "", "and": True})
    builder.table.cellWidget(0, 3).combo.setCurrentText(listed.lov[0])
    encoded = builder.filter_string()

    restored = QueryBuilder()
    restored.set_schema(schema)
    restored.set_filter_string(encoded)
    assert restored.filter_string() == encoded
    assert restored.table.cellWidget(0, 3)._listed


def test_seeding_a_listed_condition_lands_in_the_dropdown(
    app: QApplication, schema
) -> None:
    listed = next(item for item in schema.fields if item.lov)
    builder = QueryBuilder()
    builder.set_schema(schema)
    builder.add_condition({"name": listed.name, "operator": "eq", "value": "", "and": True})
    builder.seed_all()
    assert builder.table.cellWidget(0, 3).text() in listed.lov
