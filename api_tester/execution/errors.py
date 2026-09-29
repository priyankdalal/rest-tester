"""Shared error taxonomy for the load-testing and Data Runner execution engines.

See ``load-testing-and-data-runner.md`` section 5.2. Every executed request
must be classified into exactly one of these categories so failures can be
grouped into signatures without losing one sanitized representative sample.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import requests

#: Ordered roughly from "never reached the network" to "reached it and the
#: response/assertions were the problem".
ERROR_CATEGORIES = (
    "validation",
    "preparation",
    "authentication",
    "dns",
    "connection",
    "tls",
    "timeout",
    "http_4xx",
    "http_5xx",
    "parsing",
    "assertion",
    "scheduler_overload",
    "cancelled",
    "internal",
)


@dataclass(frozen=True)
class ClassifiedError:
    category: str
    message: str

    def signature(self, endpoint_id: str) -> str:
        """A stable, low-cardinality key for grouping identical failures.

        Digits and quoted/hex-like tokens are collapsed so e.g. two 404s for
        different IDs group together instead of producing one row per value.
        """
        normalized = re.sub(r"[0-9a-fA-F-]{8,}", "<id>", self.message)
        normalized = re.sub(r"\d+", "<n>", normalized)
        normalized = normalized.strip()[:160]
        return f"{endpoint_id}|{self.category}|{normalized}"


def classify_exception(exc: BaseException, *, expected_status: str | None = None) -> ClassifiedError:
    """Maps a raised exception to an :class:`ClassifiedError` category.

    ``client.execute_endpoint`` already turns SSL/timeout/connection failures
    into ``RuntimeError`` with a friendly, already-sanitized message (see
    ``api_tester/client.py``), so string sniffing on that message is the most
    reliable classification signal available without re-parsing the
    underlying ``requests`` exception, which the client layer deliberately
    does not propagate.
    """
    if isinstance(exc, ValueError):
        return ClassifiedError("validation", str(exc))
    message = str(exc)
    lowered = message.lower()
    if isinstance(exc, requests.exceptions.SSLError) or "ssl certificate" in lowered:
        return ClassifiedError("tls", message)
    if isinstance(exc, requests.exceptions.Timeout) or "timed out" in lowered:
        return ClassifiedError("timeout", message)
    if isinstance(exc, requests.exceptions.ConnectionError) or "could not connect" in lowered:
        return ClassifiedError("connection", message)
    if "authentication" in lowered or "invalid_token" in lowered or "token" in lowered and "renew" in lowered:
        return ClassifiedError("authentication", message)
    if isinstance(exc, requests.RequestException):
        return ClassifiedError("connection", message)
    return ClassifiedError("internal", message)


def classify_status(status_code: int) -> ClassifiedError | None:
    """Classifies an HTTP response that was received but was not the expected status."""
    if 400 <= status_code < 500:
        return ClassifiedError("http_4xx", f"HTTP {status_code}")
    if 500 <= status_code < 600:
        return ClassifiedError("http_5xx", f"HTTP {status_code}")
    return None
