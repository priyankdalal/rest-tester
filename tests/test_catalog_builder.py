"""CatalogDocument editing, validation, round-tripping, and id preservation."""

import json
import sys
import types
from pathlib import Path

from api_tester.catalog_builder import (
    CatalogDocument,
    EndpointDraft,
    ServiceDraft,
    default_sample,
    new_response_schema,
    merge_documents,
)
from api_tester.catalog import load_catalog


BUNDLED = Path(__file__).resolve().parents[1] / "data" / "api_catalog.json"


def sample_document() -> CatalogDocument:
    document = CatalogDocument()
    service = ServiceDraft(name="Demo", default_base_url="https://localhost:7001")
    service.endpoints.append(
        EndpointDraft(
            service="Demo",
            controller="Brands",
            action="GetBrand",
            method="GET",
            path="/Brands/{brandId}",
            parameters=[
                {"name": "brandId", "source": "path", "type": "integer", "required": True}
            ],
        )
    )
    document.services.append(service)
    return document


def test_round_trip_of_the_bundled_catalog_is_semantically_lossless() -> None:
    document = CatalogDocument.load(BUNDLED)
    original = json.loads(BUNDLED.read_text(encoding="utf-8"))
    produced = document.to_dict()

    assert [service["name"] for service in produced["services"]] == [
        service["name"] for service in original["services"]
    ]
    for before, after in zip(original["services"], produced["services"]):
        assert [item["id"] for item in before["endpoints"]] == [
            item["id"] for item in after["endpoints"]
        ]
        for old, new in zip(before["endpoints"], after["endpoints"]):
            for key, value in old.items():
                assert new[key] == value, f"{old['id']}.{key}"


def test_loading_preserves_endpoint_ids_so_saved_requests_are_not_orphaned() -> None:
    document = CatalogDocument.load(BUNDLED)
    loaded_ids = {
        endpoint.id for service in document.services for endpoint in service.endpoints
    }
    catalog_ids = {
        endpoint.id
        for service in load_catalog(BUNDLED).services
        for endpoint in service.endpoints
    }
    assert loaded_ids == catalog_ids


def test_renaming_an_endpoint_keeps_a_loaded_identity_until_refreshed() -> None:
    document = sample_document()
    endpoint = document.services[0].endpoints[0]
    endpoint.identity = endpoint.computed_id()
    before = endpoint.id
    endpoint.action = "GetBrandById"
    assert endpoint.id == before
    endpoint.refresh_identity()
    assert endpoint.id != before


def test_validation_accepts_aspnet_route_constraints() -> None:
    document = sample_document()
    endpoint = document.services[0].endpoints[0]
    endpoint.path = "/Brands/{brandId:int}"
    assert document.validate() == []


def test_validation_reports_a_placeholder_without_a_path_parameter() -> None:
    document = sample_document()
    document.services[0].endpoints[0].parameters = []
    problems = document.validate()
    assert any("brandId" in problem for problem in problems)


def test_validation_reports_duplicate_service_names() -> None:
    document = sample_document()
    document.services.append(ServiceDraft(name="Demo"))
    problems = document.validate()
    assert any("Duplicate service name" in problem for problem in problems)


def test_save_and_reload_keeps_the_document_equal(tmp_path: Path) -> None:
    document = sample_document()
    target = tmp_path / "catalog.json"
    document.save(target)
    assert CatalogDocument.load(target).to_dict() == document.to_dict()
    assert load_catalog(target).services[0].name == "Demo"


def test_merge_documents_keeps_both_services() -> None:
    first = sample_document()
    second = CatalogDocument()
    second.services.append(ServiceDraft(name="Other"))
    first.response_schemas["BrandResponse"] = new_response_schema("BrandResponse")
    second.response_schemas["BrandResponse"] = new_response_schema("OtherBrandResponse")
    second.response_schemas["OtherResponse"] = new_response_schema("OtherResponse")
    merged = merge_documents([first, second])
    assert [service.name for service in merged.services] == ["Demo", "Other"]
    assert "BrandResponse" in merged.response_schemas
    assert "OtherResponse" in merged.response_schemas
    assert merged.response_schemas["BrandResponse"]["name"] == "BrandResponse"


def test_autodetect_response_schema_merges_the_detected_schema(
    tmp_path: Path, monkeypatch
) -> None:
    document = CatalogDocument()
    service = ServiceDraft(name="Demo", repository=str(tmp_path))
    endpoint = EndpointDraft(
        service="Demo",
        controller="Brands",
        action="GetBrand",
        method="GET",
        path="/Brands/{brandId}",
        source_file="Controllers\\BrandsController.cs",
        source_line=42,
    )
    service.endpoints.append(endpoint)
    document.services.append(service)

    fake_generator = types.ModuleType("tools.generate_catalog")
    fake_generator.scan_dotnet_project = lambda service_name, repository, shared_root=None: {
        "endpoints": [
            {
                "id": endpoint.id,
                "controller": endpoint.controller,
                "action": endpoint.action,
                "method": endpoint.method,
                "path": endpoint.path,
                "source_file": endpoint.source_file,
                "source_line": endpoint.source_line,
                "response_schema": "BrandResponse",
            }
        ],
        "response_schemas": {
            "BrandResponse": new_response_schema("BrandResponse")
        },
    }
    monkeypatch.setitem(sys.modules, "tools.generate_catalog", fake_generator)

    detected = document.autodetect_response_schema(endpoint.id)

    assert detected == "BrandResponse"
    assert endpoint.response_schema == "BrandResponse"
    assert document.response_schemas["BrandResponse"]["name"] == "BrandResponse"


def test_default_sample_matches_the_declared_type() -> None:
    assert default_sample("boolean", "isActive") in {"true", "false", True, False}
    assert default_sample("integer", "pageSize") is not None
    assert default_sample("string", "name") is not None
