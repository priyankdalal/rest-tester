import json
import threading
from copy import deepcopy
from dataclasses import replace
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from api_tester.catalog import Endpoint
from api_tester.client import execute_endpoint
from api_tester.environment import AppSettings
from api_tester.execution.models import ExecutionEnvironmentSnapshot
from api_tester.execution.persistence import RunStore
from api_tester.request_auth import AuthenticationContext
from api_tester.security import compare_reports, execution_report, scan_catalog, scan_response
from api_tester.workspace_store import WorkspaceStore


def review():
    return scan_catalog({"services": [{
        "name": "Demo", "security_metadata": {"document": {
            "openapi": "3.1.0", "paths": {"/items": {"get": {"responses": {"200": {"description": "OK"}}}}},
        }},
        "endpoints": [{"id": "demo", "method": "GET", "path": "/items"}],
    }]})


def test_comparison_identifies_new_changed_and_resolved_without_claiming_fix():
    baseline = review()
    current = deepcopy(baseline)
    current.findings = []
    compared = compare_reports(current, baseline)
    assert compared["resolved"] == baseline.findings
    assert "not independently" in compared["reason"]
    current = deepcopy(baseline)
    current.findings[0].evidence = "Changed evidence"
    assert compare_reports(current, baseline)["changed"] == current.findings
    current.findings[0].evidence = baseline.findings[0].evidence
    current.findings[0].disposition = "accepted risk"
    assert compare_reports(current, baseline)["unchanged"] == current.findings
    for attribute, value in (("state", "partial"), ("scope", "different"),
                             ("environment", "different"), ("rule_version", "old")):
        current = replace(baseline, **{attribute: value})
        compared = compare_reports(current, baseline)
        assert compared["incomparable"] and compared["resolved"] == []


def test_dispositions_expire_reopen_changed_evidence_and_respect_revocation(tmp_path):
    store = WorkspaceStore(tmp_path / "workspace.db")
    try:
        previous = review()
        previous.findings[0].disposition = "accepted risk"
        previous.findings[0].reason = "Temporary development exception"
        previous.findings[0].expires_at = (date.today() + timedelta(days=1)).isoformat()
        store.save_security_report(previous.to_dict())
        current = review()
        inherited = store.inherit_security_dispositions(current.to_dict())
        assert inherited["findings"][0]["disposition"] == "accepted risk"
        assert current.findings[0].disposition == "open"
        current.findings[0].evidence = "New evidence"
        current.findings[0].disposition = "accepted risk"
        current.findings[0].reason = "Stale incoming decision"
        changed = store.inherit_security_dispositions(current.to_dict())
        assert changed["findings"][0]["disposition"] == "open"
        assert changed["findings"][0]["reason"] == ""
        assert any("Changed" in message for message in changed["diagnostics"])
        previous.findings[0].expires_at = (date.today() - timedelta(days=1)).isoformat()
        store.save_security_report(previous.to_dict())
        assert store.inherit_security_dispositions(review().to_dict())["findings"][0]["disposition"] == "open"
        previous.findings[0].expires_at = "invalid"
        store.save_security_report(previous.to_dict())
        assert any("Invalid" in message for message in store.inherit_security_dispositions(review().to_dict())["diagnostics"])
        revoked = review()
        store.save_security_report(revoked.to_dict())
        previous.findings[0].expires_at = ""
        store.save_security_report(previous.to_dict())
        # Upserting an older review does not supersede a newer open decision.
        assert store.inherit_security_dispositions(review().to_dict())["findings"][0]["disposition"] == "open"
    finally:
        store.close()


def test_settings_default_off_and_snapshot_owns_evidence():
    settings = AppSettings.from_dict({}, {})
    assert not settings.security_auto_catalog
    assert not settings.security_auto_response
    assert not settings.security_execution_capture
    settings.security_auto_catalog = settings.security_auto_response = settings.security_execution_capture = True
    restored = AppSettings.from_dict(settings.to_dict(), {})
    assert restored.security_auto_catalog and restored.security_auto_response and restored.security_execution_capture
    evidence = {"services": []}
    snapshot = ExecutionEnvironmentSnapshot(
        "local", "Local", {}, security_document=evidence, security_capture=True,
    )
    evidence["services"].append({"name": "modified"})
    assert snapshot.security_document == {"services": []}
    assert "security_document" not in snapshot.to_dict()


def test_execution_sample_is_partial_and_reports_missing_evidence(tmp_path):
    captured = review()
    store = RunStore(tmp_path / "run.db")
    try:
        store.record_security_review("run", captured.to_dict())
        restored = store.security_reviews("run")
        assert restored == [captured.to_dict()]
        combined = execution_report(restored, "Run")
        assert combined.state == "partial" and combined.findings == captured.findings
        assert compare_reports(combined, combined)["incomparable"]
        assert execution_report([], "Old run").diagnostics
        for index in range(205):
            value = {**captured.to_dict(), "id": f"sample-{index}"}
            store.record_security_review("bounded", value)
        assert len(store.security_reviews("bounded")) == 200
        updated = {**captured.to_dict(), "id": "sample-0", "state": "partial"}
        store.record_security_review("bounded", updated)
        retained = store.security_reviews("bounded")
        assert len(retained) == 200
        assert next(report for report in retained if report["id"] == "sample-0")["state"] == "partial"
    finally:
        store.close()


@pytest.mark.parametrize("auth_context", [False, True])
@pytest.mark.parametrize("header", ["Authorization", "X-Api-Key"])
def test_reflected_credentials_detected_before_redaction(auth_context, header):
    token = "local-synthetic-credential-123"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            reflected = self.headers[header].removeprefix("Bearer ")
            self.wfile.write(json.dumps({"echo": reflected}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = Endpoint("demo", "Demo", "Items", "List", "GET", "/items")
        origin = f"http://127.0.0.1:{server.server_port}"
        context = AuthenticationContext("local", {}, {}, {"Demo": origin}) if auth_context else None
        result = execute_endpoint(
            endpoint, origin, "", "", {}, None, "200", probe_connection=False,
            auth_context=context, auth_mode="manual",
            custom_headers={header: f"Bearer {token}" if header == "Authorization" else token},
        )
        assert result.security_context["secret_reflected"]
        assert token not in result.response_body and token not in result.content.decode()
        report = scan_response({}, endpoint, result)
        assert any(finding.rule_id == "HTTP009" for finding in report.findings)
        assert token not in json.dumps(report.to_dict())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
