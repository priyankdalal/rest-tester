from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from api_tester.catalog import load_catalog

from api_tester.catalog import load_catalog

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "api_catalog.json"


def generator_module():
    try:
        import tools.generate_catalog as generator
    except Exception as exc:
        pytest.skip(f"tools.generate_catalog is not importable yet: {exc}")
    return generator


def is_response_schema_enrichment(problem: str) -> bool:
    return "response_schema" in problem and "missing from the committed catalog" in problem


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def test_catalog_matches_microservice_controllers() -> None:
    generator = generator_module()
    committed = catalog()
    generated = generator.build_catalog(ROOT.parent)
    problems = [
        problem
        for problem in generator.catalog_differences(committed, generated)
        if not is_response_schema_enrichment(problem)
    ]
    assert problems == [], (
        "Controller APIs changed. Run `python -m tools.generate_catalog` and review "
        "the generated endpoint test cases.\n" + "\n".join(problems[:20])
    )


def sample_catalog() -> dict:
    return {
        "schema_version": 2,
        "services": [
            {
                "name": "Demo",
                "endpoints": [
                    {
                        "id": "a1",
                        "method": "GET",
                        "path": "/Brand",
                        "parameters": [{"name": "Filter", "source": "query"}],
                        "response_schema": "BrandResponse",
                    }
                ],
            }
        ],
        "filter_schemas": {"Brand": {"fields": [{"name": "Status", "lov": ["A"]}]}},
        "response_schemas": {"BrandResponse": {"fields": [{"name": "Name", "kind": "text"}]}},
    }


def test_drift_ignores_enrichment_the_extractor_cannot_produce() -> None:
    generator = generator_module()
    generated = sample_catalog()
    committed = sample_catalog()
    committed["services"][0]["endpoints"][0]["parameters"][0]["values"] = ["a", "b"]
    committed["services"][0]["endpoints"][0]["filter_entity"] = None
    committed["filter_schemas"]["Brand"]["fields"][0]["enum"] = "Status"
    assert generator.catalog_differences(committed, generated) == []


def test_an_enum_reference_releases_the_extracted_value_list() -> None:
    generator = generator_module()
    generated = sample_catalog()
    committed = sample_catalog()
    field = committed["filter_schemas"]["Brand"]["fields"][0]
    field["enum"] = "Status"
    field["lov"] = ["Draft", "Active"]
    assert generator.catalog_differences(committed, generated) == []


def test_a_rebound_list_is_still_checked_without_an_enum_reference() -> None:
    generator = generator_module()
    generated = sample_catalog()
    committed = sample_catalog()
    committed["filter_schemas"]["Brand"]["fields"][0]["lov"] = ["Draft"]
    assert generator.catalog_differences(committed, generated) != []


def test_drift_still_catches_a_changed_route_and_a_new_endpoint() -> None:
    generator = generator_module()
    generated = sample_catalog()
    committed = sample_catalog()
    committed["services"][0]["endpoints"][0]["path"] = "/Brands"
    assert any(
        "path" in problem
        for problem in generator.catalog_differences(committed, generated)
    )

    committed = sample_catalog()
    generated["services"][0]["endpoints"].append({"id": "b2", "method": "POST"})
    assert generator.catalog_differences(committed, generated) != []


def test_drift_catches_a_parameter_the_committed_catalog_lacks() -> None:
    generator = generator_module()
    generated = sample_catalog()
    committed = sample_catalog()
    del committed["services"][0]["endpoints"][0]["parameters"][0]["source"]
    problems = generator.catalog_differences(committed, generated)
    assert any("source" in problem for problem in problems)


def endpoint_cases() -> list[dict]:
    if not CATALOG_PATH.exists():
        return []
    return [
        endpoint
        for service in catalog()["services"]
        for endpoint in service["endpoints"]
    ]


@pytest.mark.parametrize("endpoint", endpoint_cases(), ids=lambda endpoint: endpoint["id"])
def test_endpoint_has_executable_test_definition(endpoint: dict) -> None:
    assert endpoint["method"] in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}
    assert endpoint["path"].startswith("/")
    assert endpoint["controller"]
    assert endpoint["action"]
    parameter_keys = [
        (parameter["source"], parameter["name"]) for parameter in endpoint["parameters"]
    ]
    assert len(parameter_keys) == len(set(parameter_keys))
    route_names = {
        item.split(":", 1)[0].lower()
        for item in re.findall(r"\{([^}]+)\}", endpoint["path"])
    }
    path_parameters = {
        parameter["name"].lower()
        for parameter in endpoint["parameters"]
        if parameter["source"] == "path"
    }
    assert route_names == path_parameters
    if endpoint["payload_type"] and endpoint["payload_type"].startswith("JsonPatchDocument<"):
        assert isinstance(endpoint["payload"], list)


def test_catalog_declares_schema_version_two() -> None:
    document = catalog()
    assert document["schema_version"] == 2
    for key in ("filter_schemas", "payload_schemas", "enums", "filter_grammar"):
        assert key in document, f"schema_version 2 catalog must expose {key}"


def test_schema_references_resolve() -> None:
    document = catalog()
    filter_schemas = document["filter_schemas"]
    payload_schemas = document["payload_schemas"]
    response_schemas = document.get("response_schemas", {})
    for service in document["services"]:
        for endpoint in service["endpoints"]:
            entity = endpoint.get("filter_entity")
            if entity:
                assert entity in filter_schemas, (
                    f"{endpoint['id']} references missing filter schema {entity}"
                )
            payload_schema = endpoint.get("payload_schema")
            if payload_schema:
                assert payload_schema in payload_schemas, (
                    f"{endpoint['id']} references missing payload schema {payload_schema}"
                )
            response_schema = endpoint.get("response_schema")
            if response_schema:
                assert response_schema in response_schemas, (
                    f"{endpoint['id']} references missing response schema {response_schema}"
                )


def test_every_filterable_endpoint_resolves_an_entity() -> None:
    document = catalog()
    unresolved = [
        endpoint["id"]
        for service in document["services"]
        for endpoint in service["endpoints"]
        if any(parameter["name"] == "Filter" for parameter in endpoint["parameters"])
        and not endpoint.get("filter_entity")
    ]
    assert unresolved == [], f"Endpoints expose Filter without a resolved entity: {unresolved}"


def test_filter_fields_use_supported_operators() -> None:
    try:
        from tools.schema_extractor import SUPPORTED_OPERATIONS
    except Exception as exc:
        pytest.skip(f"tools.schema_extractor is not importable yet: {exc}")

    for name, schema in catalog()["filter_schemas"].items():
        assert schema["fields"], f"{name} resolved to a schema with no filterable fields"
        for field in schema["fields"]:
            allowed = {
                operation["value"] for operation in SUPPORTED_OPERATIONS[field["data_type"]]
            }
            operators = {operation["value"] for operation in field["operators"]}
            assert operators, f"{name}.{field['name']} has no operators"
            assert operators <= allowed, (
                f"{name}.{field['name']} ({field['data_type']}) declares operators "
                f"outside the platform contract: {sorted(operators - allowed)}"
            )


def test_lov_fields_carry_enum_values() -> None:
    for name, schema in catalog()["filter_schemas"].items():
        for field in schema["fields"]:
            if field["data_type"] != "LOV":
                continue
            assert field.get("lov"), (
                f"{name}.{field['name']} is an LOV without selectable values"
            )


def test_load_catalog_reads_response_schema_references_and_store(tmp_path: Path) -> None:
    target = tmp_path / "catalog.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "services": [
                    {
                        "name": "Demo",
                        "repository": "DemoRepo",
                        "endpoints": [
                            {
                                "id": "demo-response",
                                "service": "Demo",
                                "controller": "Brands",
                                "action": "GetBrand",
                                "method": "GET",
                                "path": "/Brands/{brandId}",
                                "parameters": [],
                                "response_schema": "BrandResponse",
                            }
                        ],
                    }
                ],
                "filter_schemas": {},
                "payload_schemas": {},
                "form_schemas": {},
                "response_schemas": {
                    "BrandResponse": {
                        "name": "BrandResponse",
                        "fields": [
                            {
                                "name": "Status",
                                "kind": "enum",
                                "clr_type": "BrandStatus",
                                "enum": "BrandStatus",
                            }
                        ],
                    }
                },
                "enums": {"BrandStatus": ["Draft", "Active"]},
            }
        ),
        encoding="utf-8",
    )

    loaded = load_catalog(target)

    assert loaded.services[0].endpoints[0].response_schema == "BrandResponse"
    assert loaded.response_schema(None) is None
    assert loaded.response_schema("Missing") is None
    assert loaded.response_schema("BrandResponse")["fields"][0]["lov"] == [
        "Draft",
        "Active",
    ]
