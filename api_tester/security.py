"""Offline contract checks and passive, evidence-limited HTTP exchange review."""

from __future__ import annotations

import csv
import fnmatch
import hashlib
import html
import io
import ipaddress
import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from .catalog import Catalog, Endpoint
from .client import ApiResult


RULE_VERSION = "1.1"
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_NODES = 50000
MAX_FINDINGS = 5000
BODY_LIMIT = 256 * 1024
CATEGORIES = {
    "API1": "Broken Object Level Authorization",
    "API2": "Broken Authentication",
    "API3": "Broken Object Property Level Authorization",
    "API4": "Unrestricted Resource Consumption",
    "API5": "Broken Function Level Authorization",
    "API6": "Unrestricted Access to Sensitive Business Flows",
    "API7": "Server Side Request Forgery",
    "API8": "Security Misconfiguration",
    "API9": "Improper Inventory Management",
    "API10": "Unsafe Consumption of APIs",
}
_SECRET_NAME = re.compile(
    r"(?i)^(authorization|proxy-authorization|cookie|set-cookie|password|passwd|"
    r"secret|client_secret|access_token|refresh_token|api[_-]?key|x-api-key|token)$"
)


def safe_text(value: Any, limit: int = 2000) -> str:
    text = str(value)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(
        r"(?i)([?&](?:access_token|refresh_token|api[_-]?key|token|password)=)[^&\s]*",
        r"\1[REDACTED]", text,
    )
    text = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", text)
    return text[:limit]


def sanitize_contract(document: dict[str, Any]) -> dict[str, Any]:
    """Retain contract structure, not literal credential defaults/examples."""
    count = 0
    def visit(node: Any, depth: int = 0, sensitive: bool = False, example: bool = False) -> Any:
        nonlocal count
        count += 1
        if count > MAX_NODES:
            raise ValueError("Contract exceeds the security review traversal limit.")
        if depth > 80:
            raise ValueError("Contract nesting exceeds the security review limit (80).")
        if isinstance(node, dict):
            private = sensitive or bool(_SECRET_NAME.fullmatch(str(node.get("name", ""))))
            return {
                str(key): (
                    (["[REDACTED]" for _ in value] if key == "enum" and isinstance(value, list) else "[REDACTED]")
                    if (private and key in {"example", "default", "examples", "enum", "const"})
                    else visit(value, depth + 1, bool(_SECRET_NAME.fullmatch(str(key))),
                               example or key in {"example", "default", "examples"})
                )
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [visit(item, depth + 1, sensitive, example) for item in node]
        if sensitive and example and node not in (None, "", False):
            return "[REDACTED]"
        return safe_text(node, MAX_DOCUMENT_BYTES) if isinstance(node, str) else node
    return visit(document)


@dataclass
class Finding:
    rule_id: str
    title: str
    severity: str
    category: str
    evidence_class: str
    service: str
    endpoint_id: str
    method: str
    path: str
    location: str
    evidence: str
    explanation: str
    remediation: str
    limitation: str
    disposition: str = "open"
    reason: str = ""
    confidence: str = "high for the documented condition; enforcement unverified"
    expires_at: str = ""


@dataclass
class Coverage:
    category: str
    status: str
    explanation: str


@dataclass
class SecurityReport:
    id: str
    created_at: str
    mode: str
    scope: str
    catalog_fingerprint: str
    rule_version: str
    endpoints_reviewed: int = 0
    findings: list[Finding] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    state: str = "complete"
    environment: str = ""
    source_fingerprint: str = ""
    comparison: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SecurityReport:
        data = dict(value)
        data["findings"] = [Finding(**item) for item in data.get("findings", [])]
        data["coverage"] = [Coverage(**item) for item in data.get("coverage", [])]
        return cls(**data)


def catalog_document(catalog: Catalog) -> dict[str, Any]:
    return asdict(catalog)


def finding_identity(finding: Finding) -> str:
    return fingerprint({
        "rule": finding.rule_id, "service": finding.service,
        "method": finding.method, "path": finding.path, "location": finding.location,
        "evidence_class": finding.evidence_class,
    })


def compare_reports(current: SecurityReport, baseline: SecurityReport) -> dict[str, Any]:
    compatible = (
        current.mode == baseline.mode and current.scope == baseline.scope
        and current.environment == baseline.environment
        and current.rule_version == baseline.rule_version
    )
    complete = current.state == baseline.state == "complete"
    if not compatible or not complete:
        return {
            "new": [], "resolved": [], "unchanged": [], "changed": [],
            "incomparable": True,
            "reason": "Comparison requires complete reports with matching mode, scope, environment, and rule version.",
        }
    old = {finding_identity(item): item for item in baseline.findings}
    new = {finding_identity(item): item for item in current.findings}
    changed, unchanged = [], []
    for key in new.keys() & old.keys():
        left, right = asdict(new[key]), asdict(old[key])
        for value in (left, right):
            for name in ("disposition", "reason", "expires_at"):
                value.pop(name, None)
        (unchanged if left == right else changed).append(new[key])
    return {
        "new": [item for key, item in new.items() if key not in old],
        "resolved": [item for key, item in old.items() if key not in new],
        "unchanged": unchanged, "changed": changed, "incomparable": False,
        "reason": "Resolved means absent from this complete review, not independently verified as remediated.",
    }


def execution_report(reports: list[dict[str, Any]], scope: str) -> SecurityReport:
    parsed = [SecurityReport.from_dict(value) for value in reports[:200]]
    report = _report({}, "execution sample", scope)
    report.state = "partial"
    report.diagnostics.append(
        "Sampled execution review only; at most 200 captured exchanges are included. "
        "No findings does not mean all requests or security controls passed."
    )
    if not parsed:
        report.diagnostics.append(
            "No exchange security evidence captured. Enable sampled capture before running; "
            "old runs and uncaptured responses cannot be retrospectively verified."
        )
    else:
        report.catalog_fingerprint = parsed[0].catalog_fingerprint
        report.environment = parsed[0].environment
        unique: dict[str, Finding] = {}
        for value in parsed:
            report.endpoints_reviewed += value.endpoints_reviewed
            for finding in value.findings:
                unique.setdefault(finding_identity(finding), finding)
        report.findings = list(unique.values())
        report.source_fingerprint = fingerprint([value.source_fingerprint for value in parsed])
    report.coverage = [
        Coverage(f"{key}:2023 - {title}", "partial" if parsed and key in {"API2", "API3", "API8"} else "not assessed",
                 "Bounded sampled exchanges; not a complete execution or authorization audit.")
        for key, title in CATEGORIES.items()
    ]
    return report


def fingerprint(document: Any) -> str:
    content = dict(document) if isinstance(document, dict) else document
    if isinstance(content, dict):
        for name in ("generated_at", "workspace_root"):
            content.pop(name, None)
    return hashlib.sha256(json.dumps(
        content, sort_keys=True, ensure_ascii=True, default=str,
    ).encode()).hexdigest()


def _report(document: dict[str, Any], mode: str, scope: str) -> SecurityReport:
    return SecurityReport(
        str(uuid4()), datetime.now(UTC).isoformat(timespec="seconds"),
        mode, safe_text(scope), fingerprint(document), RULE_VERSION,
    )


def _local(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _walk(node: Any, pointer: str = "#", depth: int = 0):
    if depth > 80:
        raise ValueError("Contract nesting exceeds the security review limit (80).")
    yield pointer, node
    if isinstance(node, dict):
        for key, child in node.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from _walk(child, pointer + "/" + escaped, depth + 1)
    elif isinstance(node, (list, tuple)):
        for index, child in enumerate(node):
            yield from _walk(child, pointer + "/" + str(index), depth + 1)


def _bounded_walk(node: Any, pointer: str = "#"):
    for count, item in enumerate(_walk(node, pointer), start=1):
        if count > MAX_NODES:
            raise ValueError("Security evidence exceeds the traversal limit.")
        yield item


def _resolve(document: dict[str, Any], node: Any, seen: set[str] | None = None) -> dict[str, Any]:
    seen = set() if seen is None else set(seen)
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError("External or invalid reference is not resolved automatically.")
        if ref in seen:
            raise ValueError("Reference cycle needs manual review.")
        seen.add(ref)
        target: Any = document
        for piece in ref[2:].split("/"):
            piece = piece.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or piece not in target:
                raise ValueError("Unresolved local reference.")
            target = target[piece]
        node = target
    if not isinstance(node, dict):
        raise ValueError("Expected a contract object.")
    return node


def _add(report: SecurityReport, endpoint: dict[str, Any], service: str, rule: str,
         title: str, category: str, location: str, evidence: str, explanation: str,
         remediation: str, severity: str = "low", evidence_class: str = "contract gap",
         limitation: str = "This contract check does not establish server-side enforcement.") -> None:
    if len(report.findings) >= MAX_FINDINGS:
        raise ValueError("Finding limit reached; narrow the review scope.")
    finding = Finding(
        rule, title, severity, category, evidence_class, safe_text(service),
        safe_text(endpoint.get("id", "")), safe_text(endpoint.get("method", "")),
        safe_text(endpoint.get("path", "")), safe_text(location), safe_text(evidence),
        explanation, remediation, limitation,
    )
    if evidence_class == "observed issue":
        finding.confidence = "high for this observed exchange; broader exposure unverified"
    elif evidence_class == "review candidate":
        finding.confidence = "review required; exploitability not established"
    report.findings.append(finding)


def _requirements(report: SecurityReport, service: str, endpoint: dict[str, Any],
                  document: dict[str, Any], operation: dict[str, Any], location: str) -> bool:
    policy = endpoint.get("security_metadata") or {}
    requirements = policy.get("security", operation.get("security", document.get("security")))
    protected = False
    if requirements is None:
        _add(report, endpoint, service, "AUTH001", "Authentication requirements undocumented",
             "API2", location + "/security", "No operation or inherited security declaration.",
             "The contract does not describe whether authentication is required.",
             "Declare security requirements or explicitly document anonymous access.")
        return False
    if not isinstance(requirements, list) or any(not isinstance(r, dict) for r in requirements):
        _add(report, endpoint, service, "AUTH002", "Invalid security requirement structure",
             "API2", location + "/security", "Expected an array of requirement objects.",
             "Security requirements cannot be interpreted reliably.",
             "Use OpenAPI security requirement objects; entries are OR, schemes within an entry are AND.",
             "medium")
        return False
    anonymous = not requirements or any(not entry for entry in requirements)
    protected = not anonymous
    if anonymous and policy.get("require_authentication") is True:
        _add(report, endpoint, service, "AUTH003", "Anonymous alternative conflicts with policy",
             "API2", location + "/security", "Anonymous access is explicitly allowed by the contract.",
             "The authored authentication expectation conflicts with an anonymous alternative.",
             "Correct the contract or the explicitly authored endpoint expectation.", "medium")
    components = document.get("components", {})
    schemes = components.get("securitySchemes", {}) if isinstance(components, dict) else {}
    if not isinstance(schemes, dict):
        schemes = {}
    for entry in requirements:
        for name, scopes in entry.items():
            if name not in schemes:
                _add(report, endpoint, service, "AUTH004", "Undefined security scheme", "API2",
                     location + "/security", f"Undefined scheme: {name}",
                     "A required scheme is absent from components.securitySchemes.",
                     "Define the referenced scheme.", "medium")
                continue
            scheme = _resolve(document, schemes[name])
            kind = scheme.get("type")
            valid = kind in {"apiKey", "http", "oauth2", "openIdConnect", "mutualTLS"}
            if kind == "mutualTLS" and str(document.get("openapi", "")).startswith("3.0."):
                valid = False
            if kind == "apiKey":
                valid = valid and bool(scheme.get("name")) and scheme.get("in") in {"query", "header", "cookie"}
                if scheme.get("in") == "query":
                    _add(report, endpoint, service, "AUTH005", "API key placed in query string",
                         "API2", location + "/security", f"Scheme {name} uses query placement.",
                         "URL credentials can be recorded in logs, history, and intermediaries.",
                         "Use a header or an appropriate authentication mechanism.", "medium")
            if kind == "http":
                valid = valid and bool(scheme.get("scheme"))
            if kind == "openIdConnect":
                valid = valid and bool(scheme.get("openIdConnectUrl"))
            if kind == "oauth2":
                flows = scheme.get("flows")
                valid = valid and isinstance(flows, dict) and bool(flows)
                allowed: set[str] = set()
                if isinstance(flows, dict):
                    for flow_name, raw_flow in flows.items():
                        flow = raw_flow if isinstance(raw_flow, dict) else {}
                        valid = valid and flow_name in {
                            "implicit", "password", "clientCredentials", "authorizationCode",
                        } and isinstance(flow.get("scopes"), dict)
                        if flow_name in {"implicit", "authorizationCode"}:
                            valid = valid and bool(flow.get("authorizationUrl"))
                        if flow_name in {"password", "clientCredentials", "authorizationCode"}:
                            valid = valid and bool(flow.get("tokenUrl"))
                        if isinstance(flow.get("scopes"), dict):
                            allowed.update(flow["scopes"])
                    if not isinstance(scopes, list) or any(
                        not isinstance(scope, str) or scope not in allowed for scope in scopes
                    ):
                        _add(report, endpoint, service, "AUTH006", "Unknown OAuth scope", "API2",
                             location + "/security", "Required scopes are not declared by the scheme.",
                             "The operation references an undefined permission.",
                             "Declare the scope in the OAuth flow or correct the requirement.", "medium")
            elif not isinstance(scopes, list) or (kind != "openIdConnect" and scopes):
                valid = False
            if not valid:
                _add(report, endpoint, service, "AUTH007", "Incomplete security scheme", "API2",
                     location + "/security", f"Scheme {name} has invalid or missing fields.",
                     "The scheme does not provide a usable authentication description.",
                     "Correct the scheme according to the document's OpenAPI version.", "medium")
    responses = operation.get("responses", {})
    if protected and isinstance(responses, dict) and not {"401", "403"}.issubset(
        {str(code) for code in responses}
    ):
        _add(report, endpoint, service, "AUTH008", "Authentication error responses undocumented",
             "API2", location + "/responses", "401 and/or 403 are not explicitly documented.",
             "Consumers lack explicit authentication/authorization failure documentation.",
             "Document applicable 401/403 responses; explain intentional alternative behaviour.")
    return protected


def _schema_review(report: SecurityReport, service: str, endpoint: dict[str, Any],
                   document: dict[str, Any], operation: dict[str, Any], location: str) -> None:
    missing: list[str] = []
    inspected: set[str] = set()
    count = 0

    def visit(node: Any, pointer: str, depth: int = 0) -> None:
        nonlocal count
        count += 1
        if count > MAX_NODES or depth > 80:
            raise ValueError("Schema traversal limit reached.")
        if not isinstance(node, dict):
            return
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in inspected:
                return
            inspected.add(ref)
            node = _resolve(document, node)
        kind = node.get("type")
        if isinstance(kind, list):
            kind = next((item for item in kind if item != "null"), "")
        bound = {"string": "maxLength", "array": "maxItems", "integer": "maximum", "number": "maximum"}.get(kind)
        if bound and bound not in node and node.get("format") not in {"binary", "byte"} and not node.get("enum"):
            missing.append(pointer + "/" + bound)
        if node.get("format") in {"uri", "url", "uri-reference"}:
            _add(report, endpoint, service, "INPUT002", "URL input needs destination-policy review",
                 "API7", pointer, "A URL-valued input is documented.",
                 "If the server fetches this URL, a destination policy is needed.",
                 "Document whether the server fetches URLs and review allowed destinations.",
                 evidence_class="review candidate",
                 limitation="A URL input is not proof of SSRF or even of server-side fetching.")
        if node.get("format") == "binary" and "x-max-file-size" not in node:
            _add(report, endpoint, service, "INPUT004", "Upload size policy undocumented", "API4",
                 pointer, "Binary input lacks a documented size-policy extension.",
                 "OpenAPI does not provide a universal file-size limit keyword.",
                 "Document the enforced upload limit, for example using x-max-file-size in bytes.",
                 evidence_class="review candidate",
                 limitation="Missing documentation does not prove unrestricted file uploads.")
        for key in ("properties", "$defs"):
            children = node.get(key)
            if isinstance(children, dict):
                for name, child in children.items():
                    visit(child, pointer + "/" + key + "/" + str(name), depth + 1)
        for key in ("items", "additionalProperties"):
            visit(node.get(key), pointer + "/" + key, depth + 1)
        for key in ("allOf", "anyOf", "oneOf"):
            for index, child in enumerate(node.get(key, []) if isinstance(node.get(key), list) else []):
                visit(child, pointer + "/" + key + "/" + str(index), depth + 1)

    parameters = operation.get("parameters", [])
    if isinstance(parameters, list):
        for index, raw in enumerate(parameters):
            parameter = _resolve(document, raw)
            if parameter.get("in") == "query" and _SECRET_NAME.fullmatch(str(parameter.get("name", ""))):
                _add(report, endpoint, service, "INPUT003", "Credential-like query parameter", "API2",
                     location + "/parameters/" + str(index), "A credential-like parameter uses query placement.",
                     "Query values can be recorded in URLs and logs.",
                     "Move credentials to an appropriate authentication mechanism.", "medium")
            visit(parameter.get("schema"), location + "/parameters/" + str(index) + "/schema")
    if operation.get("requestBody"):
        body = _resolve(document, operation["requestBody"])
        content = body.get("content", {})
        if isinstance(content, dict):
            for media_type, media in content.items():
                if isinstance(media, dict):
                    visit(media.get("schema"), location + "/requestBody/content/" + str(media_type) + "/schema")
    if missing:
        _add(report, endpoint, service, "INPUT001", "Input bounds need review", "API4",
             missing[0], f"{len(missing)} documented inputs lack upper bounds. " + "; ".join(missing[:8]),
             "The contract does not describe an upper bound for these inputs.",
             "Document appropriate bounds or record why a bound is not applicable.",
             evidence_class="review candidate",
             limitation="Missing contract bounds do not prove unlimited server-side resource consumption.")


def scan_catalog(document: dict[str, Any], cancel: threading.Event | None = None, *,
                 scope: str = "Entire catalog", endpoint_ids: set[str] | None = None) -> SecurityReport:
    report = _report(document, "catalog", scope)
    examined_contract = False
    contracts: list[str] = []
    try:
        inventory = {"services": [{
            "name": service.get("name"),
            "endpoints": [{
                key: endpoint.get(key) for key in ("id", "method", "path", "security_metadata")
            } for endpoint in service.get("endpoints", [])],
        } for service in document.get("services", [])]}
        for _, _node in _bounded_walk(inventory):
            if cancel is not None and cancel.is_set():
                report.state = "cancelled"
                break
        if report.state != "cancelled":
            for service in document.get("services", []):
                if cancel is not None and cancel.is_set():
                    report.state = "cancelled"
                    break
                name = str(service.get("name", ""))
                metadata = service.get("security_metadata") or {}
                source_label = str(metadata.get("source_file", ""))
                source_prefix = source_label + " " if source_label else ""
                contract = metadata.get("document")
                usable = isinstance(contract, dict) and str(contract.get("openapi", "")).startswith(("3.0.", "3.1."))
                endpoints = [
                    endpoint for endpoint in service.get("endpoints", [])
                    if endpoint_ids is None or endpoint.get("id") in endpoint_ids
                ]
                if not endpoints:
                    continue
                routes: set[tuple[str, str]] = set()
                for endpoint in endpoints:
                    route = (str(endpoint.get("method", "")).upper(), str(endpoint.get("path", "")))
                    if route in routes:
                        _add(report, endpoint, name, "INVENTORY001", "Duplicate operation in inventory",
                             "API9", "catalog/endpoints", "The same method and path occur more than once in this service.",
                             "Duplicate inventory entries can cause ambiguous test and ownership mappings.",
                             "Remove duplicate entries or separate distinct API versions.", "low")
                    routes.add(route)
                if not usable:
                    report.diagnostics.append(safe_text(
                        f"{name}: security contract unavailable or unsupported; import an OpenAPI 3.0/3.1 document."
                    ))
                else:
                    examined_contract = True
                    contracts.append(fingerprint(contract))
                    for pointer, node in _bounded_walk(contract):
                        if node == "[REDACTED]" and any(
                            marker in pointer for marker in ("/example", "/default", "/enum", "/const")
                        ):
                            _add(report, {}, name, "SECRET001", "Credential-like contract sample requires review",
                                 "API8", source_prefix + pointer, "A credential-like sample was sanitized during import.",
                                 "The source contract may include a credential default or example.",
                                 "Use clearly synthetic placeholders in published contracts; review the original source locally.",
                                 "low", "review candidate",
                                 "A sanitized placeholder is not proof that a live credential was exposed.")
                        if isinstance(node, dict) and "$ref" in node:
                            try:
                                _resolve(contract, node)
                            except ValueError as error:
                                report.state = "partial"
                                report.diagnostics.append(safe_text(f"{name} {pointer}: {error}"))
                for endpoint in endpoints:
                    if cancel is not None and cancel.is_set():
                        report.state = "cancelled"
                        break
                    report.endpoints_reviewed += 1
                    path = str(endpoint.get("path", ""))
                    method = str(endpoint.get("method", "")).lower()
                    location = source_prefix + "#/paths/" + path.replace("~", "~0").replace("/", "~1") + "/" + method
                    if not usable:
                        continue
                    try:
                        paths = contract.get("paths", {})
                        path_item = _resolve(contract, paths.get(path, {}))
                        operation = _resolve(contract, path_item.get(method, {}))
                        if not operation:
                            report.state = "partial"
                            report.diagnostics.append(safe_text(
                                f"{name} {method.upper()} {path}: no matching contract operation; reimport the contract."
                            ))
                            continue
                        if operation.get("deprecated") is True:
                            _add(report, endpoint, name, "INVENTORY002", "Deprecated operation needs lifecycle review",
                                 "API9", location + "/deprecated", "The operation is marked deprecated.",
                                 "Deprecated endpoints need an explicit lifecycle and exposure decision.",
                                 "Document the replacement and retirement plan; verify deployed exposure.",
                                 "info", "review candidate",
                                 "Deprecation alone is not a vulnerability.")
                        _requirements(report, name, endpoint, contract, operation, location)
                        combined = dict(operation)
                        shared = path_item.get("parameters", [])
                        local = operation.get("parameters", [])
                        merged: dict[tuple[str, str], Any] = {}
                        for raw in list(shared) + list(local):
                            parameter = _resolve(contract, raw)
                            merged[(str(parameter.get("in")), str(parameter.get("name")))] = raw
                        combined["parameters"] = list(merged.values())
                        _schema_review(report, name, endpoint, contract, combined, location)
                    except (ValueError, TypeError, AttributeError) as error:
                        report.state = "partial"
                        report.diagnostics.append(safe_text(f"{name} {location}: {type(error).__name__}; contract structure needs review."))
                if usable:
                    servers = contract.get("servers", [])
                    schemes = (contract.get("components") or {}).get("securitySchemes", {})
                    urls = [
                        (f"#/servers/{index}/url", item.get("url", ""))
                        for index, item in enumerate(servers) if isinstance(item, dict)
                    ]
                    for pointer, node in _bounded_walk(contract):
                        if isinstance(node, dict) and "/paths/" in pointer and isinstance(node.get("servers"), list):
                            urls.extend(
                                (pointer + f"/servers/{index}/url", item.get("url", ""))
                                for index, item in enumerate(node["servers"]) if isinstance(item, dict)
                            )
                    for pointer, node in _bounded_walk(schemes, "#/components/securitySchemes"):
                        if isinstance(node, dict):
                            urls.extend((pointer + "/" + key, node[key]) for key in (
                                "authorizationUrl", "tokenUrl", "refreshUrl", "openIdConnectUrl",
                            ) if key in node)
                    for pointer, url in urls:
                        if isinstance(url, str) and urlsplit(url).scheme == "http" and not _local(url):
                            _add(report, {}, name, "TRANSPORT001", "Non-HTTPS contract URL", "API8",
                                 source_prefix + pointer, "A non-local absolute URL uses HTTP.",
                                 "HTTP does not protect credentials and content in transit.",
                                 "Use HTTPS; explicitly review trusted development exceptions.", "medium")
    except (ValueError, TypeError, AttributeError, RecursionError):
        report.state = "partial"
        report.diagnostics.append("Review stopped: invalid structure or traversal/finding limit. Narrow the scope or repair the input.")
    partial_categories = {"API2", "API4", "API7", "API8"} if examined_contract else set()
    if report.endpoints_reviewed:
        partial_categories.add("API9")
    report.coverage = [
        Coverage(f"{key}:2023 - {title}",
                 "partial" if key in partial_categories else "not assessed",
                 "Contract/inventory evidence only; deployed inventory and runtime enforcement are not tested."
                 if key in partial_categories else
                 "Requires additional contract, deployment, identity, or business-flow evidence.")
        for key, title in CATEGORIES.items()
    ]
    if not document.get("services"):
        report.diagnostics.append("Catalog has no services; no endpoints were reviewed.")
    elif not report.endpoints_reviewed and report.state == "complete":
        report.diagnostics.append("No endpoints match the selected scope.")
    report.source_fingerprint = fingerprint(sorted(contracts)) if contracts else ""
    return report


def scan_response(document: dict[str, Any], endpoint: Endpoint, result: ApiResult,
                  cancel: threading.Event | None = None, *, environment: str = "") -> SecurityReport:
    report = _report(document, "passive response", f"{endpoint.method} {endpoint.path}")
    report.environment = safe_text(environment)
    report.source_fingerprint = fingerprint({
        "endpoint": endpoint.id, "status": result.status_code,
        "body": hashlib.sha256(result.content).hexdigest(),
        "response_headers": fingerprint({key.lower(): value for key, value in result.response_headers.items()}),
        "context": result.security_context,
    })
    report.endpoints_reviewed = 1
    headers = {key.lower(): value for key, value in result.response_headers.items()}
    ep = asdict(endpoint)

    def observed(rule: str, title: str, category: str, evidence: str,
                 explanation: str, remediation: str, severity: str = "medium") -> None:
        _add(report, ep, endpoint.service, rule, title, category, "HTTP exchange",
             evidence, explanation, remediation, severity, "observed issue",
             "Applies only to this exchange; no additional requests were sent.")

    if cancel is not None and cancel.is_set():
        report.state = "cancelled"
    else:
        context = result.security_context
        if context.get("verify_ssl") is False and urlsplit(result.url).scheme == "https":
            observed("HTTP001", "Certificate verification disabled", "API8",
                     "The client disabled HTTPS certificate validation.",
                     "This request did not authenticate the server certificate.",
                     "Trust the service certificate and enable verification.")
        if context.get("credential_present") and urlsplit(result.url).scheme == "http":
            observed("HTTP002", "Credentials sent over HTTP", "API2",
                     "Credential presence was recorded before redaction; the URL uses HTTP.",
                     "Credentials were sent without transport encryption.",
                     "Use HTTPS; do not send credentials over unencrypted connections.")
        if context.get("secret_reflected"):
            observed("HTTP009", "Known request credential reflected in response", "API8",
                     "An exact known credential matched response content before redaction; the value was discarded.",
                     "This exchange returned a credential supplied by the client.",
                     "Remove credential values from response bodies and headers; investigate exposure.", "high")
        body = result.response_body[:BODY_LIMIT]
        if len(result.response_body) > BODY_LIMIT:
            report.state = "partial"
            report.diagnostics.append("Body analysis limited to the first 256 KiB.")
        if re.search(r"Traceback \(most recent call last\)|"
                     r"\bat [\w.]+\([^)]*\) in .+:line \d+|"
                     r"Microsoft\.AspNetCore\.\w+Exception|"
                     r"java\.[\w.]+Exception[\s:]", body):
            observed("HTTP003", "Internal exception details in response", "API8",
                     "An internal stack-trace/exception signature was found (body values omitted).",
                     "Internal implementation details appear in the returned body.",
                     "Return a sanitized error contract and retain diagnostic details in server logs.")
        if result.status_code and result.content and not headers.get("content-type"):
            observed("HTTP004", "Response content type absent", "API8", "No Content-Type header.",
                     "The client cannot reliably identify the response representation.",
                     "Set the appropriate response Content-Type.", "low")
        if headers.get("access-control-allow-origin") == "*" and headers.get("access-control-allow-credentials", "").lower() == "true":
            observed("HTTP005", "Incompatible credentialed CORS configuration", "API8",
                     "Wildcard origin with Access-Control-Allow-Credentials: true.",
                     "Browsers reject this combination for credentialed access; it is not proof of data access.",
                     "Use an explicit approved origin policy.", "low")
        service = next((item for item in document.get("services", []) if item.get("name") == endpoint.service), {})
        contract = (service.get("security_metadata") or {}).get("document", {})
        if isinstance(contract, dict) and str(contract.get("openapi", "")).startswith(("3.0.", "3.1.")):
            try:
                path_item = _resolve(contract, contract.get("paths", {}).get(endpoint.path, {}))
                operation = _resolve(contract, path_item.get(endpoint.method.lower(), {}))
                responses = operation.get("responses", {})
                raw_response = responses.get(str(result.status_code), responses.get(
                    str(result.status_code // 100) + "XX", responses.get("default", {}),
                ))
                expected_content = _resolve(contract, raw_response).get("content", {})
                actual_type = result.content_type.split(";", 1)[0].strip().lower()
                if result.content and actual_type and expected_content and not any(
                    fnmatch.fnmatchcase(actual_type, str(media).lower()) for media in expected_content
                ):
                    observed("HTTP008", "Response content type differs from contract", "API8",
                             "Actual media type: " + actual_type + "; documented types: " + ", ".join(expected_content),
                             "This exchange returned a representation not documented for its status.",
                             "Correct the response or update the contract.", "low")
            except (ValueError, TypeError, AttributeError):
                report.state = "partial"
                report.diagnostics.append("Response contract comparison unavailable: invalid or unresolved contract.")
        sensitive = endpoint.security_metadata.get("sensitive_response_fields", [])
        if isinstance(sensitive, list) and sensitive and "json" in result.content_type.lower():
            try:
                decoded = json.loads(body)
                paths = [pointer for pointer, node in _bounded_walk(decoded)
                         if pointer.rsplit("/", 1)[-1] in sensitive and node not in (None, "", "[REDACTED]")]
                if paths:
                    observed("HTTP006", "Annotated sensitive response field present", "API3",
                             "Field paths only: " + "; ".join(paths[:10]),
                             "Fields explicitly annotated as sensitive were returned.",
                             "Verify whether this identity may see these fields; restrict the response if not.")
                    cache = headers.get("cache-control", "").lower().split(",")
                    if not any(value.strip().split("=", 1)[0] == "no-store" for value in cache):
                        observed("HTTP007", "Sensitive response lacks no-store caching policy", "API8",
                                 "Annotated sensitive fields present; Cache-Control does not contain no-store.",
                                 "Sensitive content may be stored by caches.",
                                 "Apply the appropriate sensitive-response caching policy.", "low")
            except (ValueError, RecursionError):
                report.state = "partial"
                report.diagnostics.append("Sensitive-field analysis unavailable: JSON invalid, truncated, or too deeply nested.")
    related = scan_catalog(document, cancel, scope=report.scope, endpoint_ids={endpoint.id})
    report.findings.extend(related.findings)
    report.diagnostics.extend(related.diagnostics)
    if related.state != "complete":
        report.state = related.state
    report.diagnostics.append("Related contract findings are included; contract and observed evidence remain distinct.")
    report.coverage = [
        Coverage(f"{key}:2023 - {title}",
                 "partial" if key in {"API2", "API3", "API8"} and report.state != "cancelled" else "not assessed",
                 "Limited passive exchange observations; authorization and enforcement remain unverified.")
        for key, title in CATEGORIES.items()
    ]
    return report


def report_html(report: SecurityReport) -> str:
    escape = lambda value: html.escape(str(value), quote=True)
    rows = "".join(
        "<article><h2>" + escape(f"{item.severity.upper()} - {item.rule_id}: {item.title}") +
        "</h2><p>" + escape(f"{item.service} {item.method} {item.path} | {item.category} | {item.evidence_class}") +
        "</p>" + "".join("<p><b>" + escape(name) + ":</b> " + escape(value) + "</p>"
                         for name, value in (
                             ("Location", item.location), ("Evidence", item.evidence),
                             ("Explanation", item.explanation), ("Remediation", item.remediation),
                             ("Limitation", item.limitation), ("Disposition", item.disposition),
                             ("Reason", item.reason), ("Confidence", item.confidence),
                             ("Disposition expiry", item.expires_at),
                         )) + "</article>"
        for item in report.findings
    )
    coverage = "".join("<li>" + escape(f"{item.category}: {item.status}. {item.explanation}") + "</li>"
                       for item in report.coverage)
    active = report.mode in {"controlled verification", "active verification"}
    return (
        '<!doctype html><html><head><meta charset="utf-8"><title>API Security Review</title>'
        '<style>body{font-family:system-ui;max-width:1100px;margin:2em auto;padding:1em}'
        'article{border-top:1px solid #aaa;padding:1em 0}p{overflow-wrap:anywhere}</style>'
        '</head><body><h1>API Security Review</h1><p>Not a security certification or OWASP compliance verdict. ' +
        ('Approved bounded verification requests were sent; results cover only authored assertions.</p><p>'
         if active else 'No additional API requests were sent.</p><p>') +
        escape(f"{report.mode} | {report.scope} | {report.state} | {report.created_at} | {report.environment}") +
        "</p><p>" + escape(f"{report.endpoints_reviewed} endpoints; {len(report.findings)} findings. "
                             f"Catalog: {report.catalog_fingerprint}; source: {report.source_fingerprint}; rules: {report.rule_version}") +
        "</p><h2>Diagnostics</h2><ul>" +
        "".join("<li>" + escape(item) + "</li>" for item in report.diagnostics) +
        "</ul><h2>OWASP coverage</h2><ul>" + coverage + "</ul>" + rows + "</body></html>"
    )


def report_csv(report: SecurityReport) -> str:
    stream = io.StringIO(newline="")
    fields = list(Finding.__dataclass_fields__)
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for finding in report.findings:
        # Prevent spreadsheet formula execution from untrusted contract text.
        writer.writerow({key: ("'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value)
                         for key, value in asdict(finding).items()})
    return stream.getvalue()
