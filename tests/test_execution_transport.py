from __future__ import annotations

from types import SimpleNamespace

import pytest

from api_tester.catalog import Endpoint, Parameter
from api_tester.execution.models import ExecutionEnvironmentSnapshot, RequestTemplate
from api_tester.execution.transport import WorkerTransport


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False

    def request(self, *args, **kwargs):
        from api_tester import client

        return client.requests.request(*args, **kwargs)

    def close(self) -> None:
        self.closed = True


def _endpoint(path: str = "/Test", method: str = "GET", parameters: tuple[Parameter, ...] = ()) -> Endpoint:
    return Endpoint(
        id="test.endpoint",
        service="Test",
        controller="Test",
        action="Get",
        method=method,
        path=path,
        parameters=parameters,
    )


def _environment() -> ExecutionEnvironmentSnapshot:
    return ExecutionEnvironmentSnapshot(
        environment_id="env-1",
        environment_name="QA",
        base_urls={"Test": "https://example.test"},
        timeout=12.5,
        verify_ssl=True,
        custom_headers={"X-Environment": "qa"},
    )


def _install_fake_request(monkeypatch: pytest.MonkeyPatch, *, status_code: int = 200):
    calls: list[dict[str, object]] = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return SimpleNamespace(
            status_code=status_code,
            headers={"content-type": "application/json"},
            content=b'{"ok":true}',
            url=url,
            request=SimpleNamespace(headers=kwargs.get("headers", {}), body=None),
            reason="OK",
        )

    monkeypatch.setattr("api_tester.client.requests.request", fake_request)
    monkeypatch.setattr("api_tester.execution.transport.requests.Session", _FakeSession)
    return calls


def test_worker_transport_success_returns_passed_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_request(monkeypatch, status_code=200)
    transport = WorkerTransport(_environment(), worker_id=2, run_id="DR-1")
    template = RequestTemplate(endpoint_id="test.endpoint", service="Test", method="GET", path="/Test")

    result, sample, error = transport.execute(_endpoint(), template)

    assert result is not None
    assert result.passed is True
    assert sample.outcome == "passed"
    assert sample.status_code == 200
    assert sample.worker_id == 2
    assert sample.run_id == "DR-1"
    assert error is None
    assert calls[0]["verify"] is True
    assert calls[0]["timeout"] == 12.5
    assert calls[0]["headers"]["X-Environment"] == "qa"


def test_worker_transport_failed_status_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_request(monkeypatch, status_code=404)
    transport = WorkerTransport(_environment(), worker_id=1, run_id="DR-2")
    template = RequestTemplate(
        endpoint_id="test.endpoint",
        service="Test",
        method="GET",
        path="/Test",
        expected_status="200",
    )

    result, sample, error = transport.execute(_endpoint(), template)

    assert result is not None
    assert result.passed is False
    assert sample.outcome == "failed"
    assert sample.status_code == 404
    assert sample.error_category == "http_4xx"
    assert error is not None
    assert error.category == "http_4xx"


def test_worker_transport_catches_validation_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_request(monkeypatch)
    transport = WorkerTransport(_environment(), worker_id=1, run_id="DR-3")
    endpoint = _endpoint(
        path="/Test/{id}",
        parameters=(Parameter("id", "path", "string", True),),
    )
    template = RequestTemplate(
        endpoint_id="test.endpoint",
        service="Test",
        method="GET",
        path="/Test/{id}",
    )

    result, sample, error = transport.execute(endpoint, template)

    assert result is None
    assert sample.outcome == "error"
    assert sample.error_category == "validation"
    assert "Required path parameter" in sample.error_message
    assert error is not None
    assert error.category == "validation"


def test_worker_transport_closes_owned_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_request(monkeypatch)
    with WorkerTransport(_environment(), worker_id=1, run_id="DR-4") as transport:
        session = transport._session
        assert session.closed is False
    assert session.closed is True

    transport = WorkerTransport(_environment(), worker_id=1, run_id="DR-5")
    session = transport._session
    transport.close()
    assert session.closed is True
