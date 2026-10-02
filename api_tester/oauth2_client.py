"""Generic OAuth 2.0 token acquisition for non-Microsoft identity providers.

MSAL stays responsible for Entra ID and B2C because it carries broker/WAM
support and Conditional Access handling that a hand-rolled client cannot
replicate. Everything else -- Auth0, Okta, Keycloak, Ping, a bespoke server --
goes through this module, which speaks plain RFC 6749/7636/8628.

Nothing here touches the UI or the settings store: callers pass the
configuration in and receive a token back, which keeps the flows unit-testable
without a live provider.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Optional

import requests

from . import auth_log

__all__ = [
    "OAuth2Config",
    "OAuth2Error",
    "OAuth2InteractionRequired",
    "TokenResponse",
    "build_authorization_url",
    "exchange_authorization_code",
    "generate_pkce_pair",
    "request_client_credentials",
    "request_device_code",
    "poll_device_token",
    "request_password_token",
    "refresh_access_token",
    "run_authorization_code_flow",
]

# Providers occasionally return an expiry so short that the token is useless by
# the time the request goes out; treat anything under this as already expired.
_EXPIRY_SKEW_SECONDS = 30
_DEFAULT_TIMEOUT_SECONDS = 30
_DEVICE_POLL_FLOOR_SECONDS = 1.0


class OAuth2Error(RuntimeError):
    """A provider-reported or transport-level failure during a token request."""


class OAuth2InteractionRequired(OAuth2Error):
    """No token can be produced without the user completing a sign-in."""


@dataclass(frozen=True)
class OAuth2Config:
    """The subset of profile settings the flows actually need."""

    client_id: str
    token_url: str
    authorization_url: str = ""
    device_authorization_url: str = ""
    client_secret: str = ""
    scopes: tuple[str, ...] = ()
    audience: str = ""
    redirect_uri: str = "http://localhost"
    client_auth: str = "basic"
    login_timeout: int = 120
    extra_params: dict[str, str] = field(default_factory=dict)

    @property
    def scope_value(self) -> str:
        return " ".join(s for s in self.scopes if s)


@dataclass(frozen=True)
class TokenResponse:
    """A normalised token payload with an absolute expiry."""

    access_token: str
    token_type: str = "Bearer"
    refresh_token: str = ""
    expires_at: float = 0.0
    scope: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, *, now: Optional[float] = None) -> bool:
        if not self.expires_at:
            return False
        current = time.time() if now is None else now
        return current >= self.expires_at - _EXPIRY_SKEW_SECONDS


def generate_pkce_pair(
    *, verifier_factory: Callable[[], str] = lambda: secrets.token_urlsafe(64)
) -> tuple[str, str]:
    """Returns an RFC 7636 ``(verifier, challenge)`` pair using S256."""
    verifier = verifier_factory()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _client_auth_parts(
    config: OAuth2Config,
) -> tuple[dict[str, str], dict[str, str]]:
    """Splits client credentials into header and body parts.

    RFC 6749 permits either, and providers disagree about which they accept,
    so the choice is explicit configuration rather than a guess.
    """
    if not config.client_secret:
        # A public client identifies itself in the body; there is no secret to
        # put in a header.
        return {}, {"client_id": config.client_id}
    if config.client_auth == "body":
        return {}, {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        }
    raw = f"{urllib.parse.quote(config.client_id)}:{urllib.parse.quote(config.client_secret)}"
    encoded = base64.b64encode(raw.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {encoded}"}, {}


def _post_token(
    config: OAuth2Config,
    data: dict[str, str],
    *,
    url: str = "",
    session: Optional[Any] = None,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Posts a token request and returns the decoded payload.

    Provider errors arrive as a JSON body with a non-2xx status, so the status
    alone is not enough to produce a useful message.
    """
    headers, body = _client_auth_parts(config)
    payload = {**body, **data}
    if config.audience:
        payload.setdefault("audience", config.audience)
    for key, value in config.extra_params.items():
        payload.setdefault(key, value)
    headers["Accept"] = "application/json"
    http = session or requests
    target = url or config.token_url
    started = perf_counter()

    def elapsed() -> float:
        return (perf_counter() - started) * 1000

    try:
        response = http.post(
            target, data=payload, headers=headers, timeout=timeout
        )
    except Exception as exc:
        auth_log.trace(
            auth_log.HTTP, http_method="POST", url=target,
            sent_fields=sorted(payload), detail=f"transport error: {exc}",
            duration_ms=elapsed(), ok=False,
        )
        raise OAuth2Error(f"The token request could not be sent: {exc}") from None
    status = int(getattr(response, "status_code", 0) or 0)
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    grant = str(payload.get("grant_type") or "")
    auth_log.trace(
        auth_log.HTTP, http_method="POST", url=target, status=status,
        sent_fields=sorted(payload), detail=f"grant {grant}" if grant else "",
        duration_ms=elapsed(),
        ok=status < 400 and isinstance(parsed, dict) and not parsed.get("error"),
    )
    if not isinstance(parsed, dict):
        raise OAuth2Error(
            f"The token endpoint returned a non-JSON response (HTTP {status})."
        )
    if parsed.get("error"):
        raise _provider_error(parsed)
    if response.status_code >= 400:
        raise OAuth2Error(f"The token endpoint returned HTTP {response.status_code}.")
    return parsed


def _provider_error(payload: dict[str, Any]) -> OAuth2Error:
    code = str(payload.get("error") or "invalid_request")
    description = str(payload.get("error_description") or "").strip()
    message = f"{code}: {description}" if description else code
    if code in {"interaction_required", "login_required", "consent_required"}:
        return OAuth2InteractionRequired(message)
    return OAuth2Error(message)


def _to_token(payload: dict[str, Any], *, now: Optional[float] = None) -> TokenResponse:
    token = payload.get("access_token")
    if not token:
        raise OAuth2Error("The identity provider did not return an access token.")
    expires_in = payload.get("expires_in")
    issued = time.time() if now is None else now
    expires_at = 0.0
    if isinstance(expires_in, (int, float)):
        expires_at = issued + float(expires_in)
    elif isinstance(expires_in, str) and expires_in.isdigit():
        expires_at = issued + float(expires_in)
    return TokenResponse(
        access_token=str(token),
        token_type=str(payload.get("token_type") or "Bearer"),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_at=expires_at,
        scope=str(payload.get("scope") or ""),
        raw=payload,
    )


# -- individual grants ------------------------------------------------------


def request_client_credentials(
    config: OAuth2Config, *, session: Optional[Any] = None, now: Optional[float] = None
) -> TokenResponse:
    data = {"grant_type": "client_credentials"}
    if config.scope_value:
        data["scope"] = config.scope_value
    return _to_token(_post_token(config, data, session=session), now=now)


def request_password_token(
    config: OAuth2Config,
    username: str,
    password: str,
    *,
    session: Optional[Any] = None,
    now: Optional[float] = None,
) -> TokenResponse:
    data = {"grant_type": "password", "username": username, "password": password}
    if config.scope_value:
        data["scope"] = config.scope_value
    return _to_token(_post_token(config, data, session=session), now=now)


def refresh_access_token(
    config: OAuth2Config,
    refresh_token: str,
    *,
    session: Optional[Any] = None,
    now: Optional[float] = None,
) -> TokenResponse:
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    if config.scope_value:
        data["scope"] = config.scope_value
    payload = _post_token(config, data, session=session)
    token = _to_token(payload, now=now)
    if not token.refresh_token:
        # Providers that do not rotate refresh tokens omit the field; keeping
        # the old one is what allows the next refresh to work.
        token = TokenResponse(
            access_token=token.access_token,
            token_type=token.token_type,
            refresh_token=refresh_token,
            expires_at=token.expires_at,
            scope=token.scope,
            raw=token.raw,
        )
    return token


def build_authorization_url(
    config: OAuth2Config,
    *,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    params = {
        "response_type": "code",
        "client_id": config.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if config.scope_value:
        params["scope"] = config.scope_value
    if config.audience:
        params["audience"] = config.audience
    params.update(config.extra_params)
    separator = "&" if urllib.parse.urlparse(config.authorization_url).query else "?"
    auth_log.trace(
        auth_log.STEP, detail="built authorization URL (PKCE S256)",
        url=config.authorization_url, sent_fields=sorted(params),
    )
    return f"{config.authorization_url}{separator}{urllib.parse.urlencode(params)}"


def exchange_authorization_code(
    config: OAuth2Config,
    code: str,
    *,
    redirect_uri: str,
    code_verifier: str,
    session: Optional[Any] = None,
    now: Optional[float] = None,
) -> TokenResponse:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    return _to_token(_post_token(config, data, session=session), now=now)


def request_device_code(
    config: OAuth2Config, *, session: Optional[Any] = None
) -> dict[str, Any]:
    """Starts the RFC 8628 device flow and returns the provider's instructions."""
    if not config.device_authorization_url:
        raise OAuth2Error("No device authorization URL is configured.")
    data: dict[str, str] = {}
    if config.scope_value:
        data["scope"] = config.scope_value
    payload = _post_token(
        config, data, url=config.device_authorization_url, session=session
    )
    if not payload.get("device_code") or not payload.get("user_code"):
        raise OAuth2Error("The device authorization endpoint returned an incomplete response.")
    auth_log.trace(
        auth_log.STEP,
        detail=f"device code issued; user must visit {payload.get('verification_uri', '')}".strip(),
    )
    return payload


def poll_device_token(
    config: OAuth2Config,
    device_payload: dict[str, Any],
    *,
    session: Optional[Any] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    now: Optional[float] = None,
) -> TokenResponse:
    """Polls until the user approves, honouring ``slow_down`` back-off."""
    interval = float(device_payload.get("interval") or 5)
    expires_in = float(device_payload.get("expires_in") or config.login_timeout or 300)
    deadline = clock() + min(expires_in, float(config.login_timeout or expires_in))
    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": str(device_payload["device_code"]),
    }
    while True:
        if clock() >= deadline:
            raise OAuth2InteractionRequired(
                "The device sign-in was not completed before it expired."
            )
        try:
            return _to_token(_post_token(config, data, session=session), now=now)
        except OAuth2Error as exc:
            message = str(exc)
            if message.startswith("authorization_pending"):
                sleep(max(interval, _DEVICE_POLL_FLOOR_SECONDS))
                continue
            if message.startswith("slow_down"):
                interval += 5
                sleep(interval)
                continue
            raise


# -- loopback redirect listener --------------------------------------------


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Captures the single authorization-code redirect, then stops."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        type(self).result = query
        body = (
            b"<html><body style='font-family:sans-serif;padding:2rem'>"
            b"<h3>Sign-in complete</h3><p>You can close this tab and return to "
            b"the API tester.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        # The default handler writes to stderr, which would leak the code.
        return


def _reserve_loopback(redirect_uri: str) -> tuple[str, int]:
    parsed = urllib.parse.urlparse(redirect_uri or "http://localhost")
    host = parsed.hostname or "localhost"
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise OAuth2Error(
            "The redirect URI must point at loopback so the tester can receive the code."
        )
    port = parsed.port or 0
    if port == 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host if host != "localhost" else "127.0.0.1", 0))
            port = probe.getsockname()[1]
    return host, port


def run_authorization_code_flow(
    config: OAuth2Config,
    *,
    session: Optional[Any] = None,
    open_browser: Callable[[str], Any] = webbrowser.open,
    now: Optional[float] = None,
) -> TokenResponse:
    """Runs auth code + PKCE against a loopback listener and returns the token."""
    if not config.authorization_url:
        raise OAuth2Error("No authorization URL is configured.")
    host, port = _reserve_loopback(config.redirect_uri)
    redirect_uri = f"http://{host}:{port}"
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(24)

    handler = type("_BoundRedirectHandler", (_RedirectHandler,), {"result": {}})
    server = http.server.HTTPServer((host if host != "localhost" else "127.0.0.1", port), handler)
    server.timeout = 1
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set() and not handler.result:
            server.handle_request()

    thread = threading.Thread(target=serve, name="oauth2-redirect", daemon=True)
    thread.start()
    auth_log.trace(
        auth_log.STEP, detail=f"listening for the redirect on {redirect_uri}"
    )
    try:
        open_browser(
            build_authorization_url(
                config,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=challenge,
            )
        )
        deadline = time.monotonic() + max(int(config.login_timeout or 120), 1)
        while not handler.result and time.monotonic() < deadline:
            time.sleep(0.1)
    finally:
        stop.set()
        try:
            server.server_close()
        except Exception:
            pass

    result = dict(handler.result)
    if not result:
        raise OAuth2InteractionRequired("The sign-in did not complete in time.")
    if result.get("error"):
        raise _provider_error(result)
    if result.get("state") != state:
        # A mismatched state means the response did not originate from the
        # request this process started, so the code must not be redeemed.
        raise OAuth2Error("The sign-in response did not match the request. Try again.")
    code = result.get("code")
    if not code:
        raise OAuth2Error("The identity provider did not return an authorization code.")
    auth_log.trace(
        auth_log.STEP, detail="redirect received; state verified, redeeming the code"
    )
    return exchange_authorization_code(
        config,
        code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        session=session,
        now=now,
    )
