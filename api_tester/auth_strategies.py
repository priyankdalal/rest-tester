"""Per-method authentication strategies.

Each supported authentication method is one :class:`AuthStrategy` subclass that
declares three things about itself:

* **what it needs to be configured** (:attr:`AuthStrategy.config_fields`) and
  **what secrets it holds** (:attr:`AuthStrategy.secret_fields`), which is what
  lets the profile dialog render itself for 13 different methods without a
  growing chain of show/hide branches;
* **which request headers or query parameters it will occupy**, which the
  request layer needs *before* a token exists in order to detect a manual
  header conflict and to render a redacted preview;
* **how to produce credentials** for one request.

The registry deliberately separates methods that can be applied from static
configuration alone from those that must see the finalized request. A method
that signs over the body cannot be applied before the body exists, and a
method whose credential depends on a server challenge cannot be applied
before the first response. :attr:`AuthStrategy.requires_request_context` and
:attr:`AuthStrategy.is_challenge_based` make that explicit so the caller
cannot accidentally apply one too early.
"""

from __future__ import annotations

import base64
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence
from urllib.parse import urlsplit

__all__ = [
    "ConfigField",
    "SecretField",
    "RequestContext",
    "AuthOutcome",
    "AuthStrategy",
    "get_strategy",
    "all_strategies",
    "STRATEGY_IDS",
    "METHOD_LABELS",
]


@dataclass(frozen=True)
class ConfigField:
    """One non-secret configuration input, rendered into the profile dialog."""

    name: str
    label: str
    kind: str = "text"  # text | int | choice | multiline | bool
    required: bool = False
    default: Any = ""
    choices: tuple[tuple[str, str], ...] = ()
    placeholder: str = ""
    help: str = ""


@dataclass(frozen=True)
class SecretField:
    """One secret input. Never persisted unless the profile opts in."""

    name: str
    label: str
    required: bool = True
    multiline: bool = False
    placeholder: str = ""
    help: str = ""


@dataclass(frozen=True)
class RequestContext:
    """The finalized request a signing strategy needs to see.

    ``body`` is the exact bytes that will go on the wire, because every
    signing method that covers a payload hashes those bytes and not a
    re-serialized approximation of them.
    """

    method: str = "GET"
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    form_params: tuple[tuple[str, str], ...] = ()
    content_type: str = ""


@dataclass
class AuthOutcome:
    """What a strategy contributes to one request."""

    headers: dict[str, str] = field(default_factory=dict)
    query: tuple[tuple[str, str], ...] = ()
    #: A ``requests`` auth callable, for challenge-based methods that cannot
    #: produce a credential until the server has replied.
    request_auth: Any = None
    #: Literal values that must be redacted from logs and generated code.
    secrets: tuple[str, ...] = ()


def _now() -> float:
    return time.time()


def _nonce() -> str:
    return secrets.token_hex(8)


class AuthStrategy:
    """Base class. Subclasses describe and apply one authentication method."""

    id: str = ""
    label: str = ""
    #: Needs the finalized method/url/body to produce a credential.
    requires_request_context: bool = False
    #: Cannot produce a credential until the server issues a challenge.
    is_challenge_based: bool = False
    #: Opens a browser during an explicit sign-in action.
    is_interactive: bool = False
    #: Obtains a token from an authorization server rather than holding one.
    is_token_based: bool = False
    #: May occupy the environment's API-key binding slot.
    is_api_key_like: bool = False
    config_fields: tuple[ConfigField, ...] = ()
    secret_fields: tuple[SecretField, ...] = ()

    def __init__(
        self,
        clock: Optional[Callable[[], float]] = None,
        nonce_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._clock = clock or _now
        self._nonce = nonce_factory or _nonce

    # -- description ----------------------------------------------------

    def header_names(self, profile: Any) -> tuple[str, ...]:
        """Headers this method will set, for conflict detection and preview."""
        return ("Authorization",) if self.id != "no_auth" else ()

    def query_names(self, profile: Any) -> tuple[str, ...]:
        """Query parameters this method will set."""
        return ()

    def validate(self, profile: Any) -> None:
        """Raises ``ValueError`` when the profile is not usable.

        The base implementation enforces every declared required field, so a
        subclass only overrides this for cross-field rules.
        """
        for config in self.config_fields:
            if config.required and not str(option(profile, config.name) or "").strip():
                raise ValueError(f"{config.label} is required for {self.label}.")

    def validate_secrets(self, secrets_map: dict[str, str]) -> None:
        for secret in self.secret_fields:
            if secret.required and not str(secrets_map.get(secret.name, "")).strip():
                raise ValueError(f"{secret.label} is required for {self.label}.")

    # -- application ----------------------------------------------------

    def apply(
        self,
        profile: Any,
        secrets_map: dict[str, str],
        context: Optional[RequestContext] = None,
        token_provider: Optional[Callable[[], str]] = None,
    ) -> AuthOutcome:
        raise NotImplementedError

    # -- helpers --------------------------------------------------------

    def _timestamp(self) -> int:
        return int(self._clock())

    @staticmethod
    def _bearer(token: str) -> AuthOutcome:
        value = f"Bearer {token}"
        return AuthOutcome(headers={"Authorization": value}, secrets=(value, token))


def option(profile: Any, name: str, default: Any = "") -> Any:
    """Reads a method-specific option, falling back to a legacy top-level field.

    ``header_name`` predates the options bag and is still a real attribute on
    the profile, so it is resolved from both places rather than being migrated
    and risking older settings documents losing their value.
    """
    options = getattr(profile, "options", None) or {}
    if name in options:
        return options[name]
    return getattr(profile, name, default)


# -- no auth ----------------------------------------------------------------


class NoAuthStrategy(AuthStrategy):
    id = "no_auth"
    label = "No Auth"

    def header_names(self, profile: Any) -> tuple[str, ...]:
        return ()

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        return AuthOutcome()


# -- static credentials -----------------------------------------------------


class BasicAuthStrategy(AuthStrategy):
    id = "basic"
    label = "Basic Auth"
    secret_fields = (
        SecretField("username", "Username"),
        SecretField("password", "Password", required=False),
    )

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        username = secrets_map.get("username", "")
        password = secrets_map.get("password", "")
        # Latin-1 is what RFC 7617 specifies for the userid:password blob.
        blob = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        value = f"Basic {blob}"
        return AuthOutcome(headers={"Authorization": value}, secrets=(value, blob, password))


class BearerTokenStrategy(AuthStrategy):
    id = "manual_bearer"
    label = "Bearer Token"
    secret_fields = (SecretField("token", "Token", multiline=True),)
    config_fields = (
        ConfigField(
            "scheme", "Scheme", default="Bearer", placeholder="Bearer",
            help="The authentication scheme prefix. Some APIs use Token or JWT.",
        ),
    )

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        token = secrets_map.get("token", "")
        scheme = str(option(profile, "scheme", "Bearer") or "Bearer").strip()
        value = f"{scheme} {token}" if scheme else token
        return AuthOutcome(headers={"Authorization": value}, secrets=(value, token))


class ApiKeyStrategy(AuthStrategy):
    id = "api_key"
    label = "API Key"
    is_api_key_like = True
    secret_fields = (SecretField("key", "API key"),)
    config_fields = (
        ConfigField(
            "header_name", "Parameter name", required=True, default="x-api-key",
            placeholder="x-api-key",
        ),
        ConfigField(
            "placement", "Add to", kind="choice", default="header",
            choices=(("Header", "header"), ("Query parameter", "query")),
            help="Query placement puts the key in the URL, where it can be "
                 "captured by proxies and server logs.",
        ),
    )

    def _placement(self, profile: Any) -> str:
        return "query" if option(profile, "placement", "header") == "query" else "header"

    def header_names(self, profile: Any) -> tuple[str, ...]:
        if self._placement(profile) == "query":
            return ()
        return (str(option(profile, "header_name", "x-api-key")),)

    def query_names(self, profile: Any) -> tuple[str, ...]:
        if self._placement(profile) != "query":
            return ()
        return (str(option(profile, "header_name", "x-api-key")),)

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        key = secrets_map.get("key", "")
        name = str(option(profile, "header_name", "x-api-key"))
        if self._placement(profile) == "query":
            return AuthOutcome(query=((name, key),), secrets=(key,))
        return AuthOutcome(headers={name: key}, secrets=(key,))


# -- JWT family -------------------------------------------------------------

_JWT_ALGORITHMS = (
    ("HS256", "HS256"), ("HS384", "HS384"), ("HS512", "HS512"),
    ("RS256", "RS256"), ("RS384", "RS384"), ("RS512", "RS512"),
    ("ES256", "ES256"), ("ES384", "ES384"), ("PS256", "PS256"),
)


def _encode_jwt(payload: dict, key: str, algorithm: str, headers: Optional[dict] = None) -> str:
    import jwt  # local import: only needed on the JWT paths

    material: Any = key
    if algorithm.startswith(("RS", "ES", "PS")):
        from cryptography.hazmat.primitives import serialization

        try:
            material = serialization.load_pem_private_key(
                key.encode("utf-8"), password=None
            )
        except Exception:
            raise ValueError(
                f"{algorithm} requires a valid PEM private key."
            ) from None
    try:
        return jwt.encode(payload, material, algorithm=algorithm, headers=headers or None)
    except Exception as exc:
        raise ValueError(f"Unable to sign the JWT: {exc}") from None


class JwtBearerStrategy(AuthStrategy):
    id = "jwt_bearer"
    label = "JWT Bearer"
    secret_fields = (
        SecretField(
            "signing_key", "Signing key", multiline=True,
            help="A shared secret for HS*, or a PEM private key for RS*/ES*/PS*.",
        ),
    )
    config_fields = (
        ConfigField(
            "algorithm", "Algorithm", kind="choice", default="HS256",
            choices=_JWT_ALGORITHMS, required=True,
        ),
        ConfigField("issuer", "Issuer (iss)"),
        ConfigField("subject", "Subject (sub)"),
        ConfigField("audience", "Audience (aud)"),
        ConfigField("key_id", "Key ID (kid)"),
        ConfigField(
            "lifetime", "Token lifetime", kind="int", default=300,
            help="Seconds until the generated token expires.",
        ),
        ConfigField(
            "claims", "Additional claims", kind="multiline",
            placeholder='{"scope": "read"}',
            help="A JSON object merged into the payload.",
        ),
        ConfigField("scheme", "Scheme", default="Bearer"),
    )

    def _payload(self, profile: Any) -> dict:
        import json

        now = self._timestamp()
        try:
            lifetime = int(option(profile, "lifetime", 300) or 300)
        except (TypeError, ValueError):
            lifetime = 300
        payload: dict[str, Any] = {"iat": now, "exp": now + max(1, lifetime)}
        for claim, key in (("iss", "issuer"), ("sub", "subject"), ("aud", "audience")):
            value = str(option(profile, key, "") or "").strip()
            if value:
                payload[claim] = value
        extra = str(option(profile, "claims", "") or "").strip()
        if extra:
            try:
                parsed = json.loads(extra)
            except ValueError:
                raise ValueError("Additional claims must be a valid JSON object.") from None
            if not isinstance(parsed, dict):
                raise ValueError("Additional claims must be a JSON object.")
            payload.update(parsed)
        return payload

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        algorithm = str(option(profile, "algorithm", "HS256") or "HS256")
        key_id = str(option(profile, "key_id", "") or "").strip()
        token = _encode_jwt(
            self._payload(profile),
            secrets_map.get("signing_key", ""),
            algorithm,
            {"kid": key_id} if key_id else None,
        )
        scheme = str(option(profile, "scheme", "Bearer") or "Bearer").strip()
        value = f"{scheme} {token}" if scheme else token
        return AuthOutcome(headers={"Authorization": value}, secrets=(value, token))


class AsapStrategy(AuthStrategy):
    id = "asap"
    label = "ASAP (Atlassian)"
    secret_fields = (
        SecretField("private_key", "Private key", multiline=True,
                    help="The PEM RSA private key issued for this service."),
    )
    config_fields = (
        ConfigField("issuer", "Issuer", required=True, placeholder="micros/service"),
        ConfigField("key_id", "Key ID", required=True, placeholder="micros/service/key.pem"),
        ConfigField("audience", "Audience", required=True, placeholder="target-service"),
        ConfigField("subject", "Subject", help="Defaults to the issuer when blank."),
        ConfigField("additional_claims", "Additional claims", kind="multiline"),
        ConfigField("lifetime", "Token lifetime", kind="int", default=3600),
        ConfigField(
            "algorithm", "Algorithm", kind="choice", default="RS256",
            choices=(("RS256", "RS256"), ("ES256", "ES256")),
        ),
    )

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        import json
        import uuid

        now = self._timestamp()
        try:
            lifetime = int(option(profile, "lifetime", 3600) or 3600)
        except (TypeError, ValueError):
            lifetime = 3600
        issuer = str(option(profile, "issuer", "") or "")
        payload: dict[str, Any] = {
            "iss": issuer,
            "sub": str(option(profile, "subject", "") or "") or issuer,
            "aud": str(option(profile, "audience", "") or ""),
            "iat": now,
            "exp": now + max(1, lifetime),
            # ASAP requires a unique jti; the issuer prefix is part of the spec.
            "jti": f"{now}/{uuid.uuid4().hex}",
        }
        extra = str(option(profile, "additional_claims", "") or "").strip()
        if extra:
            try:
                parsed = json.loads(extra)
            except ValueError:
                raise ValueError("Additional claims must be a valid JSON object.") from None
            if not isinstance(parsed, dict):
                raise ValueError("Additional claims must be a JSON object.")
            payload.update(parsed)
        token = _encode_jwt(
            payload,
            secrets_map.get("private_key", ""),
            str(option(profile, "algorithm", "RS256") or "RS256"),
            {"kid": str(option(profile, "key_id", "") or "")},
        )
        value = f"Bearer {token}"
        return AuthOutcome(headers={"Authorization": value}, secrets=(value, token))


# -- challenge based --------------------------------------------------------


class DigestAuthStrategy(AuthStrategy):
    id = "digest"
    label = "Digest Auth"
    is_challenge_based = True
    secret_fields = (
        SecretField("username", "Username"),
        SecretField("password", "Password", required=False),
    )

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        import requests.auth

        password = secrets_map.get("password", "")
        # requests computes the digest response itself after the 401
        # challenge; nothing can be placed on the first request.
        return AuthOutcome(
            request_auth=requests.auth.HTTPDigestAuth(
                secrets_map.get("username", ""), password
            ),
            secrets=(password,) if password else (),
        )


# -- request signing --------------------------------------------------------


class AwsSignatureStrategy(AuthStrategy):
    id = "aws_sigv4"
    label = "AWS Signature"
    requires_request_context = True
    secret_fields = (
        SecretField("access_key", "Access key ID"),
        SecretField("secret_key", "Secret access key"),
        SecretField("session_token", "Session token", required=False, multiline=True),
    )
    config_fields = (
        ConfigField("region", "Region", required=True, default="us-east-1"),
        ConfigField("service", "Service", required=True, placeholder="execute-api"),
        ConfigField(
            "sign_payload_header", "Sign payload header", kind="bool", default=False,
            help="Required by S3. Adds x-amz-content-sha256 to the signed headers.",
        ),
    )

    def header_names(self, profile: Any) -> tuple[str, ...]:
        names = ["Authorization", "X-Amz-Date"]
        if option(profile, "sign_payload_header", False):
            names.append("X-Amz-Content-Sha256")
        return tuple(names)

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        from .auth_signing import aws_sigv4_headers

        if context is None:
            raise ValueError("AWS Signature requires the request context.")
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self._clock()))
        headers = aws_sigv4_headers(
            method=context.method,
            url=context.url,
            headers=context.headers,
            body=context.body,
            access_key=secrets_map.get("access_key", ""),
            secret_key=secrets_map.get("secret_key", ""),
            region=str(option(profile, "region", "us-east-1") or "us-east-1"),
            service=str(option(profile, "service", "") or ""),
            timestamp=timestamp,
            session_token=secrets_map.get("session_token", ""),
            sign_payload_header=bool(option(profile, "sign_payload_header", False)),
        )
        return AuthOutcome(headers=headers, secrets=(headers["Authorization"],))


class OAuth1Strategy(AuthStrategy):
    id = "oauth1"
    label = "OAuth 1.0"
    requires_request_context = True
    secret_fields = (
        SecretField("consumer_secret", "Consumer secret", required=False),
        SecretField("token_secret", "Token secret", required=False),
        SecretField("private_key", "Private key", required=False, multiline=True,
                    help="PEM key, required only for RSA-SHA1."),
    )
    config_fields = (
        ConfigField("consumer_key", "Consumer key", required=True),
        ConfigField("token", "Access token"),
        ConfigField(
            "signature_method", "Signature method", kind="choice", default="HMAC-SHA1",
            choices=(
                ("HMAC-SHA1", "HMAC-SHA1"), ("HMAC-SHA256", "HMAC-SHA256"),
                ("RSA-SHA1", "RSA-SHA1"), ("PLAINTEXT", "PLAINTEXT"),
            ),
        ),
        ConfigField("realm", "Realm"),
    )

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        from .auth_signing import oauth1_authorization

        if context is None:
            raise ValueError("OAuth 1.0 requires the request context.")
        method = str(option(profile, "signature_method", "HMAC-SHA1") or "HMAC-SHA1")
        if method == "PLAINTEXT" and urlsplit(context.url).scheme != "https":
            raise ValueError("PLAINTEXT signatures transmit the secret and require HTTPS.")
        value = oauth1_authorization(
            method=context.method,
            url=context.url,
            consumer_key=str(option(profile, "consumer_key", "") or ""),
            consumer_secret=secrets_map.get("consumer_secret", ""),
            token=str(option(profile, "token", "") or ""),
            token_secret=secrets_map.get("token_secret", ""),
            signature_method=method,
            timestamp=str(self._timestamp()),
            nonce=self._nonce(),
            realm=str(option(profile, "realm", "") or ""),
            body_params=context.form_params or None,
            private_key=secrets_map.get("private_key", ""),
        )
        return AuthOutcome(headers={"Authorization": value}, secrets=(value,))


class HawkStrategy(AuthStrategy):
    id = "hawk"
    label = "Hawk Authentication"
    requires_request_context = True
    secret_fields = (SecretField("key", "Hawk key"),)
    config_fields = (
        ConfigField("key_id", "Hawk auth ID", required=True),
        ConfigField(
            "algorithm", "Algorithm", kind="choice", default="sha256",
            choices=(("SHA-256", "sha256"), ("SHA-1", "sha1")),
        ),
        ConfigField("ext", "Extension data"),
        ConfigField(
            "include_payload_hash", "Include payload hash", kind="bool", default=False,
            help="Binds the signature to the request body when the server verifies it.",
        ),
    )

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        from .auth_signing import hawk_authorization

        if context is None:
            raise ValueError("Hawk requires the request context.")
        value = hawk_authorization(
            method=context.method,
            url=context.url,
            key_id=str(option(profile, "key_id", "") or ""),
            key=secrets_map.get("key", ""),
            timestamp=str(self._timestamp()),
            nonce=self._nonce(),
            algorithm=str(option(profile, "algorithm", "sha256") or "sha256"),
            ext=str(option(profile, "ext", "") or ""),
            body=context.body,
            content_type=context.content_type,
            include_payload_hash=bool(option(profile, "include_payload_hash", False)),
        )
        return AuthOutcome(headers={"Authorization": value}, secrets=(value,))


class EdgeGridStrategy(AuthStrategy):
    id = "edgegrid"
    label = "Akamai EdgeGrid"
    requires_request_context = True
    secret_fields = (
        SecretField("client_secret", "Client secret"),
        SecretField("access_token", "Access token"),
    )
    config_fields = (
        ConfigField("client_token", "Client token", required=True),
        ConfigField(
            "max_body", "Max body size", kind="int", default=131072,
            help="Bytes of the POST body included in the signature.",
        ),
    )

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        from .auth_signing import edgegrid_authorization

        if context is None:
            raise ValueError("Akamai EdgeGrid requires the request context.")
        try:
            max_body = int(option(profile, "max_body", 131072) or 131072)
        except (TypeError, ValueError):
            max_body = 131072
        value = edgegrid_authorization(
            method=context.method,
            url=context.url,
            client_token=str(option(profile, "client_token", "") or ""),
            client_secret=secrets_map.get("client_secret", ""),
            access_token=secrets_map.get("access_token", ""),
            timestamp=time.strftime("%Y%m%dT%H:%M:%S+0000", time.gmtime(self._clock())),
            nonce=self._nonce(),
            body=context.body,
            max_body=max_body,
        )
        return AuthOutcome(headers={"Authorization": value}, secrets=(value,))


# -- token based ------------------------------------------------------------


class _TokenBackedStrategy(AuthStrategy):
    """Shared behaviour for methods whose token comes from the manager."""

    is_token_based = True

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        if token_provider is None:
            raise ValueError(f"{self.label} requires a token provider.")
        return self._bearer(token_provider())


class EntraPkceStrategy(_TokenBackedStrategy):
    id = "entra_pkce"
    label = "Microsoft Entra ID (PKCE)"
    is_interactive = True
    config_fields = (
        ConfigField("authority", "Authority", required=True,
                    placeholder="https://login.microsoftonline.com/<tenant>"),
        ConfigField("client_id", "Client ID", required=True),
        ConfigField("scopes", "Scopes (comma separated)", required=True,
                    placeholder="api://.../.default"),
        ConfigField("redirect_uri", "Redirect URI", placeholder="http://localhost",
                    help="The loopback address the browser is redirected back to. "
                         "Leave blank or omit the port to let the listener take any "
                         "free port, which needs http://localhost registered on the "
                         "app. Pin a port when the app registration only allows a "
                         "specific one."),
        ConfigField("login_timeout", "Sign-in timeout", kind="int", default=120),
    )


class B2cPkceStrategy(EntraPkceStrategy):
    id = "b2c_pkce"
    label = "Azure AD B2C (PKCE)"


class ClientCredentialsStrategy(_TokenBackedStrategy):
    id = "client_credentials"
    label = "OAuth 2.0 client credentials"
    secret_fields = (SecretField("client_secret", "Client secret"),)
    config_fields = (
        ConfigField("authority", "Authority", required=True,
                    placeholder="https://login.microsoftonline.com/<tenant>"),
        ConfigField("client_id", "Client ID", required=True),
        ConfigField("scopes", "Scopes (comma separated)", required=True,
                    placeholder="api://.../.default"),
    )


class GenericOAuth2Strategy(_TokenBackedStrategy):
    """OAuth 2.0 against any authorization server, not just Microsoft."""

    id = "oauth2"
    label = "OAuth 2.0"
    is_interactive = True
    secret_fields = (
        SecretField("client_secret", "Client secret", required=False,
                    help="Omit for a public client using PKCE."),
    )
    config_fields = (
        ConfigField(
            "grant_type", "Grant type", kind="choice", default="authorization_code",
            choices=(
                ("Authorization Code (PKCE)", "authorization_code"),
                ("Client Credentials", "client_credentials"),
                ("Device Code", "device_code"),
                ("Resource Owner Password", "password"),
            ),
            required=True,
        ),
        ConfigField("authorization_url", "Authorization URL",
                    placeholder="https://id.example.com/oauth2/authorize"),
        ConfigField("token_url", "Token URL", required=True,
                    placeholder="https://id.example.com/oauth2/token"),
        ConfigField("device_authorization_url", "Device authorization URL"),
        ConfigField("client_id", "Client ID", required=True),
        ConfigField("scopes", "Scopes (comma separated)", placeholder="openid profile"),
        ConfigField("audience", "Audience",
                    help="Sent as an extra parameter by Auth0-style servers."),
        ConfigField("redirect_uri", "Redirect URI", default="http://localhost",
                    placeholder="http://localhost",
                    help="The loopback address the browser is redirected back to. "
                         "Must match a redirect URI registered with the provider."),
        ConfigField(
            "client_auth", "Client authentication", kind="choice", default="basic",
            choices=(
                ("Basic header", "basic"),
                ("In request body", "body"),
            ),
        ),
        ConfigField("scheme", "Header prefix", default="Bearer"),
        ConfigField("login_timeout", "Sign-in timeout", kind="int", default=120),
    )

    def validate(self, profile: Any) -> None:
        super().validate(profile)
        grant = str(option(profile, "grant_type", "authorization_code") or "")
        if grant == "authorization_code" and not str(
            option(profile, "authorization_url", "") or ""
        ).strip():
            raise ValueError("Authorization URL is required for the authorization code grant.")
        if grant == "device_code" and not str(
            option(profile, "device_authorization_url", "") or ""
        ).strip():
            raise ValueError("Device authorization URL is required for the device code grant.")

    def apply(self, profile, secrets_map, context=None, token_provider=None) -> AuthOutcome:
        if token_provider is None:
            raise ValueError(f"{self.label} requires a token provider.")
        token = token_provider()
        scheme = str(option(profile, "scheme", "Bearer") or "Bearer").strip()
        value = f"{scheme} {token}" if scheme else token
        return AuthOutcome(headers={"Authorization": value}, secrets=(value, token))


# -- registry ---------------------------------------------------------------

_STRATEGY_CLASSES: tuple[type[AuthStrategy], ...] = (
    NoAuthStrategy,
    BasicAuthStrategy,
    BearerTokenStrategy,
    JwtBearerStrategy,
    DigestAuthStrategy,
    OAuth1Strategy,
    GenericOAuth2Strategy,
    EntraPkceStrategy,
    B2cPkceStrategy,
    ClientCredentialsStrategy,
    HawkStrategy,
    AwsSignatureStrategy,
    ApiKeyStrategy,
    EdgeGridStrategy,
    AsapStrategy,
)

_REGISTRY: dict[str, AuthStrategy] = {cls.id: cls() for cls in _STRATEGY_CLASSES}

#: Ordered ids, mirroring the order the methods are offered in the UI.
STRATEGY_IDS: tuple[str, ...] = tuple(cls.id for cls in _STRATEGY_CLASSES)
METHOD_LABELS: dict[str, str] = {cls.id: cls.label for cls in _STRATEGY_CLASSES}


def get_strategy(method: str) -> AuthStrategy:
    """Returns the strategy for ``method``, or raises ``ValueError``."""
    strategy = _REGISTRY.get(method)
    if strategy is None:
        raise ValueError(
            f"Unsupported authentication method {method!r}. Supported methods "
            "are: " + ", ".join(STRATEGY_IDS) + "."
        )
    return strategy


def all_strategies() -> tuple[AuthStrategy, ...]:
    return tuple(_REGISTRY[key] for key in STRATEGY_IDS)
