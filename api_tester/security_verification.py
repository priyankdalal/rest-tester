"""Opt-in, bounded execution of user-authored controlled security assertions.

No payloads, identities, targets, or exploit attempts are generated. Credentials
and response bodies remain in memory and are never included in the report.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import requests

from .security import CATEGORIES, Coverage, Finding, SecurityReport


MAX_BODY_BYTES = 256 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024
_METHODS = {"GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"}
_READ_ONLY = {"GET", "HEAD", "OPTIONS"}
_CREDENTIAL = re.compile(
    r"authorization|cookie|password|passwd|secret|token|api[-_]?key|credential",
    re.IGNORECASE,
)
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_TRANSPORT_HEADERS = {
    "host", "content-length", "transfer-encoding", "connection", "upgrade",
    "proxy-authorization", "proxy-connection", "expect",
}


@dataclass
class VerificationCase:
    name: str
    category: str
    method: str
    path: str
    identity: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    json_body: Any = None
    expected_status: list[int] = field(default_factory=list)
    expected_headers: dict[str, str] = field(default_factory=dict)
    forbidden_fields: list[str] = field(default_factory=list)
    repeat: int = 1
    fixture: str = ""
    upload_path: str = ""
    upload_field: str = "file"


@dataclass
class VerificationPlan:
    approved_origin: str
    approved: bool = False
    cases: list[VerificationCase] = field(default_factory=list)
    cleanup: list[VerificationCase] = field(default_factory=list)
    identities: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    max_requests: int = 20
    timeout: float = 10
    min_interval_ms: int = 250
    allow_mutations: bool = False
    acknowledged_risks: bool = False


def _text(value: Any) -> bool:
    return isinstance(value, str) and not any(ord(c) < 32 or ord(c) == 127 for c in value)


def canonical_origin(value: str) -> str:
    """Normalize an origin, allowing plaintext HTTP only for literal loopback."""
    if not _text(value) or not value or any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("An exact HTTP(S) origin without whitespace is required.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"} or not host
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or "?" in value or "#" in value or "%" in parsed.netloc
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
            host = address.compressed
            loopback = address.is_loopback
        except ValueError:
            host = host.encode("idna").decode("ascii").lower()
            if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError
            if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                   for label in host.split(".")):
                raise ValueError
            loopback = host == "localhost"
        if parsed.scheme == "http" and not loopback:
            raise ValueError
        port = port if port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Origin must be HTTPS, or HTTP on literal loopback; no credentials or URL suffixes.") from None
    rendered_host = f"[{host}]" if ":" in host else host
    suffix = "" if port == (443 if parsed.scheme == "https" else 80) else f":{port}"
    return f"{parsed.scheme}://{rendered_host}{suffix}"


def _validate_path(value: str) -> None:
    if not _text(value) or not value.startswith("/") or len(value) > 4096:
        raise ValueError("Cases require a bounded origin-relative path beginning with '/'.")
    decoded = value
    for _ in range(8):
        if (
            any(c.isspace() for c in decoded) or "\\" in decoded or "#" in decoded
            or not decoded.startswith("/") or decoded.startswith("//")
        ):
            raise ValueError("Path contains an unsafe URL escape or suffix.")
        path, _, query = decoded.partition("?")
        if "//" in path or any(piece in {".", ".."} for piece in path.split("/")):
            raise ValueError("Path traversal and double slashes are not permitted.")
        for item in re.split(r"[&;]", query):
            key = item.partition("=")[0]
            if _CREDENTIAL.search(key):
                raise ValueError("Credentials must be supplied only through a named in-memory identity.")
        if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
            raise ValueError("Path contains control characters.")
        try:
            next_value = unquote(decoded, errors="strict")
        except UnicodeError:
            raise ValueError("Path contains invalid URL encoding.") from None
        if next_value == decoded:
            if "%" in decoded:
                raise ValueError("Malformed or ambiguous URL escapes are not permitted.")
            break
        # Reject encoded URL delimiters and dot segments, including nested escapes.
        if any(token in decoded.lower() for token in ("%2f", "%5c", "%2e", "%3f", "%23", "%40")):
            raise ValueError("Encoded path delimiters are not permitted.")
        decoded = next_value
    else:
        raise ValueError("Nested URL encoding exceeds the validation limit.")


def _headers(value: Any, *, identity: bool = False, expectation: bool = False) -> None:
    if not isinstance(value, dict) or len(value) > 40:
        raise ValueError("Headers must be a bounded string-to-string object.")
    names: set[str] = set()
    for key, item in value.items():
        if (
            not isinstance(key, str) or not _HEADER_NAME.fullmatch(key)
            or not _text(item) or len(item) > 8192 or key.lower() in names
        ):
            raise ValueError("Header names and values must be valid bounded strings.")
        names.add(key.lower())
        if not expectation and key.lower() in _TRANSPORT_HEADERS:
            raise ValueError("Transport and proxy override headers are not permitted.")
        if not identity and not expectation and _CREDENTIAL.search(key):
            raise ValueError("Credential headers require a named in-memory identity.")


def validate_plan(plan: VerificationPlan) -> VerificationPlan:
    """Return a detached validated plan; failures never echo supplied input."""
    if not isinstance(plan, VerificationPlan):
        raise ValueError("A VerificationPlan is required.")
    plan = copy.deepcopy(plan)
    plan.approved_origin = canonical_origin(plan.approved_origin)
    if plan.approved is not True:
        raise ValueError("Explicit origin approval is required before any request.")
    if any(type(getattr(plan, key)) is not bool
           for key in ("allow_mutations", "acknowledged_risks")):
        raise ValueError("Mutation gates must be explicit booleans.")
    if type(plan.max_requests) is not int or not 1 <= plan.max_requests <= 50:
        raise ValueError("Request budget must be between 1 and 50.")
    if (type(plan.timeout) not in (int, float) or not math.isfinite(plan.timeout)
            or not 0 < plan.timeout <= 30):
        raise ValueError("Timeout must be greater than zero and at most 30 seconds.")
    if type(plan.min_interval_ms) is not int or not 100 <= plan.min_interval_ms <= 60000:
        raise ValueError("Minimum interval must be between 100 and 60000 milliseconds.")
    if not isinstance(plan.identities, dict) or len(plan.identities) > 20:
        raise ValueError("Identities must be a bounded in-memory object.")
    for name, headers in plan.identities.items():
        if not _text(name) or not name or len(name) > 100:
            raise ValueError("Each identity requires a short name.")
        _headers(headers, identity=True)
    if not isinstance(plan.cases, list) or not plan.cases or not isinstance(plan.cleanup, list):
        raise ValueError("At least one explicit case and a cleanup list are required.")
    if len(plan.cases) + len(plan.cleanup) > 50:
        raise ValueError("Plan exceeds the bounded case limit.")
    total = 0
    for case in plan.cases + plan.cleanup:
        if not isinstance(case, VerificationCase):
            raise ValueError("Every case must be a VerificationCase.")
        if not _text(case.name) or not case.name or len(case.name) > 200:
            raise ValueError("Each case requires a short reviewed description.")
        if case.category not in CATEGORIES or case.method not in _METHODS:
            raise ValueError("Case category or HTTP method is unsupported.")
        _validate_path(case.path)
        if not isinstance(case.identity, str) or (case.identity and case.identity not in plan.identities):
            raise ValueError("A case references an unavailable identity.")
        _headers(case.headers)
        _headers(case.expected_headers, expectation=True)
        if (not isinstance(case.expected_status, list)
                or any(type(s) is not int or not 100 <= s <= 599 for s in case.expected_status)):
            raise ValueError("Expected statuses must be explicit HTTP status integers.")
        if (not isinstance(case.forbidden_fields, list) or len(case.forbidden_fields) > 100
                or any(not _text(s) or not s or len(s) > 200 for s in case.forbidden_fields)):
            raise ValueError("Forbidden fields must be a bounded list of JSON property names.")
        if not (case.expected_status or case.expected_headers or case.forbidden_fields):
            raise ValueError("Every case requires at least one explicit response assertion.")
        if type(case.repeat) is not int or not 1 <= case.repeat <= 10:
            raise ValueError("Repeat must be between 1 and 10; no brute-force generation.")
        total += case.repeat
        if not _text(case.fixture) or len(case.fixture) > 1000:
            raise ValueError("Fixture descriptions must be bounded strings.")
        if case.category in {"API6", "API7", "API10"} and not case.fixture.strip():
            raise ValueError("Business-flow, URL-policy and downstream cases require a reviewed controlled fixture description.")
        mutating = case.method not in _READ_ONLY or bool(case.upload_path)
        if mutating and not (plan.allow_mutations and plan.acknowledged_risks
                             and case.fixture.strip() and plan.cleanup):
            raise ValueError("Mutations require both risk gates, a fixture name, and an explicit cleanup plan.")
        try:
            body = json.dumps(case.json_body, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            raise ValueError("Request JSON must be a bounded serializable value.") from None
        if len(body) > MAX_BODY_BYTES:
            raise ValueError("Request JSON exceeds the bounded payload limit.")
        if case.method in _READ_ONLY and (case.json_body is not None or case.upload_path):
            raise ValueError("Read-only methods cannot carry bodies or uploads.")
        if not _text(case.upload_path) or not _text(case.upload_field) or not case.upload_field:
            raise ValueError("Upload fields and paths must be explicit strings.")
        if case.upload_path:
            if case.json_body is not None:
                raise ValueError("Choose JSON or one bounded upload, not both.")
            try:
                path = Path(case.upload_path)
                if not path.is_file() or path.stat().st_size > MAX_UPLOAD_BYTES:
                    raise OSError
            except (OSError, ValueError):
                raise ValueError("Upload fixture must be an existing file of at most 1 MiB.") from None
    if total > plan.max_requests:
        raise ValueError("Budget must cover all case repeats and reserved cleanup requests.")
    return plan


def _forbidden_present(document: Any, names: set[str]) -> bool:
    pending = [document]
    count = 0
    while pending:
        count += 1
        if count > 50000:
            raise ValueError("Response JSON exceeds the assertion traversal limit.")
        value = pending.pop()
        if isinstance(value, dict):
            if names.intersection(value):
                return True
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def execute_verification(plan: VerificationPlan, cancel: threading.Event) -> SecurityReport:
    """Execute only approved assertions; always attempt reserved cleanup.

    A verified category refers solely to the supplied bounded assertions, never
    to absence of an OWASP vulnerability. Failures are observations, not exploit
    verdicts. Validation completes before opening any network connection.
    """
    plan = validate_plan(plan)
    report = SecurityReport(
        id=str(uuid4()), created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        mode="active verification", scope=plan.approved_origin,
        catalog_fingerprint="", rule_version="active-1.0",
        environment=plan.approved_origin,
    )
    report.diagnostics.append(
        "Controlled user-authored assertions only; not a penetration test, "
        "vulnerability verdict, certification, or general security assurance. "
        "No response bodies or identity credentials are stored."
    )
    outcomes: dict[str, list[str]] = {category: [] for category in CATEGORIES}
    sent = 0
    last_start: float | None = None
    cleanup_budget = sum(case.repeat for case in plan.cleanup)
    attempted_main = False

    def run(case: VerificationCase, index: int, *, cleanup: bool) -> bool:
        nonlocal sent, last_start
        label = f"{'Cleanup' if cleanup else 'Case'} {index}"
        if sent >= plan.max_requests - (0 if cleanup else cleanup_budget):
            report.state = "partial"
            report.diagnostics.append(f"{label}: request budget reached; not verified.")
            if not cleanup:
                outcomes[case.category].append("error")
            return False
        if last_start is not None:
            wait = max(0.0, plan.min_interval_ms / 1000 - (time.monotonic() - last_start))
            if cleanup:
                time.sleep(wait)
            elif cancel.wait(wait):
                return False
        if not cleanup and cancel.is_set():
            return False
        last_start = time.monotonic()
        sent += 1
        if not cleanup:
            report.endpoints_reviewed += 1
        try:
            headers = dict(case.headers)
            headers.update(plan.identities.get(case.identity, {}))
            kwargs: dict[str, Any] = {"headers": headers}
            if case.json_body is not None:
                kwargs["json"] = case.json_body
            if case.upload_path:
                # Read once under the size cap to bound races and multipart allocation.
                with Path(case.upload_path).open("rb") as stream:
                    upload = stream.read(MAX_UPLOAD_BYTES + 1)
                if len(upload) > MAX_UPLOAD_BYTES:
                    raise ValueError("Upload grew beyond limit.")
                kwargs["files"] = {case.upload_field: ("fixture.bin", upload, "application/octet-stream")}
            with requests.Session() as session:
                session.trust_env = False
                session.auth = None
                session.cookies.clear()
                request = requests.Request(case.method, plan.approved_origin + case.path, **kwargs)
                prepared = session.prepare_request(request)
                target = urlsplit(prepared.url)
                if canonical_origin(f"{target.scheme}://{target.netloc}") != plan.approved_origin:
                    raise ValueError("Prepared request escaped approved origin.")
                with session.send(
                    prepared, timeout=plan.timeout, allow_redirects=False,
                    stream=True, proxies={},
                ) as response:
                    failures: list[str] = []
                    if case.expected_status and response.status_code not in case.expected_status:
                        failures.append("expected status")
                    if any(response.headers.get(key) != value
                           for key, value in case.expected_headers.items()):
                        failures.append("expected response header")
                    if case.forbidden_fields:
                        content = bytearray()
                        deadline = time.monotonic() + plan.timeout
                        for chunk in response.iter_content(8192):
                            if time.monotonic() > deadline:
                                raise ValueError("Response read deadline exceeded.")
                            content.extend(chunk)
                            if len(content) > MAX_BODY_BYTES:
                                raise ValueError("Response assertion size exceeded.")
                            if not cleanup and cancel.is_set():
                                raise ValueError("Cancelled during response assertion.")
                        document = json.loads(content)
                        if _forbidden_present(document, set(case.forbidden_fields)):
                            failures.append("forbidden JSON property")
                    if failures:
                        if cleanup:
                            report.state = "partial"
                        else:
                            outcomes[case.category].append("failed")
                        report.findings.append(Finding(
                            rule_id="ACTIVE-ASSERTION", title=f"{label}: controlled assertion failed",
                            severity="info", category=case.category,
                            evidence_class="controlled assertion", service="approved origin",
                            endpoint_id=label, method=case.method, path="", location=label,
                            evidence=f"HTTP {response.status_code}; failed: {', '.join(failures)}.",
                            explanation="The supplied assertion did not match this bounded exchange.",
                            remediation="Review the controlled fixture, expected behavior and server implementation.",
                            limitation="No exploit or vulnerability verdict; only this explicit assertion was checked.",
                            confidence="high for the observed bounded assertion only",
                        ))
                        report.diagnostics.append(f"{label}: assertion failed.")
                    else:
                        if not cleanup:
                            outcomes[case.category].append("passed")
                        report.diagnostics.append(f"{label}: explicit assertions passed (HTTP {response.status_code}).")
            return True
        except Exception:
            # Requests exceptions can contain URLs, credentials and server text.
            report.state = "partial"
            report.diagnostics.append(f"{label}: transport, fixture or assertion error; not verified.")
            if not cleanup:
                outcomes[case.category].append("error")
            return True

    try:
        for index, case in enumerate(plan.cases, 1):
            for _ in range(case.repeat):
                if cancel.is_set():
                    report.state = "partial"
                    break
                attempted_main = True
                if not run(case, index, cleanup=False):
                    report.state = "partial"
                    break
            if cancel.is_set():
                break
    finally:
        # Cancellation cannot consume or suppress the reserved cleanup budget.
        # Also run cleanup on pre-start cancellation: a supplied fixture may exist.
        for index, case in enumerate(plan.cleanup, 1):
            for _ in range(case.repeat):
                run(case, index, cleanup=True)
    if cancel.is_set():
        report.state = "partial"
        report.diagnostics.append("Verification cancelled; reserved cleanup was still attempted.")
    if not attempted_main:
        report.state = "partial"
    for category in CATEGORIES:
        results = outcomes[category]
        expected = sum(case.repeat for case in plan.cases if case.category == category)
        status = "not tested"
        explanation = "No controlled assertions supplied for this category."
        if expected:
            if len(results) == expected and all(value == "passed" for value in results):
                status = "verified"
                explanation = f"Only the {expected} supplied bounded assertion(s) passed; category-wide enforcement is unknown."
            elif "failed" in results:
                status = "assertion failed"
                explanation = "An explicit controlled assertion failed; this is not a vulnerability verdict."
            else:
                status = "unknown"
                explanation = "Cancellation or execution errors left supplied assertions incomplete."
        report.coverage.append(Coverage(category, status, explanation))
    report.diagnostics.append(f"Requests attempted: {sent}/{plan.max_requests}; cleanup reservation: {cleanup_budget}.")
    return report
