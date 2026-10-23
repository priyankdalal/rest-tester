import json
import threading
from copy import deepcopy
from dataclasses import replace

import pytest

from api_tester.catalog import Endpoint, load_catalog
from api_tester.catalog_builder import CatalogDocument
from api_tester.client import ApiResult
from api_tester.scanners.openapi import load_document, scan
from api_tester.security import (
    SecurityReport, catalog_document, fingerprint, report_csv, report_html,
    sanitize_contract, scan_catalog, scan_response,
)
from api_tester.workspace_store import WorkspaceStore
from tools.generate_catalog import preserve_security_metadata


@pytest.fixture
def contract():
    return {
        "openapi": "3.1.0",
        "info": {"title": "Demo", "version": "1"},
        "security": [{"bearer": []}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "paths": {"/items": {"get": {"responses": {
            "200": {"description": "OK", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"id": {"type": "integer"}},
            }}}},
            "401": {"description": "Unauthenticated"},
            "403": {"description": "Forbidden"},
        }}}},
    }


@pytest.fixture
def endpoint():
    return Endpoint("demo", "Demo", "Items", "List", "GET", "/items")


def document(contract):
    return {"services": [{
        "name": "Demo", "repository": "", "security_metadata": {"document": contract},
        "endpoints": [{"id": "demo", "service": "Demo", "method": "GET", "path": "/items"}],
    }]}


def rules(report):
    return {item.rule_id for item in report.findings}


def response(**overrides):
    values = {
        "passed": True, "status_code": 200, "elapsed_ms": 1,
        "url": "https://example.test/items", "response_headers": {"Content-Type": "application/json"},
        "response_body": "{}", "content": b"{}", "content_type": "application/json",
    }
    return ApiResult(**{**values, **overrides})


def test_valid_contract_reports_coverage_not_security_certification(contract):
    report = scan_catalog(document(contract))
    assert report.state == "complete"
    assert report.endpoints_reviewed == 1
    assert not report.findings
    assert len(report.coverage) == 10
    assert all(item.status != "passed" for item in report.coverage)
    assert "certification" in report_html(report)
    assert SecurityReport.from_dict(report.to_dict()) == report


def test_old_catalog_is_unknown_not_unprotected():
    report = scan_catalog({"services": [{"name": "Demo", "endpoints": [{"id": "one"}]}]})
    assert not report.findings
    assert report.endpoints_reviewed == 1
    assert "unavailable" in report.diagnostics[0]
    assert all(item.status == "not assessed" for item in report.coverage if not item.category.startswith("API9"))


@pytest.mark.parametrize("requirements", [[], [{}], [{"bearer": []}, {}]])
def test_anonymous_alternatives_are_not_implicitly_vulnerabilities(contract, requirements):
    contract["paths"]["/items"]["get"]["security"] = requirements
    assert "AUTH001" not in rules(scan_catalog(document(contract)))
    assert "AUTH003" not in rules(scan_catalog(document(contract)))
    doc = document(contract)
    doc["services"][0]["endpoints"][0]["security_metadata"] = {"require_authentication": True}
    assert "AUTH003" in rules(scan_catalog(doc))


@pytest.mark.parametrize("requirements", [
    [{"bearer": [], "key": []}], [{"bearer": []}, {"key": []}],
])
def test_and_or_security_and_inheritance(contract, requirements):
    contract["security"] = requirements
    contract["components"]["securitySchemes"]["key"] = {"type": "apiKey", "in": "header", "name": "X-Key"}
    assert not scan_catalog(document(contract)).findings


def test_missing_scheme_and_missing_auth_error_docs(contract):
    contract["security"] = [{"missing": []}]
    del contract["paths"]["/items"]["get"]["responses"]["403"]
    assert {"AUTH004", "AUTH008"} <= rules(scan_catalog(document(contract)))


def test_missing_security_is_a_contract_gap(contract):
    del contract["security"]
    report = scan_catalog(document(contract))
    finding = next(item for item in report.findings if item.rule_id == "AUTH001")
    assert finding.evidence_class == "contract gap"
    assert "enforcement" in finding.limitation


@pytest.mark.parametrize("requirements", [False, {}, [None], "bearer"])
def test_malformed_security_is_explicit(contract, requirements):
    contract["security"] = requirements
    assert "AUTH002" in rules(scan_catalog(document(contract)))


def test_oauth_scope_and_flow_validation(contract):
    contract["security"] = [{"oauth": ["write"]}]
    scheme = {
        "type": "oauth2", "flows": {"clientCredentials": {
            "tokenUrl": "https://example.test/token", "scopes": {"read": "Read"},
        }},
    }
    contract["components"]["securitySchemes"]["oauth"] = scheme
    assert "AUTH006" in rules(scan_catalog(document(contract)))
    scheme["flows"]["clientCredentials"]["scopes"]["write"] = "Write"
    assert "AUTH006" not in rules(scan_catalog(document(contract)))
    del scheme["flows"]["clientCredentials"]["tokenUrl"]
    assert "AUTH007" in rules(scan_catalog(document(contract)))


def test_openid_scopes_are_allowed_without_discovery(contract):
    contract["components"]["securitySchemes"]["oidc"] = {
        "type": "openIdConnect", "openIdConnectUrl": "https://example.test/.well-known/openid-configuration",
    }
    contract["security"] = [{"oidc": ["openid", "profile"]}]
    assert not scan_catalog(document(contract)).findings


def test_transport_query_and_input_constraints(contract):
    contract["servers"] = [{"url": "http://example.test"}, {"url": "http://localhost:5000"}]
    contract["components"]["securitySchemes"]["bearer"] = {
        "type": "apiKey", "in": "query", "name": "api_key",
    }
    contract["paths"]["/items"]["get"]["parameters"] = [
        {"name": "token", "in": "query", "schema": {"type": "string"}},
        {"name": "pageSize", "in": "query", "schema": {"type": "integer", "maximum": 100}},
        {"name": "callback", "in": "query", "schema": {"type": "string", "format": "uri"}},
    ]
    report = scan_catalog(document(contract))
    assert {"TRANSPORT001", "AUTH005", "INPUT001", "INPUT002", "INPUT003"} <= rules(report)
    assert len([item for item in report.findings if item.rule_id == "TRANSPORT001"]) == 1
    assert "pageSize/maximum" not in next(item.evidence for item in report.findings if item.rule_id == "INPUT001")


def test_shared_parameter_override_is_respected(contract):
    contract["paths"]["/items"]["parameters"] = [
        {"name": "size", "in": "query", "schema": {"type": "integer"}},
    ]
    contract["paths"]["/items"]["get"]["parameters"] = [
        {"name": "size", "in": "query", "schema": {"type": "integer", "maximum": 50}},
    ]
    assert "INPUT001" not in rules(scan_catalog(document(contract)))


def test_local_references_unescape_and_external_refs_never_fetch(contract, monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: pytest.fail("Network request during static scan"))
    contract["components"]["parameters"] = {"size/limit": {
        "name": "size", "in": "query", "schema": {"type": "integer", "maximum": 50},
    }}
    operation = contract["paths"]["/items"]["get"]
    operation["parameters"] = [{"$ref": "#/components/parameters/size~1limit"}]
    assert scan_catalog(document(contract)).state == "complete"
    operation["parameters"] = [{"$ref": "https://example.test/parameter.json"}]
    report = scan_catalog(document(contract))
    assert report.state == "partial"
    assert any("External" in item for item in report.diagnostics)


def test_missing_ref_and_reference_cycle_are_partial(contract):
    contract["components"]["schemas"] = {
        "Broken": {"$ref": "#/components/schemas/Missing"},
        "Cycle": {"$ref": "#/components/schemas/Cycle"},
    }
    report = scan_catalog(document(contract))
    assert report.state == "partial"
    assert any("Unresolved" in item for item in report.diagnostics)
    assert any("cycle" in item for item in report.diagnostics)


def test_composition_and_file_policies(contract):
    contract["paths"]["/items"]["get"]["requestBody"] = {"content": {
        "multipart/form-data": {"schema": {
            "allOf": [{"type": "object", "properties": {
                "File": {"type": "string", "format": "binary"},
                "Label": {"type": "string", "maxLength": 30},
            }}],
        }},
    }}
    assert "INPUT004" in rules(scan_catalog(document(contract)))
    contract["paths"]["/items"]["get"]["requestBody"]["content"]["multipart/form-data"]["schema"]["allOf"][0]["properties"]["File"]["x-max-file-size"] = 1024
    assert "INPUT004" not in rules(scan_catalog(document(contract)))


def test_scope_cancellation_empty_and_limits(contract, monkeypatch):
    assert scan_catalog(document(contract), endpoint_ids={"absent"}).endpoints_reviewed == 0
    event = threading.Event()
    event.set()
    assert scan_catalog(document(contract), event).state == "cancelled"
    assert scan_catalog({"services": []}).diagnostics
    monkeypatch.setattr("api_tester.security.MAX_NODES", 4)
    assert scan_catalog(document(contract)).state == "partial"


@pytest.mark.parametrize("version", ["2.0", "3.2.0", "invalid"])
def test_unsupported_version_is_unknown(contract, version):
    contract["openapi"] = version
    report = scan_catalog(document(contract))
    assert not report.findings
    assert any("unsupported" in item for item in report.diagnostics)


def test_passive_review_records_tls_credentials_and_stack_without_body_secrets(contract, endpoint, monkeypatch):
    monkeypatch.setattr("requests.request", lambda *a, **k: pytest.fail("Passive review sent a request"))
    result = response(
        url="http://example.test/items?token=super-secret",
        response_body="Traceback (most recent call last):\npassword: private-body-value",
        security_context={"credential_present": True, "verify_ssl": False},
    )
    report = scan_response(document(contract), endpoint, result, environment="QA")
    assert {"HTTP002", "HTTP003"} <= rules(report)
    assert "HTTP001" not in rules(report)
    serialized = json.dumps(report.to_dict())
    assert "super-secret" not in serialized and "private-body-value" not in serialized
    assert report.environment == "QA"
    assert "HTTP001" in rules(scan_response(document(contract), endpoint, replace(
        result, url="https://example.test/items",
    )))


def test_passive_sensitive_fields_cors_and_cache(contract, endpoint):
    endpoint = replace(endpoint, security_metadata={"sensitive_response_fields": ["privateData"]})
    result = response(
        response_body='{"privateData":"literal-private-value"}',
        response_headers={
            "content-type": "application/json",
            "access-control-allow-origin": "*", "access-control-allow-credentials": "true",
        },
    )
    report = scan_response(document(contract), endpoint, result)
    assert {"HTTP005", "HTTP006", "HTTP007"} <= rules(report)
    assert "literal-private-value" not in json.dumps(report.to_dict())
    result = replace(result, response_headers={**result.response_headers, "cache-control": "private, no-store"})
    assert "HTTP007" not in rules(scan_response(document(contract), endpoint, result))


def test_passive_truncation_cancellation_and_related_contract(contract, endpoint):
    del contract["security"]
    report = scan_response(document(contract), endpoint, response(response_body="x" * (256 * 1024 + 1)))
    assert report.state == "partial"
    assert "AUTH001" in rules(report)
    event = threading.Event()
    event.set()
    assert scan_response(document(contract), endpoint, response(), event).state == "cancelled"


def test_import_round_trip_merge_and_regeneration_preserve_metadata(contract, tmp_path):
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    found = scan(path, "Demo")
    draft = CatalogDocument()
    draft.merge_scan("Demo", found)
    draft.services[0].endpoints[0].security_metadata = {"sensitive_response_fields": ["privateData"]}
    catalog_path = tmp_path / "catalog.json"
    draft.save(catalog_path)
    catalog = load_catalog(catalog_path)
    assert catalog.services[0].security_metadata["document"] == contract
    assert catalog.services[0].endpoints[0].security_metadata["sensitive_response_fields"] == ["privateData"]
    draft.merge_scan("Demo", found)
    assert draft.services[0].endpoints[0].security_metadata
    new = deepcopy(draft.to_dict())
    new["services"][0].pop("security_metadata")
    new["services"][0]["endpoints"][0].pop("security_metadata")
    preserve_security_metadata(new, draft.to_dict())
    assert new["services"][0]["security_metadata"]
    assert new["services"][0]["endpoints"][0]["security_metadata"]
    assert scan_catalog(catalog_document(catalog)).endpoints_reviewed == 1


def test_contract_sanitization_keeps_structure_not_literal_credentials(contract):
    contract["components"]["schemas"] = {"Login": {
        "type": "object", "properties": {
            "password": {"type": "string", "default": "my-private-password"},
        }, "example": {"password": "my-private-password"},
    }}
    contract["components"]["securitySchemes"]["token"] = {"type": "http", "scheme": "bearer"}
    sanitized = sanitize_contract(contract)
    assert "my-private-password" not in json.dumps(sanitized)
    assert sanitized["components"]["securitySchemes"]["token"]["scheme"] == "bearer"
    assert sanitized["components"]["schemas"]["Login"]["properties"]["password"]["type"] == "string"


def test_import_size_limit(tmp_path, monkeypatch):
    path = tmp_path / "openapi.json"
    path.write_text('{"openapi":"3.1.0"}', encoding="utf-8")
    monkeypatch.setattr("api_tester.security.MAX_DOCUMENT_BYTES", 2)
    with pytest.raises(ValueError, match="limit"):
        load_document(path)


def test_report_exports_escape_html_and_spreadsheet_formula(contract):
    del contract["security"]
    report = scan_catalog(document(contract))
    finding = report.findings[0]
    finding.evidence = '<script>alert("bad")</script>'
    finding.path = '=HYPERLINK("bad")'
    assert "<script>" not in report_html(report)
    assert "&lt;script&gt;" in report_html(report)
    assert "'=HYPERLINK" in report_csv(report)


def test_report_store_is_durable_and_retained(contract, tmp_path):
    report = scan_catalog(document(contract))
    path = tmp_path / "workspace.db"
    store = WorkspaceStore(path)
    store.save_security_report(report.to_dict())
    store.close()
    store = WorkspaceStore(path)
    assert store.security_report(report.id) == report.to_dict()
    assert store.security_reports()[0] == report.to_dict()
    old = replace(report, id="old", created_at="2000-01-01T00:00:00+00:00")
    store.save_security_report(old.to_dict())
    assert store.apply_retention() == 1
    assert store.security_report("old") is None
    store.close()


def test_fingerprint_ignores_generated_timestamp_but_tracks_security(contract):
    before = document(contract)
    after = deepcopy(before)
    after["generated_at"] = "later"
    assert fingerprint(before) == fingerprint(after)
    after["services"][0]["security_metadata"]["document"]["security"] = []
    assert fingerprint(before) != fingerprint(after)


def test_inventory_deprecation_and_operation_server_rules(contract):
    operation = contract["paths"]["/items"]["get"]
    operation["deprecated"] = True
    operation["servers"] = [{"url": "http://example.test"}]
    doc = document(contract)
    doc["services"][0]["endpoints"].append(dict(doc["services"][0]["endpoints"][0], id="duplicate"))
    assert {"INVENTORY001", "INVENTORY002", "TRANSPORT001"} <= rules(scan_catalog(doc))


def test_passive_content_type_comparison_respects_wildcards(contract, endpoint):
    result = response(content_type="text/html; charset=utf-8")
    assert "HTTP008" in rules(scan_response(document(contract), endpoint, result))
    media = contract["paths"]["/items"]["get"]["responses"]["200"]["content"]
    media["text/*"] = media.pop("application/json")
    assert "HTTP008" not in rules(scan_response(document(contract), endpoint, result))


def test_credential_enums_sanitized_without_changing_security_requirement_structure(contract, tmp_path):
    contract["components"]["schemas"] = {"Login": {"type": "object", "properties": {
        "password": {"type": "string", "enum": ["private-password"], "default": "private-password"},
    }}}
    contract["security"] = [{"token": []}]
    contract["components"]["securitySchemes"]["token"] = {"type": "http", "scheme": "bearer"}
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    found = scan(path, "Demo")
    assert "private-password" not in json.dumps(found.security_metadata)
    doc = document(found.security_metadata["document"])
    assert "SECRET001" in rules(scan_catalog(doc))
    assert "AUTH007" not in rules(scan_catalog(doc))


def test_mutual_tls_is_version_aware(contract):
    contract["components"]["securitySchemes"]["bearer"] = {"type": "mutualTLS"}
    assert "AUTH007" not in rules(scan_catalog(document(contract)))
    contract["openapi"] = "3.0.3"
    assert "AUTH007" in rules(scan_catalog(document(contract)))


def test_import_local_path_item_ref(contract, tmp_path):
    contract["components"]["pathItems"] = {"items": deepcopy(contract["paths"]["/items"])}
    contract["paths"]["/items"] = {"$ref": "#/components/pathItems/items"}
    path = tmp_path / "openapi.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    assert scan(path, "Demo").count == 1


def test_large_catalog_is_bounded_and_fast():
    from time import perf_counter

    doc = {"services": [{
        "name": "Demo", "endpoints": [
            {"id": str(index), "method": "GET", "path": f"/items/{index}"} for index in range(1000)
        ],
    }]}
    start = perf_counter()
    report = scan_catalog(doc)
    elapsed = perf_counter() - start
    assert report.state == "complete" and report.endpoints_reviewed == 1000
    assert elapsed < 2, f"1,000-endpoint review took {elapsed:.2f}s"


def test_legacy_payload_stores_do_not_consume_contract_traversal_budget(monkeypatch):
    doc = {"services": [{"name": "Demo", "endpoints": [{"id": "one", "method": "GET", "path": "/items"}]}]}
    doc["payload_schemas"] = {"Large": {"unused": ["value"] * 1000}}
    monkeypatch.setattr("api_tester.security.MAX_NODES", 50)
    report = scan_catalog(doc)
    assert report.state == "complete" and report.endpoints_reviewed == 1
