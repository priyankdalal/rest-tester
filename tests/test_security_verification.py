"""Only loopback synthetic fixtures are used; no production API is contacted."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest
import requests

from api_tester.security import report_csv, report_html
from api_tester.security_verification import (
    MAX_BODY_BYTES, MAX_UPLOAD_BYTES, VerificationCase, VerificationPlan,
    canonical_origin, execute_verification, validate_plan,
)


@pytest.fixture
def controlled_server():
    state = {"requests": [], "rate": 0, "cancel": None}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_DELETE(self):
            self.respond()

        def respond(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state["requests"].append({
                "path": self.path, "method": self.command,
                "authorization": self.headers.get("Authorization", ""),
                "cookie": self.headers.get("Cookie", ""),
                "body": body, "time": time.monotonic(),
            })
            path = self.path.split("?")[0]
            token = self.headers.get("Authorization", "")
            status, payload = 200, {"safe": True}
            headers = {}
            if path == "/auth":
                status = 200 if token == "Bearer owner-token" else 401
            elif path == "/objects/owner":
                status = 200 if token == "Bearer owner-token" else 403
            elif path == "/properties":
                payload = {"profile": {"name": "fixture-user"}}
            elif path == "/leaky-properties":
                payload = {"items": [{"secret": "response-secret-never-store"}]}
            elif path == "/rate":
                state["rate"] += 1
                status = 429 if state["rate"] > 2 else 200
            elif path == "/admin":
                status = 204 if token == "Bearer admin-token" else 403
            elif path == "/workflow":
                status = 409
            elif path == "/url-policy":
                status = 400
            elif path == "/config":
                headers["X-Content-Type-Options"] = "nosniff"
            elif path == "/inventory/retired":
                status = 404
            elif path == "/downstream-fixture":
                status = 502
            elif path == "/upload":
                status = 413
            elif path == "/cleanup":
                status = 204
            elif path == "/cleanup-fail":
                status = 500
            elif path == "/redirect":
                status = 302
                headers["Location"] = "/never-follow"
            elif path == "/set-cookie":
                headers["Set-Cookie"] = "session=fixture-secret"
            elif path == "/invalid-json":
                payload = None
            elif path == "/large-json":
                payload = {"data": "x" * (MAX_BODY_BYTES + 1)}
            elif path == "/cancel":
                state["cancel"].set()
            content = (json.dumps(payload).encode() if payload is not None else b"not-json")
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if status != 204:
                try:
                    self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def case(category="API8", path="/config", **kwargs):
    return VerificationCase(
        name=kwargs.pop("name", "Explicit reviewed fixture assertion"),
        category=category, method=kwargs.pop("method", "GET"), path=path,
        expected_status=kwargs.pop("expected_status", [200]), **kwargs,
    )


def plan(origin, cases=None, **kwargs):
    return VerificationPlan(
        origin, approved=kwargs.pop("approved", True),
        cases=cases if cases is not None else [case()], min_interval_ms=100, **kwargs,
    )


@pytest.fixture
def upload_fixture():
    # Scratch files stay in this repository, never in system temporary directories.
    path = Path(__file__).parent / f".controlled-security-upload-{uuid4().hex}.bin"
    path.write_bytes(b"local synthetic fixture")
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def test_all_categories_explicit_controlled_assertions(controlled_server, upload_fixture):
    origin, state = controlled_server
    cases = [
        case("API1", "/objects/owner", identity="denied", expected_status=[403]),
        case("API1", "/objects/owner", identity="owner", expected_status=[200]),
        case("API2", "/auth", expected_status=[401]),
        case("API2", "/auth", identity="invalid", expected_status=[401]),
        case("API2", "/auth", identity="owner", expected_status=[200]),
        case("API3", "/properties", identity="denied", forbidden_fields=["secret"]),
        case("API4", "/rate", repeat=2),
        case("API4", "/rate", expected_status=[429]),
        case("API4", "/upload", method="POST", upload_path=str(upload_fixture),
             expected_status=[413], fixture="Synthetic upload rejection fixture"),
        case("API5", "/admin", identity="denied", expected_status=[403]),
        case("API5", "/admin", identity="admin", expected_status=[204]),
        case("API6", "/workflow", method="POST", json_body={"fixture": "conflict"},
             expected_status=[409], fixture="Controlled conflict workflow"),
        case("API7", "/url-policy", method="POST", json_body={"url": "fixture:rejected"},
             expected_status=[400], fixture="Reviewed URL policy rejection, no SSRF target"),
        case("API8", "/config", expected_headers={"X-Content-Type-Options": "nosniff"}),
        case("API9", "/inventory/retired", expected_status=[404]),
        case("API10", "/downstream-fixture", method="POST", json_body={"fixture": "controlled-error"},
             expected_status=[502], fixture="Synthetic downstream rejection fixture"),
    ]
    report = execute_verification(plan(
        origin, cases, allow_mutations=True, acknowledged_risks=True,
        identities={
            "owner": {"Authorization": "Bearer owner-token"},
            "denied": {"Authorization": "Bearer denied-token"},
            "invalid": {"Authorization": "Bearer invalid-token"},
            "admin": {"Authorization": "Bearer admin-token"},
        },
        cleanup=[case(path="/cleanup", method="DELETE", expected_status=[204],
                      fixture="Controlled fixture cleanup")],
    ), threading.Event())
    assert report.state == "complete"
    assert not report.findings
    assert report.endpoints_reviewed == 17
    assert {item.category for item in report.coverage if item.status == "verified"} == {
        f"API{i}" for i in range(1, 11)
    }
    assert all("bounded" in item.explanation for item in report.coverage)
    assert len(state["requests"]) == 18
    assert state["requests"][-1]["method"] == "DELETE"
    assert all(b["time"] - a["time"] >= 0.09
               for a, b in zip(state["requests"], state["requests"][1:]))
    exported = json.dumps(report.to_dict()) + report_html(report) + report_csv(report)
    for secret in ("owner-token", "denied-token", "invalid-token", "admin-token",
                   "fixture-user", "fixture:rejected"):
        assert secret not in exported


@pytest.mark.parametrize("origin", [
    "http://example.com", "http://127.0.0.1.example.com", "http://localhost.example.com",
    "file://127.0.0.1", "https://user:secret@example.com",
    "https://user@example.com", "https://example.com/path",
    "https://example.com?token=secret", "https://example.com#secret",
    "https://example.com?", "https://example.com#", "https://example.com:0",
    "https://example.com:65536", "https://example.com\\@evil.example",
    " https://example.com", "https://example.com\n", "https://%65xample.com",
    "http://127.1", "http://2130706433", "https://-invalid.example",
])
def test_reject_unsafe_origin_before_transport(origin, monkeypatch):
    def forbidden(*_, **__):
        pytest.fail("Validation must not reach transport.")
    monkeypatch.setattr(requests.Session, "send", forbidden)
    with pytest.raises(ValueError):
        execute_verification(plan(origin), threading.Event())


@pytest.mark.parametrize(("origin", "expected"), [
    ("HTTP://LOCALHOST:80/", "http://localhost"),
    ("http://127.0.0.1:080", "http://127.0.0.1"),
    ("http://[::1]:80/", "http://[::1]"),
    ("https://EXAMPLE.com:443/", "https://example.com"),
    ("https://example.com:8443", "https://example.com:8443"),
])
def test_canonical_origin(origin, expected):
    assert canonical_origin(origin) == expected


@pytest.mark.parametrize("path", [
    "https://evil.example/test", "//evil.example/test", "/../config", "/a/../config",
    "/./config", "/a//config", "/\\evil.example", "/%2f%2fevil.example",
    "/%252f%252fevil.example", "/%25252e%25252e/config", "/a%5c..%5cconfig",
    "/config#fragment", "/config%23fragment", "/config?token=credential",
    "/config?%74oken=credential", "/config?access%5ftoken=credential",
    "/config?%2574oken=credential", "/config?api-key=credential",
    "/config?password=credential", "/config\n", "/config%0d%0aHeader:value",
    "/config%3fescape=1", "/config%", "/config%ff",
])
def test_origin_relative_escape_defenses(path, controlled_server):
    origin, state = controlled_server
    with pytest.raises((ValueError, UnicodeError)):
        execute_verification(plan(origin, [case(path=path)]), threading.Event())
    assert not state["requests"]


@pytest.mark.parametrize("updates", [
    {"approved": False}, {"max_requests": 51}, {"max_requests": 0},
    {"max_requests": True}, {"timeout": 31}, {"timeout": 0},
    {"timeout": float("nan")}, {"timeout": float("inf")}, {"timeout": True},
    {"min_interval_ms": 99}, {"min_interval_ms": True},
])
def test_plan_bounds(updates, controlled_server):
    origin, state = controlled_server
    current = plan(origin)
    for key, value in updates.items():
        setattr(current, key, value)
    with pytest.raises(ValueError):
        execute_verification(current, threading.Event())
    assert not state["requests"]


@pytest.mark.parametrize("updates", [
    {"repeat": 11}, {"repeat": 0}, {"repeat": True}, {"expected_status": []},
    {"expected_status": [True]}, {"expected_status": [600]},
    {"category": "OTHER"}, {"method": "TRACE"}, {"identity": "missing"},
    {"headers": {"Authorization": "Bearer secret"}},
    {"headers": {"Cookie": "fixture=secret"}}, {"headers": {"Host": "evil.example"}},
    {"headers": {"X-Api-Key": "secret"}}, {"headers": {"Proxy-Authorization": "secret"}},
    {"headers": {"X-Test": "unsafe\r\ninjected"}}, {"json_body": {"body": "read-only"}},
    {"forbidden_fields": [""]}, {"expected_headers": {"X-Test": "invalid\n"}},
])
def test_case_validation_before_requests(updates, controlled_server):
    origin, state = controlled_server
    current = case()
    for key, value in updates.items():
        setattr(current, key, value)
    with pytest.raises(ValueError):
        execute_verification(plan(origin, [current]), threading.Event())
    assert not state["requests"]


@pytest.mark.parametrize("category", ["API6", "API7", "API10"])
def test_sensitive_categories_need_reviewed_fixture(category, controlled_server):
    origin, state = controlled_server
    with pytest.raises(ValueError, match="fixture"):
        execute_verification(plan(origin, [case(category=category)]), threading.Event())
    assert not state["requests"]


@pytest.mark.parametrize(("allow", "acknowledge", "fixture", "cleanup"), [
    (False, False, "fixture", True), (True, False, "fixture", True),
    (False, True, "fixture", True), (True, True, "", True),
    (True, True, "fixture", False),
])
def test_mutations_require_all_gates(allow, acknowledge, fixture, cleanup, controlled_server):
    origin, state = controlled_server
    with pytest.raises(ValueError, match="Mutations"):
        execute_verification(plan(
            origin, [case(path="/workflow", method="POST", fixture=fixture)],
            allow_mutations=allow, acknowledged_risks=acknowledge,
            cleanup=[case(path="/cleanup")] if cleanup else [],
        ), threading.Event())
    assert not state["requests"]


def test_cleanup_budget_is_reserved_and_required(controlled_server):
    origin, state = controlled_server
    current = plan(origin, [case(repeat=2)], max_requests=2,
                   cleanup=[case(path="/cleanup", expected_status=[204])])
    with pytest.raises(ValueError, match="reserved cleanup"):
        execute_verification(current, threading.Event())
    assert not state["requests"]
    current.max_requests = 3
    report = execute_verification(current, threading.Event())
    assert report.state == "complete"
    assert len(state["requests"]) == 3
    assert state["requests"][-1]["path"] == "/cleanup"


@pytest.mark.parametrize("pre_cancel", [False, True])
def test_cancellation_never_suppresses_cleanup(pre_cancel, controlled_server):
    origin, state = controlled_server
    cancel = threading.Event()
    state["cancel"] = cancel
    if pre_cancel:
        cancel.set()
    report = execute_verification(plan(
        origin, [case(path="/cancel"), case(path="/never-run")],
        cleanup=[case(path="/cleanup", expected_status=[204])],
    ), cancel)
    assert report.state == "partial"
    assert state["requests"][-1]["path"] == "/cleanup"
    assert "/never-run" not in [item["path"] for item in state["requests"]]
    assert any("cleanup" in text.lower() for text in report.diagnostics)
    assert not any(item.status == "verified" for item in report.coverage)


def test_failure_and_cleanup_failure_are_visible(controlled_server):
    origin, state = controlled_server
    report = execute_verification(plan(
        origin, [case(path="/config", expected_status=[403])],
        cleanup=[case(path="/cleanup-fail", expected_status=[204])],
    ), threading.Event())
    assert report.state == "partial"
    assert len(report.findings) == 2
    assert report.findings[1].title.startswith("Cleanup")
    assert next(item for item in report.coverage if item.category == "API8").status == "assertion failed"
    assert [item["path"] for item in state["requests"]] == ["/config", "/cleanup-fail"]


def test_response_fields_and_headers_are_explicit_assertions(controlled_server):
    origin, _ = controlled_server
    report = execute_verification(plan(origin, [
        case("API3", "/leaky-properties", forbidden_fields=["secret"]),
        case("API8", "/config", expected_headers={"X-Content-Type-Options": "wrong"}),
    ]), threading.Event())
    assert len(report.findings) == 2
    assert all(item.severity == "info" for item in report.findings)
    exported = json.dumps(report.to_dict())
    assert "response-secret-never-store" not in exported
    assert "/leaky-properties" not in exported
    assert "wrong" not in exported


@pytest.mark.parametrize("path", ["/invalid-json", "/large-json"])
def test_unreadable_response_not_a_pass(path, controlled_server):
    origin, _ = controlled_server
    report = execute_verification(plan(origin, [
        case("API3", path, forbidden_fields=["secret"]),
    ]), threading.Event())
    assert report.state == "partial"
    assert next(item for item in report.coverage if item.category == "API3").status == "unknown"
    assert any("error" in item for item in report.diagnostics)


def test_no_redirects_or_cookie_persistence(controlled_server, monkeypatch):
    origin, state = controlled_server
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "nonexistent-environment-certificate")
    report = execute_verification(plan(origin, [
        case(path="/redirect", expected_status=[302]),
        case(path="/set-cookie"),
        case(path="/config"),
    ]), threading.Event())
    assert report.state == "complete"
    assert [item["path"] for item in state["requests"]] == ["/redirect", "/set-cookie", "/config"]
    assert all(not item["cookie"] for item in state["requests"])


def test_absent_credentials_and_named_identity_are_isolated(controlled_server):
    origin, state = controlled_server
    report = execute_verification(plan(origin, [
        case("API2", "/auth", identity="owner"),
        case("API2", "/auth", expected_status=[401]),
    ], identities={"owner": {"Authorization": "Bearer owner-token"}}), threading.Event())
    assert report.state == "complete"
    assert [item["authorization"] for item in state["requests"]] == ["Bearer owner-token", ""]


def test_no_netrc_and_safe_exception_redaction(controlled_server, monkeypatch):
    origin, state = controlled_server
    original = requests.Session.send
    calls = []
    def send(session, prepared, **kwargs):
        assert session.trust_env is False
        assert session.auth is None
        assert kwargs["allow_redirects"] is False
        assert kwargs["proxies"] == {}
        assert kwargs["timeout"] <= 30
        calls.append(prepared.path_url)
        if len(calls) == 1:
            raise requests.ConnectionError("credential-unique-value password=do-not-save server-body-secret")
        return original(session, prepared, **kwargs)
    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(requests.sessions, "get_netrc_auth",
                        lambda *_: pytest.fail("Ambient netrc must not be consulted."))
    report = execute_verification(plan(
        origin, [case(name="credential-unique-value", path="/config?fixture=credential-unique-value")],
        identities={"sensitive-name": {"X-Custom-Credential": "credential-unique-value"}},
        cleanup=[case(path="/cleanup", expected_status=[204])],
    ), threading.Event())
    assert report.state == "partial"
    assert all(item.status != "verified" for item in report.coverage)
    assert state["requests"][-1]["path"] == "/cleanup"
    exported = json.dumps(report.to_dict()) + report_html(report) + report_csv(report)
    for secret in ("credential-unique-value", "do-not-save", "server-body-secret", "sensitive-name"):
        assert secret not in exported


def test_upload_validation_and_size_limit(controlled_server, upload_fixture):
    origin, state = controlled_server
    upload_fixture.write_bytes(b"x" * (MAX_UPLOAD_BYTES + 1))
    current = plan(
        origin, [case(method="POST", upload_path=str(upload_fixture), fixture="Upload fixture")],
        allow_mutations=True, acknowledged_risks=True, cleanup=[case(path="/cleanup")],
    )
    with pytest.raises(ValueError, match="1 MiB"):
        execute_verification(current, threading.Event())
    assert not state["requests"]


def test_validate_returns_detached_memory_only_plan(controlled_server):
    origin, _ = controlled_server
    current = plan(origin, identities={"owner": {"Authorization": "Bearer owner-token"}})
    detached = validate_plan(current)
    current.identities["owner"]["Authorization"] = "changed"
    assert detached.identities["owner"]["Authorization"] == "Bearer owner-token"
    assert "owner-token" not in repr(detached)
