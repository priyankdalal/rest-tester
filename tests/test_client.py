from types import SimpleNamespace

import pytest

from api_tester.catalog import Endpoint, Parameter
from api_tester.client import (
    generate_curl,
    generate_python,
    prepare_endpoint_request,
    execute_endpoint,
    status_matches,
)


def test_status_range() -> None:
    assert status_matches(204, "200-299")
    assert not status_matches(404, "200-299")


def test_status_list() -> None:
    assert status_matches(201, "200, 201, 202")
    assert not status_matches(204, "200, 201, 202")


def _endpoint() -> Endpoint:
    return Endpoint(
        id="test.get",
        service="Test",
        controller="Test",
        action="Get",
        method="GET",
        path="/Test",
    )


def _capture_request(monkeypatch):
    calls = []

    def fake_request(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b"{}",
            url="https://localhost:5001/Test",
            request=SimpleNamespace(headers={}, body=None),
            reason="OK",
        )

    monkeypatch.setattr("api_tester.client.requests.request", fake_request)
    return calls


def test_execute_endpoint_verifies_ssl_by_default(monkeypatch) -> None:
    calls = _capture_request(monkeypatch)
    execute_endpoint(_endpoint(), "https://localhost:5001", "", "", {}, None, "200")
    assert calls[0]["verify"] is True


def test_execute_endpoint_allows_self_signed_local_certificates(monkeypatch) -> None:
    calls = _capture_request(monkeypatch)
    execute_endpoint(
        _endpoint(),
        "https://localhost:5001",
        "",
        "",
        {},
        None,
        "200",
        verify_ssl=False,
    )
    assert calls[0]["verify"] is False


def test_ssl_failure_explains_how_to_allow_a_local_certificate(monkeypatch) -> None:
    import requests

    def fail_request(*args, **kwargs):
        raise requests.exceptions.SSLError("self-signed certificate")

    monkeypatch.setattr("api_tester.client.requests.request", fail_request)
    with pytest.raises(RuntimeError, match="uncheck.*Verify SSL certificates"):
        execute_endpoint(_endpoint(), "https://localhost:5001", "", "", {}, None, "200")


def test_prepared_request_and_generated_artifacts_redact_secrets() -> None:
    prepared = prepare_endpoint_request(
        _endpoint(),
        "https://localhost:5001",
        "secret-token",
        "secret-api-key",
        {},
        None,
    )
    assert prepared.headers["Authorization"] == "Bearer secret-token"
    assert prepared.headers["x-api-key"] == "secret-api-key"
    assert prepared.resolved_url == "https://localhost:5001/Test"
    curl = generate_curl(prepared)
    python = generate_python(prepared)
    assert "secret-token" not in curl
    assert "secret-api-key" not in curl
    assert "secret-token" not in python
    assert "secret-api-key" not in python


def test_prepare_request_rejects_a_missing_required_parameter() -> None:
    endpoint = Endpoint(
        id="test.get-one",
        service="Test",
        controller="Test",
        action="Get",
        method="GET",
        path="/Test/{id}",
        parameters=(Parameter("id", "path", "int", True),),
    )
    with pytest.raises(ValueError, match="Required path parameter.*id"):
        prepare_endpoint_request(endpoint, "https://example.test", "", "", {}, None)


def test_prepare_request_resolves_path_and_query() -> None:
    endpoint = Endpoint(
        id="test.get-one",
        service="Test",
        controller="Test",
        action="Get",
        method="GET",
        path="/Test/{id}",
        parameters=(
            Parameter("id", "path", "string", True),
            Parameter("Filter", "query", "string", False),
        ),
    )
    prepared = prepare_endpoint_request(
        endpoint,
        "https://example.test/root",
        "",
        "",
        {"path:id": "a b", "query:Filter": "Name__eq:=Trial"},
        None,
    )
    assert prepared.resolved_url == (
        "https://example.test/root/Test/a%20b?Filter=Name__eq%3A%3DTrial"
    )


def test_prepare_request_omits_an_unchecked_query_parameter() -> None:
    endpoint = Endpoint(
        id="test.get-one",
        service="Test",
        controller="Test",
        action="Get",
        method="GET",
        path="/Test/{id}",
        parameters=(
            Parameter("id", "path", "string", False),
            Parameter("Filter", "query", "string", True),
        ),
    )
    prepared = prepare_endpoint_request(
        endpoint,
        "https://example.test",
        "",
        "",
        {
            "path:id": "12",
            "query:Filter": "Name__eq:=Trial",
            "enabled:query:Filter": "false",
        },
        None,
    )
    assert prepared.resolved_url == "https://example.test/Test/12"


def test_path_parameter_is_required_even_when_catalog_metadata_says_optional() -> None:
    endpoint = Endpoint(
        id="test.get-one",
        service="Test",
        controller="Test",
        action="Get",
        method="GET",
        path="/Test/{id}",
        parameters=(Parameter("id", "path", "string", False),),
    )
    with pytest.raises(ValueError, match="Required path parameter.*id"):
        prepare_endpoint_request(
            endpoint,
            "https://example.test",
            "",
            "",
            {"enabled:path:id": "false"},
            None,
        )


def test_prepare_request_includes_environment_custom_headers() -> None:
    prepared = prepare_endpoint_request(
        _endpoint(),
        "https://example.test",
        "",
        "",
        {},
        None,
        {"X-Tenant": "development"},
    )
    assert prepared.headers["X-Tenant"] == "development"
