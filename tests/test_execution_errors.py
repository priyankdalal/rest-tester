from __future__ import annotations

import requests

from api_tester.execution.errors import classify_exception, classify_status


def test_classify_exception_value_error_is_validation() -> None:
    result = classify_exception(ValueError("Required path values are missing: Id"))
    assert result.category == "validation"


def test_classify_exception_ssl_runtime_message_is_tls() -> None:
    result = classify_exception(RuntimeError("SSL certificate verification failed. ..."))
    assert result.category == "tls"


def test_classify_exception_timeout_runtime_message() -> None:
    result = classify_exception(RuntimeError("Request timed out after 30 seconds. ..."))
    assert result.category == "timeout"


def test_classify_exception_connection_runtime_message() -> None:
    result = classify_exception(RuntimeError("Could not connect to https://qa.example.com/Brand. ..."))
    assert result.category == "connection"


def test_classify_exception_requests_exception_falls_back_to_connection() -> None:
    result = classify_exception(requests.exceptions.ConnectionError("boom"))
    assert result.category == "connection"


def test_classify_exception_unknown_is_internal() -> None:
    result = classify_exception(RuntimeError("totally unexpected failure"))
    assert result.category == "internal"


def test_classify_status_4xx_and_5xx() -> None:
    assert classify_status(404).category == "http_4xx"
    assert classify_status(503).category == "http_5xx"


def test_classify_status_2xx_is_none() -> None:
    assert classify_status(204) is None


def test_signature_normalizes_ids_and_numbers_for_grouping() -> None:
    first = classify_exception(ValueError("Brand 8f14e45fceea167a5a36dedd4bea2543 missing"))
    second = classify_exception(ValueError("Brand 3c59dc048e8850243be8079a5c74d079 missing"))
    assert first.signature("brand.get") == second.signature("brand.get")


def test_signature_differs_by_endpoint_and_category() -> None:
    error = classify_exception(ValueError("Required path values are missing: Id"))
    assert error.signature("brand.get") != error.signature("trial.get")
