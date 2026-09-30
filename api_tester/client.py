from __future__ import annotations

import json
import re
import socket
import ssl
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import requests

from . import content as content_module
from .auth_strategies import RequestContext
from .catalog import Endpoint
from .request_auth import AuthenticationContext, SECRET_HEADER_NAMES
from .workspace_store import WorkspaceStore, as_store


def parameter_enabled_key(source: str, name: str) -> str:
    return f"enabled:{source}:{name}"


def parameter_is_enabled(
    values: dict[str, str], source: str, name: str
) -> bool:
    if source == "path":
        return True
    return values.get(parameter_enabled_key(source, name), "true").lower() != "false"


@dataclass(frozen=True)
class ApiResult:
    passed: bool
    status_code: int
    elapsed_ms: int
    url: str
    response_headers: dict[str, str]
    response_body: str
    error: str = ""
    content: bytes = b""
    content_type: str = ""
    content_disposition: str = ""
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: str = ""
    reason: str = ""
    timings: dict[str, float | str] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.content)


@dataclass(frozen=True)
class PreparedEndpointRequest:
    method: str
    url: str
    query: dict[str, str]
    headers: dict[str, str]
    form: dict[str, str]
    files: dict[str, str]
    json_body: Any

    @property
    def resolved_url(self) -> str:
        return requests.Request(self.method, self.url, params=self.query).prepare().url or self.url


#: Choices offered wherever an expected status is picked. Any value
#: status_matches accepts (a range "200-299" or a list "200,204") may also be
#: typed, so this only needs the common cases.
EXPECTED_STATUS_CHOICES = (
    "200-299", "200", "201", "202", "204",
    "400", "401", "403", "404", "409", "500",
)


def status_matches(actual: int, expected: str) -> bool:
    expected = expected.strip()
    range_match = re.fullmatch(r"(\d{3})-(\d{3})", expected)
    if range_match:
        return int(range_match.group(1)) <= actual <= int(range_match.group(2))
    return actual in {int(item.strip()) for item in expected.split(",") if item.strip().isdigit()}


def prepare_endpoint_request(
    endpoint: Endpoint,
    base_url: str,
    access_token: str,
    api_key: str,
    values: dict[str, str],
    payload: Any,
    custom_headers: dict[str, str] | None = None,
    *,
    omitted_headers: frozenset[str] = frozenset(),
    provided_headers: frozenset[str] = frozenset(),
    provided_query: frozenset[str] = frozenset(),
) -> PreparedEndpointRequest:
    path = endpoint.path
    query: dict[str, str] = {}
    form: dict[str, str] = {}
    files: dict[str, str] = {}
    headers = {"Accept": "application/json"}
    if access_token.strip():
        token = access_token.strip()
        headers["Authorization"] = (
            token if token.lower().startswith("bearer ") else f"Bearer {token}"
        )
    if api_key.strip():
        headers["x-api-key"] = api_key.strip()
    headers.update(custom_headers or {})

    for parameter in endpoint.parameters:
        if parameter.source == "header" and parameter.name.lower() in omitted_headers:
            continue
        if not parameter_is_enabled(values, parameter.source, parameter.name):
            continue
        value = values.get(f"{parameter.source}:{parameter.name}", "").strip()
        if not value:
            if parameter.source == "header" and parameter.name.lower() in provided_headers:
                continue
            if parameter.source == "query" and parameter.name in provided_query:
                continue
            if parameter.required or parameter.source == "path":
                raise ValueError(
                    f"Required {parameter.source} parameter is missing: {parameter.name}"
                )
            continue
        if parameter.source == "path":
            path = re.sub(
                r"\{" + re.escape(parameter.name) + r"(?::[^}]+)?\}",
                quote(value, safe=""),
                path,
                flags=re.IGNORECASE,
            )
        elif parameter.source == "query":
            query[parameter.name] = value
        elif parameter.source == "header":
            headers[parameter.name] = value
        elif parameter.source == "form":
            target = files if "file" in parameter.type.lower() else form
            target[parameter.name] = value

    missing_files = [
        f"{name}: {path}" for name, path in files.items() if not Path(path).expanduser().is_file()
    ]
    if missing_files:
        raise ValueError("Form file does not exist: " + ", ".join(missing_files))

    unresolved = re.findall(r"\{([^}:]+)(?::[^}]+)?\}", path)
    if unresolved:
        raise ValueError(f"Required path values are missing: {', '.join(unresolved)}")
    json_body = (
        payload
        if payload is not None
        and endpoint.method not in {"GET", "HEAD"}
        and not form
        and not files
        else None
    )
    return PreparedEndpointRequest(
        method=endpoint.method,
        url=f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        query=query,
        headers=headers,
        form=form,
        files=files,
        json_body=json_body,
    )


def redact_headers(
    headers: dict[str, str], secret_names: frozenset[str] = SECRET_HEADER_NAMES,
) -> dict[str, str]:
    return {
        name: "******" if name.lower() in secret_names and value else value
        for name, value in headers.items()
    }


def generate_curl(request: PreparedEndpointRequest, redact_secrets: bool = True) -> str:
    headers = redact_headers(request.headers) if redact_secrets else request.headers
    parts = ["curl", "-X", request.method, f'"{request.resolved_url}"']
    for name, value in headers.items():
        parts.extend(["-H", f'"{name}: {value}"'])
    for name, value in request.form.items():
        parts.extend(["-F", f'"{name}={value}"'])
    for name, path in request.files.items():
        parts.extend(["-F", f'"{name}=@{path}"'])
    if request.json_body is not None:
        parts.extend(
            [
                "-H",
                '"Content-Type: application/json"',
                "--data-raw",
                "'" + json.dumps(request.json_body, separators=(",", ":")) + "'",
            ]
        )
    return " ".join(parts)


def generate_python(request: PreparedEndpointRequest) -> str:
    headers = redact_headers(request.headers)
    lines = [
        "import requests",
        "",
        f"url = {request.resolved_url!r}",
        f"headers = {headers!r}",
    ]
    arguments = ["method=" + repr(request.method), "url=url", "headers=headers"]
    if request.form:
        lines.append(f"data = {request.form!r}")
        arguments.append("data=data")
    if request.json_body is not None:
        lines.append(f"payload = {request.json_body!r}")
        arguments.append("json=payload")
    if request.files:
        lines.append(f"file_paths = {request.files!r}")
        lines.append(
            "files = {name: open(path, 'rb') for name, path in file_paths.items()}"
        )
        arguments.append("files=files")
    lines.extend(
        [
            "",
            f"response = requests.request({', '.join(arguments)})",
            "response.raise_for_status()",
        ]
    )
    return "\n".join(lines)


def measure_connection(url: str, verify_ssl: bool, timeout: float) -> dict[str, float | str]:
    """Measures DNS/TCP/TLS with a diagnostic connection before the HTTP request."""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return {"error": "URL has no host"}
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    timings: dict[str, float | str] = {}
    try:
        started = perf_counter()
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        timings["dns_ms"] = round((perf_counter() - started) * 1000, 2)
        family, socket_type, protocol, _, address = addresses[0]
        started = perf_counter()
        connection = socket.socket(family, socket_type, protocol)
        connection.settimeout(min(timeout, 5))
        connection.connect(address)
        timings["tcp_ms"] = round((perf_counter() - started) * 1000, 2)
        try:
            if parsed.scheme == "https":
                context = (
                    ssl.create_default_context()
                    if verify_ssl
                    else ssl._create_unverified_context()
                )
                started = perf_counter()
                tls_socket = context.wrap_socket(connection, server_hostname=host)
                timings["tls_ms"] = round((perf_counter() - started) * 1000, 2)
                tls_socket.close()
            else:
                timings["tls_ms"] = 0.0
        finally:
            connection.close()
    except (OSError, ssl.SSLError) as exc:
        timings["probe_error"] = str(exc)
    return timings


class _ExplicitAuthentication(requests.auth.AuthBase):
    """Occupies the ``auth`` slot so ``requests`` cannot fall back to ``.netrc``.

    A challenge-based method (Digest) supplies its own callable, which this
    wrapper runs after the no-op guard so the same slot serves both purposes.
    """

    def __init__(self, inner: requests.auth.AuthBase | None = None) -> None:
        self._inner = inner

    def __call__(self, request):
        # Prevent requests from replacing configured/no-auth headers with .netrc credentials.
        if self._inner is not None:
            return self._inner(request)
        return request


def _request_context(prepared: PreparedEndpointRequest) -> RequestContext:
    """Describes the outgoing request for the methods that sign it.

    The body is serialized exactly as ``requests`` will send it, because a
    signature computed over a re-serialized approximation would not match.
    """
    body = b""
    content_type = prepared.headers.get("Content-Type", "") or prepared.headers.get(
        "content-type", ""
    )
    form: tuple[tuple[str, str], ...] = ()
    if prepared.json_body is not None:
        body = json.dumps(prepared.json_body).encode("utf-8")
        content_type = content_type or "application/json"
    elif prepared.form:
        form = tuple((str(k), str(v)) for k, v in prepared.form.items())
        body = urlencode(prepared.form).encode("utf-8")
        content_type = content_type or "application/x-www-form-urlencoded"
    return RequestContext(
        method=prepared.method.upper(),
        url=prepared.resolved_url,
        headers=dict(prepared.headers),
        body=body,
        form_params=form,
        content_type=content_type,
    )


def _invalid_token_challenge(challenge: str) -> bool:
    parts = challenge.strip().split(None, 1)
    return (
        len(parts) == 2 and parts[0].lower() == "bearer"
        and requests.utils.parse_dict_header(parts[1]).get("error") == "invalid_token"
    )


def execute_endpoint(
    endpoint: Endpoint,
    base_url: str,
    access_token: str,
    api_key: str,
    values: dict[str, str],
    payload: Any,
    expected_status: str,
    timeout: float = 30,
    verify_ssl: bool = True,
    custom_headers: dict[str, str] | None = None,
    *,
    auth_context: AuthenticationContext | None = None,
    auth_mode: str = "inherit",
    session: requests.Session | None = None,
    probe_connection: bool = True,
) -> ApiResult:
    execution_started = perf_counter()
    if auth_context is None and auth_mode != "inherit":
        auth_context = AuthenticationContext("", {}, {}, {endpoint.service: base_url})
    omitted = (
        auth_context.secret_names
        if auth_context is not None and auth_context.is_disabled(endpoint.service, auth_mode)
        else frozenset()
    )
    prepared = prepare_endpoint_request(
        endpoint, base_url, access_token, api_key, values, payload, custom_headers,
        omitted_headers=omitted,
        provided_headers=auth_context.managed_header_names(endpoint.service, auth_mode) if auth_context else frozenset(),
        provided_query=auth_context.managed_query_names(endpoint.service, auth_mode) if auth_context else frozenset(),
    )
    auth_ms = 0.0
    first_attempt_ms = 0
    secrets_seen: tuple[str, ...] = ()
    for attempt in range(2):
        started = perf_counter()
        applied = (
            auth_context.apply(endpoint.service, prepared.url, prepared.headers, auth_mode,
                               force_refresh=attempt == 1,
                               context=_request_context(prepared))
            if auth_context is not None else None
        )
        auth_ms += (perf_counter() - started) * 1000 if applied and applied.managed else 0
        if applied:
            secrets_seen += applied.secrets
            applied = replace(applied, secrets=secrets_seen)
        outgoing = (
            replace(prepared, headers=applied.headers,
                    query={**prepared.query, **dict(applied.query)})
            if applied else prepared
        )
        try:
            result = _execute_endpoint(
                endpoint, base_url, access_token, api_key, values, payload, expected_status,
                timeout, verify_ssl, custom_headers, _prepared=outgoing,
                managed=bool(applied and applied.managed),
                no_auth=bool(auth_context and auth_context.is_disabled(endpoint.service, auth_mode)),
                execution_started=execution_started if applied and applied.managed else None,
                session=session,
                probe_connection=probe_connection,
                request_auth=applied.request_auth if applied else None,
            )
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            if applied and applied.managed:
                raise RuntimeError(applied.redact(str(exc))) from None
            raise
        challenge = next(
            (value for name, value in result.response_headers.items()
             if name.lower() == "www-authenticate"), "",
        )
        retry = (
            attempt == 0 and applied is not None and applied.renewable
            and result.status_code == 401 and not status_matches(401, expected_status)
            and endpoint.method.upper() in {"GET", "HEAD"}
            and not prepared.files and not prepared.form and prepared.json_body is None
            and _invalid_token_challenge(challenge)
        )
        if retry:
            first_attempt_ms = result.elapsed_ms
            continue
        if applied:
            response_headers = redact_headers(result.response_headers, applied.secret_names)
            body = applied.redact(result.response_body)
            result = replace(
                result,
                request_headers={
                    name: applied.redact(value)
                    for name, value in redact_headers(result.request_headers, applied.secret_names).items()
                },
                request_body=applied.redact(result.request_body),
                url=applied.redact(result.url),
                response_headers={name: applied.redact(value) for name, value in response_headers.items()},
                response_body=body,
                reason=applied.redact(result.reason),
                content_disposition=applied.redact(result.content_disposition),
                content=body.encode("utf-8") if body != result.response_body else result.content,
            )
        if applied and applied.managed:
            result = replace(result, timings={
                **result.timings, "auth_ms": round(auth_ms, 2),
                "auth_retry_count": attempt, "initial_http_ms": first_attempt_ms,
            })
        return result
    raise RuntimeError("Authentication retry limit exceeded.")


def _execute_endpoint(
    endpoint: Endpoint,
    base_url: str,
    access_token: str,
    api_key: str,
    values: dict[str, str],
    payload: Any,
    expected_status: str,
    timeout: float = 30,
    verify_ssl: bool = True,
    custom_headers: dict[str, str] | None = None,
    *,
    _prepared: PreparedEndpointRequest | None = None,
    managed: bool = False,
    no_auth: bool = False,
    execution_started: float | None = None,
    session: requests.Session | None = None,
    probe_connection: bool = True,
    request_auth: requests.auth.AuthBase | None = None,
) -> ApiResult:
    prepared = _prepared or prepare_endpoint_request(
        endpoint,
        base_url,
        access_token,
        api_key,
        values,
        payload,
        custom_headers,
    )
    path = endpoint.path
    query: dict[str, str] = {}
    form: dict[str, str] = {}
    file_values: dict[str, str] = {}
    headers = {"Accept": "application/json"}
    if access_token.strip():
        token = access_token.strip()
        headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
    if api_key.strip():
        headers["x-api-key"] = api_key.strip()

    for parameter in endpoint.parameters:
        value = values.get(f"{parameter.source}:{parameter.name}", "").strip()
        if not value:
            continue
        if parameter.source == "path":
            path = re.sub(
                r"\{" + re.escape(parameter.name) + r"(?::[^}]+)?\}",
                quote(value, safe=""),
                path,
                flags=re.IGNORECASE,
            )
        elif parameter.source == "query":
            query[parameter.name] = value
        elif parameter.source == "header":
            headers[parameter.name] = value
        elif parameter.source == "form":
            if "file" in parameter.type.lower():
                file_values[parameter.name] = value
            else:
                form[parameter.name] = value

    unresolved = re.findall(r"\{([^}:]+)(?::[^}]+)?\}", path)
    if unresolved:
        raise ValueError(f"Required path values are missing: {', '.join(unresolved)}")

    # Execution and generated artifacts share the same final request model.
    url = prepared.url
    query = prepared.query
    headers = prepared.headers
    form = prepared.form
    file_values = prepared.files
    timings = measure_connection(url, verify_ssl, timeout) if probe_connection else {}
    started = perf_counter()
    if execution_started is not None:
        timings["request_offset_ms"] = (started - execution_started) * 1000
    with ExitStack() as stack:
        files = {
            name: stack.enter_context(Path(file_path).expanduser().open("rb"))
            for name, file_path in file_values.items()
        }
        try:
            requester = session.request if session is not None else requests.request
            response = requester(
                endpoint.method,
                url,
                params=query,
                headers=headers,
                data=form or None,
                files=files or None,
                json=prepared.json_body,
                timeout=timeout,
                verify=verify_ssl,
                **({"allow_redirects": False,
                    "auth": _ExplicitAuthentication(request_auth)} if managed or no_auth else {}),
            )
        except requests.exceptions.SSLError as exc:
            raise RuntimeError(
                "SSL certificate verification failed. For a trusted environment, install or "
                "trust the service certificate. For a local self-signed API, uncheck "
                "'Verify SSL certificates (uncheck for local APIs)' in the Environment section "
                "and retry."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise RuntimeError(
                f"Request timed out after {timeout:g} seconds. Increase Request timeout "
                "in Edit Environment and retry."
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise RuntimeError(
                f"Could not connect to {url}. Check the selected environment, service "
                "base URL, and whether the local API is running."
            ) from exc
    elapsed_ms = round((perf_counter() - started) * 1000)
    timings["http_ms"] = elapsed_ms
    content_type = response.headers.get("content-type", "")
    raw = response.content or b""
    info = content_module.inspect_body(
        raw,
        content_type,
        response.headers.get("content-disposition", ""),
        response.url,
    )
    # Assertions and captures operate on text, so binary bodies get a placeholder.
    body = info.text if not info.is_binary else f"<{info.kind} body, {len(raw)} bytes>"
    return ApiResult(
        passed=status_matches(response.status_code, expected_status),
        status_code=response.status_code,
        elapsed_ms=elapsed_ms,
        url=response.url,
        response_headers=dict(response.headers),
        response_body=body,
        content=raw,
        content_type=content_type,
        content_disposition=response.headers.get("content-disposition", ""),
        request_headers=redact_headers(dict(response.request.headers)),
        request_body=_request_body_text(response.request.body),
        reason=response.reason or "",
        timings=timings,
    )


def _request_body_text(body: object) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", errors="replace")
    if isinstance(body, str):
        return body
    return f"<{type(body).__name__} stream>"


def append_history(
    target: "WorkspaceStore | Path | str",
    endpoint: Endpoint,
    result: ApiResult,
    environment: str = "",
) -> None:
    """Records one request in the workspace database."""
    as_store(target).append_run_history(
        endpoint.id,
        passed=result.passed,
        status_code=result.status_code,
        elapsed_ms=result.elapsed_ms,
        url=result.url,
        environment=environment,
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
    )
