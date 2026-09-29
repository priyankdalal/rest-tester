"""Verifies ``multipart/form-data`` endpoints render and round-trip correctly.

The form DTOs (``FieldBookImportRequest``, ``ImagePayload``, ...) are bound by
ASP.NET property-by-property, so the catalog must flatten them into real form
fields rather than sending the DTO parameter name.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_tester.catalog import load_catalog
from api_tester.suite import TestCase, TestSuite, load_suite, save_suite

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "api_catalog.json"


@pytest.fixture(scope="module")
def catalog():
    return load_catalog(CATALOG_PATH)


@pytest.fixture(scope="module")
def form_endpoints(catalog):
    return [
        endpoint
        for service in catalog.services
        for endpoint in service.endpoints
        if endpoint.form_schema
    ]


class TestFormCatalog:
    def test_every_form_endpoint_has_a_schema(self, catalog):
        missing = [
            f"{e.method} {e.path}"
            for service in catalog.services
            for e in service.endpoints
            if any(p.source == "form" for p in e.parameters) and not e.form_schema
        ]
        assert missing == []

    def test_schema_is_resolvable(self, catalog, form_endpoints):
        assert form_endpoints
        for endpoint in form_endpoints:
            assert catalog.form_schema(endpoint.form_schema) is not None, endpoint.path

    def test_complex_dto_is_flattened_into_real_field_names(self, catalog):
        endpoint = next(
            e
            for service in catalog.services
            for e in service.endpoints
            if e.path == "/FieldBook/import"
        )
        names = [p.name for p in endpoint.parameters if p.source == "form"]
        assert names == ["File", "IsTemplate", "MatchType", "ColumnMappings"]
        # The DTO parameter itself must be gone, or the upload binds to nothing.
        assert "request" not in names

    def test_file_property_keeps_its_clr_type(self, catalog):
        schema = catalog.form_schema("FieldBookImportRequest")
        file_field = next(f for f in schema["fields"] if f["name"] == "File")
        assert file_field["kind"] == "file"
        assert "IFormFile" in file_field["clr_type"]

    def test_bare_form_file_action_gets_a_schema(self, catalog):
        schema = catalog.form_schema("Product.ImportProductsFromExcel")
        assert schema is not None
        assert [f["kind"] for f in schema["fields"]] == ["file"]

    def test_required_modifier_is_detected(self, catalog):
        """``public required IFormFile File`` must mark the field required."""
        schema = catalog.form_schema("ImagePayload")
        file_field = next(f for f in schema["fields"] if f["name"] == "File")
        assert file_field["required"] is True

    def test_scalar_kinds_are_typed(self, catalog):
        schema = catalog.form_schema("ImagePayload")
        kinds = {f["name"]: f["kind"] for f in schema["fields"]}
        assert kinds == {"File": "file", "LocationId": "integer", "TrialId": "integer"}

    def test_no_nested_object_fields_survive(self, catalog):
        """Multipart bodies are flat; nested objects must be dotted names."""
        for schema in catalog.form_schemas.values():
            for field in schema["fields"]:
                assert field["kind"] != "object", field


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class TestFormBuilder:
    def _builder(self, catalog, name):
        from api_tester.builders import FormBuilder

        builder = FormBuilder()
        builder.set_schema(catalog.form_schema(name))
        return builder

    def test_editors_match_field_types(self, qt_app, catalog):
        builder = self._builder(catalog, "FieldBookImportRequest")
        kinds = {name: type(editor).__name__ for name, editor, _ in builder._editors}
        assert kinds == {
            "File": "FilePicker",
            "IsTemplate": "QCheckBox",
            "MatchType": "QLineEdit",
            "ColumnMappings": "QLineEdit",
        }

    def test_values_are_form_prefixed_strings(self, qt_app, catalog):
        builder = self._builder(catalog, "FieldBookImportRequest")
        builder._editors[0][1].setText(r"C:\tmp\a.xlsx")
        builder._editors[1][1].setChecked(True)
        builder._editors[2][1].setText("Code")
        values = builder.values()
        assert values["form:File"] == r"C:\tmp\a.xlsx"
        assert values["form:IsTemplate"] == "true"
        assert values["form:MatchType"] == "Code"

    def test_empty_fields_are_omitted(self, qt_app, catalog):
        builder = self._builder(catalog, "FieldBookImportRequest")
        assert "form:File" not in builder.values()
        assert "form:ColumnMappings" not in builder.values()

    def test_unchecked_boolean_is_still_sent(self, qt_app, catalog):
        """A false flag must be explicit, not absent."""
        builder = self._builder(catalog, "FieldBookImportRequest")
        assert builder.values()["form:IsTemplate"] == "false"

    def test_set_values_restores_a_saved_case(self, qt_app, catalog):
        builder = self._builder(catalog, "FieldBookImportRequest")
        builder.set_values(
            {"form:File": "x.xlsx", "form:IsTemplate": "true", "form:MatchType": "Name"}
        )
        assert builder.values() == {
            "form:File": "x.xlsx",
            "form:IsTemplate": "true",
            "form:MatchType": "Name",
        }

    def test_set_schema_with_values_prefills(self, qt_app, catalog):
        from api_tester.builders import FormBuilder

        builder = FormBuilder()
        builder.set_schema(
            catalog.form_schema("ImagePayload"), {"form:File": "p.png", "form:TrialId": "7"}
        )
        assert builder.values() == {"form:File": "p.png", "form:TrialId": "7"}

    def test_seed_fills_scalars_but_never_the_file(self, qt_app, catalog):
        builder = self._builder(catalog, "ImagePayload")
        builder.seed()
        values = builder.values()
        assert "form:File" not in values
        assert values["form:LocationId"]
        assert values["form:TrialId"]

    def test_seeded_integers_are_numeric(self, qt_app, catalog):
        builder = self._builder(catalog, "ImagePayload")
        builder.seed()
        int(builder.values()["form:LocationId"])

    def test_no_schema_disables_seeding(self, qt_app, catalog):
        from api_tester.builders import FormBuilder

        builder = FormBuilder()
        builder.set_schema(None)
        assert builder.values() == {}
        assert not builder.seed_button.isEnabled()

    def test_changed_signal_fires_on_edit(self, qt_app, catalog):
        builder = self._builder(catalog, "FieldBookImportRequest")
        seen = []
        builder.changed.connect(lambda: seen.append(1))
        builder._editors[2][1].setText("Code")
        assert seen

    def test_form_actions_are_icon_only_and_theme_aware(self, qt_app, catalog):
        from api_tester import theme
        from api_tester.viewers import FilePicker
        from PyQt6.QtWidgets import QPushButton

        builder = self._builder(catalog, "ImagePayload")
        seed_buttons = [
            button
            for button in builder.container.findChildren(QPushButton)
            if button.property("formFieldAction") == "seed"
        ]
        file_picker = next(
            editor
            for _name, editor, _field in builder._editors
            if isinstance(editor, FilePicker)
        )
        buttons = [builder.seed_button, *seed_buttons, file_picker.browse_button]
        policies = [
            (
                button.sizePolicy().horizontalPolicy(),
                button.sizePolicy().verticalPolicy(),
            )
            for button in buttons
        ]

        assert all(button.text() == "" for button in buttons)
        assert all(button.toolTip() for button in buttons)
        assert all(button.accessibleName() == button.toolTip() for button in buttons)
        assert all(button.width() == 56 for button in seed_buttons)
        theme.apply_theme(qt_app, "Light")
        builder.refresh_theme()
        light_keys = [button.icon().cacheKey() for button in buttons]
        theme.apply_theme(qt_app, "Dark")
        builder.refresh_theme()
        assert [button.icon().cacheKey() for button in buttons] != light_keys
        assert policies == [
            (
                button.sizePolicy().horizontalPolicy(),
                button.sizePolicy().verticalPolicy(),
            )
            for button in buttons
        ]
        theme.apply_theme(qt_app, "Light")


class TestFormPersistence:
    def test_case_values_round_trip_through_disk(self, tmp_path):
        suite = TestSuite(name="forms")
        suite.cases.append(
            TestCase(
                id="c1",
                name="import field book",
                endpoint_id="abc",
                values={
                    "form:File": r"C:\tmp\book.xlsx",
                    "form:IsTemplate": "true",
                    "form:MatchType": "Id",
                },
            )
        )
        path = tmp_path / "forms.json"
        save_suite(suite, path)
        reloaded = load_suite(path)
        assert reloaded.cases[0].values == suite.cases[0].values

    def test_form_values_survive_json_encoding(self, tmp_path):
        """Windows paths contain backslashes that must not be mangled."""
        suite = TestSuite(name="forms")
        suite.cases.append(
            TestCase(id="c1", name="c", endpoint_id="a", values={"form:File": r"C:\a\b\c.xlsx"})
        )
        path = tmp_path / "s.json"
        save_suite(suite, path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["cases"][0]["values"]["form:File"] == r"C:\a\b\c.xlsx"
