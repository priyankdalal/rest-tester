"""Core authentication engine for the REST tester.

Authentication is organised as a strategy registry (:mod:`auth_strategies`),
which is what lets a single engine cover credential-style methods (bearer
tokens, API keys, Basic, JWT), per-request signing methods (AWS SigV4, OAuth
1.0, Hawk, Akamai EdgeGrid) and interactive OAuth alike. Microsoft Entra ID
and Azure AD B2C stay on MSAL for broker and Conditional Access support; every
other OAuth 2.0 provider is handled by :mod:`oauth2_client`. NTLM is
deliberately excluded.

Design notes
------------
* ``msal`` / ``msal_extensions`` are optional dependencies. They are only
  imported lazily, inside the default factory functions below, so importing
  this module never requires them to be installed and tests can fully
  substitute fakes through the injectable hooks on
  :class:`AuthenticationManager` (``msal_app_factory``, ``persistence_factory``)
  without the real packages ever being imported.
* Nothing here logs, raises, or returns a raw secret, access/refresh token,
  or unfiltered provider error payload. Every exception raised to a caller is
  an :class:`AuthError` (or the more specific :class:`InteractionRequired`)
  carrying a short, sanitized message; the original exception is deliberately
  *not* chained (``raise ... from None``) so a sensitive payload can never
  ride along on the traceback.
* ``headers()`` never triggers an interactive browser sign-in; only
  ``sign_in()`` does, and it is bounded by ``AuthProfile.login_timeout``.
* A per-(environment, profile, config-fingerprint) lock makes sign-in/renew
  single-flight: only one interactive/silent-refresh attempt runs at a time
  for a given profile context. When ``persistent`` caching is enabled, the
  on-disk cache is wrapped through ``msal_extensions`` which owns its own
  cross-process file lock. Remembered static secrets use the same encrypted
  persistence and cross-process lock primitives in a separate file.
* Config changes (method, authority, client_id, scopes, header name, and any
  method-specific option) change a profile's fingerprint, which isolates it
  from any previously cached MSAL app/token-cache/on-disk file for the same
  profile id -- old sessions are never silently reused after a config edit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Optional
from urllib.parse import urlparse
from uuid import uuid4

from . import auth_log, oauth2_client
from .auth_strategies import (
    STRATEGY_IDS,
    AuthOutcome,
    RequestContext,
    all_strategies,
    get_strategy,
)

__all__ = [
    "AuthError",
    "InteractionRequired",
    "AuthProfile",
    "AuthBinding",
    "AuthenticationManager",
    "get_auth_manager",
    "SUPPORTED_METHODS",
]


class AuthError(RuntimeError):
    """A sanitized, user-facing authentication failure.

    Messages are always short and safe to display; they never include raw
    provider responses, tokens, or secrets.
    """


class InteractionRequired(AuthError):
    """A silent/cached token could not be produced.

    The caller should invite the user to run :meth:`AuthenticationManager.sign_in`.
    """


#: Authentication methods implemented in this release. Derived from the
#: strategy registry so a new method is registered in exactly one place.
SUPPORTED_METHODS = STRATEGY_IDS

#: Methods that hold at least one caller-supplied secret.
_SECRET_METHODS = frozenset(
    strategy.id for strategy in all_strategies() if strategy.secret_fields
)

#: Methods backed by an MSAL public/confidential client application.
_MSAL_METHODS = frozenset({"entra_pkce", "b2c_pkce", "client_credentials"})

#: Methods that acquire a token from an authorization server.
_OAUTH_METHODS = _MSAL_METHODS | {"oauth2"}

#: Methods that open an interactive browser window via ``sign_in()``.
_INTERACTIVE_METHODS = frozenset(
    strategy.id for strategy in all_strategies() if strategy.is_interactive
)

_NAME_MAX_LENGTH = 200
_MIN_LOGIN_TIMEOUT = 30
_MAX_LOGIN_TIMEOUT = 600
#: Loopback redirect ports are restricted to the non-privileged range.
_MIN_REDIRECT_PORT = 1024
_MAX_REDIRECT_PORT = 65535
_SHORT_LIFETIME_THRESHOLD_SECONDS = 60

#: RFC 7230 header-field-name token: any run of these characters.
_HEADER_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def _is_header_token(value: str) -> bool:
    return bool(value) and bool(_HEADER_TOKEN_RE.match(value))


def _is_clean_token(value: str) -> bool:
    """True for a non-empty printable string with no whitespace/control chars."""
    return bool(value) and all(0x21 <= ord(ch) <= 0x7E for ch in value)


def _validate_authority(authority: str, *, b2c: bool) -> None:
    if not authority:
        raise ValueError("Authority URL is required for this method.")
    parsed = urlparse(authority)
    if parsed.scheme != "https":
        raise ValueError("Authority must be an HTTPS URL.")
    if not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("Authority must not contain embedded user credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("Authority must not contain a query string or fragment.")
    segments = [segment for segment in parsed.path.split("/") if segment]
    minimum_segments = 2 if b2c else 1
    if len(segments) < minimum_segments:
        if b2c:
            raise ValueError(
                "B2C/External ID authority must include the tenant and policy "
                "path, e.g. https://<tenant>.b2clogin.com/<tenant-id>/<policy>."
            )
        raise ValueError(
            "Authority must include a tenant path segment, e.g. "
            "https://login.microsoftonline.com/<tenant-id>."
        )


def _parse_redirect_port(redirect_uri: str) -> Optional[int]:
    """Validates a loopback redirect URI and returns the port to listen on.

    MSAL builds the redirect target itself as ``http://localhost:{port}``, so
    the port is the only part a caller can influence. Everything else is
    validated against that fixed shape rather than accepted and silently
    ignored. Returns ``None`` when no port is pinned, which lets the listener
    take a system-allocated one.
    """
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise ValueError(
            "Redirect URI must use http://. The loopback listener is not "
            "TLS-terminated, and Entra allows http only for localhost."
        )
    if parsed.username or parsed.password:
        raise ValueError("Redirect URI must not contain embedded user credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("Redirect URI must not contain a query string or fragment.")
    if parsed.path not in ("", "/"):
        raise ValueError(
            "Redirect URI must not contain a path. The loopback listener only "
            "answers on the root of the address."
        )
    if parsed.hostname != "localhost":
        raise ValueError(
            "Redirect URI host must be localhost. The loopback listener always "
            "binds http://localhost, so 127.0.0.1 or a LAN address would not match."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Redirect URI port must be a number.") from exc
    if port is None:
        return None
    if not (_MIN_REDIRECT_PORT <= port <= _MAX_REDIRECT_PORT):
        raise ValueError(
            f"Redirect URI port must be between {_MIN_REDIRECT_PORT} and "
            f"{_MAX_REDIRECT_PORT}."
        )
    return port


def _validate_endpoint(url: str, label: str) -> None:
    """Rejects an OAuth endpoint that is not a plain HTTPS URL.

    Loopback is allowed so a locally hosted identity server can be targeted
    during development, matching the rule the request layer already applies.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"The {label} must be an HTTP(S) URL.")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"The {label} must use HTTPS except on loopback.")
    if not parsed.netloc or parsed.username or parsed.password:
        raise ValueError(f"The {label} must not contain embedded user credentials.")


def _scrub_options(method: str, options: Any) -> dict[str, Any]:
    """Keeps the options bag free of anything the method treats as a secret.

    ``options`` is written to the plain settings file, whereas secrets belong
    in the encrypted store. Dropping secret-named keys here means a UI or
    settings-file mistake cannot turn into a credential on disk.
    """
    if not isinstance(options, dict):
        return {}
    try:
        reserved = {field.name for field in get_strategy(method).secret_fields}
    except (KeyError, ValueError):
        reserved = set()
    return {str(k): v for k, v in options.items() if str(k) not in reserved}


def _clamp_login_timeout(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 120
    return max(_MIN_LOGIN_TIMEOUT, min(_MAX_LOGIN_TIMEOUT, parsed))


@dataclass
class AuthProfile:
    """Persisted authentication configuration. Never holds a secret/token.

    Only :meth:`from_dict`/:meth:`to_dict` participate in persistence; the
    fields below are exactly the document shape that is safe to write to
    settings on disk.
    """

    id: str = field(default_factory=lambda: uuid4().hex)
    name: str = "Authentication"
    method: str = "entra_pkce"
    authority: str = ""
    client_id: str = ""
    scopes: list[str] = field(default_factory=list)
    header_name: str = "x-api-key"
    persistent: bool = False
    login_timeout: int = 120
    #: Loopback redirect target for interactive sign-in. Empty means "let the
    #: listener take a system-allocated port", which requires the app
    #: registration to list the port-wildcard URI ``http://localhost``.
    redirect_uri: str = ""
    #: Method-specific configuration. Keeps the dataclass stable as methods are
    #: added, rather than growing a column per method that only one uses.
    options: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AuthProfile":
        raw_scopes = value.get("scopes", [])
        scopes = [str(item) for item in raw_scopes] if isinstance(raw_scopes, list) else []
        return cls(
            id=str(value.get("id") or uuid4().hex),
            name=str(value.get("name", "Authentication")),
            method=str(value.get("method", "entra_pkce")),
            authority=str(value.get("authority", "")),
            client_id=str(value.get("client_id", "")),
            scopes=scopes,
            header_name=str(value.get("header_name", "x-api-key")),
            persistent=bool(value.get("persistent", False)),
            login_timeout=_clamp_login_timeout(value.get("login_timeout", 120)),
            redirect_uri=str(value.get("redirect_uri", "")).strip(),
            options=_scrub_options(str(value.get("method", "entra_pkce")), value.get("options")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "method": self.method,
            "authority": self.authority,
            "client_id": self.client_id,
            "scopes": list(self.scopes),
            "header_name": self.header_name,
            "persistent": self.persistent,
            "login_timeout": self.login_timeout,
            "redirect_uri": self.redirect_uri,
            "options": _scrub_options(self.method, self.options),
        }

    def strategy(self):
        """The :class:`~api_tester.auth_strategies.AuthStrategy` for this method."""
        return get_strategy(self.method)

    def validate(self) -> None:
        """Raises ``ValueError`` for a malformed/unsupported configuration."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Profile name must be a non-empty string.")
        if len(self.name) > _NAME_MAX_LENGTH:
            raise ValueError(f"Profile name must be {_NAME_MAX_LENGTH} characters or fewer.")
        if self.method not in SUPPORTED_METHODS:
            raise ValueError(
                f"Unsupported authentication method {self.method!r}. Supported "
                "methods are: " + ", ".join(SUPPORTED_METHODS) + ". Device code, "
                "generic OAuth, and custom identity providers are not "
                "implemented in this release."
            )
        if not isinstance(self.login_timeout, int) or not (
            _MIN_LOGIN_TIMEOUT <= self.login_timeout <= _MAX_LOGIN_TIMEOUT
        ):
            raise ValueError(
                f"Login timeout must be an integer between {_MIN_LOGIN_TIMEOUT} "
                f"and {_MAX_LOGIN_TIMEOUT} seconds."
            )
        if self.method == "api_key":
            if not _is_header_token(self.header_name):
                raise ValueError("API key header name must be a valid HTTP header token.")
        if self.method in _MSAL_METHODS:
            _validate_authority(self.authority, b2c=self.method == "b2c_pkce")
            if not _is_clean_token(self.client_id):
                raise ValueError("Client ID must be a non-empty value with no whitespace.")
            if not self.scopes:
                raise ValueError("At least one scope is required for this method.")
            for scope in self.scopes:
                if not isinstance(scope, str) or not _is_clean_token(scope):
                    raise ValueError("Scopes must be non-empty values with no whitespace.")
        else:
            # Newer methods declare their own required fields, so validation
            # lives with the strategy instead of accumulating here.
            get_strategy(self.method).validate(self)
        if self.method == "oauth2":
            for name in ("authorization_url", "token_url", "device_authorization_url"):
                value = str(self.options.get(name, "") or "").strip()
                if value:
                    _validate_endpoint(value, name.replace("_", " "))
        if self.redirect_uri:
            if self.method not in _INTERACTIVE_METHODS:
                raise ValueError(
                    "A redirect URI only applies to interactive sign-in "
                    "(entra_pkce, b2c_pkce). Clear it for this method."
                )
            _parse_redirect_port(self.redirect_uri)

    def redirect_port(self) -> Optional[int]:
        """The loopback port to listen on, or ``None`` for system-allocated."""
        if not self.redirect_uri:
            return None
        return _parse_redirect_port(self.redirect_uri)


@dataclass
class AuthBinding:
    """Which authentication profile an environment uses, per credential slot."""

    identity: str = ""
    api_key: str = ""
    disabled: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AuthBinding":
        return cls(
            identity=str(value.get("identity", "")),
            api_key=str(value.get("api_key", "")),
            disabled=bool(value.get("disabled", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "api_key": self.api_key,
            "disabled": self.disabled,
        }


def _safe_key(*parts: str) -> str:
    """Hashes arbitrary identifiers into a filesystem-safe, fixed-length key.

    Environment/profile ids come from user-editable settings documents, so
    they are never used directly as path components.
    """
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingerprint(profile: AuthProfile) -> str:
    """A short digest of the config that changes a token cache's identity.

    Every field that can affect which credential a profile produces has to be
    covered here, including the per-method ``options`` bag; otherwise editing
    a token URL or grant type would silently reuse the previous session.
    """
    payload = "|".join(
        [
            profile.method,
            profile.authority,
            profile.client_id,
            ",".join(sorted(profile.scopes)),
            profile.header_name,
            json.dumps(profile.options, sort_keys=True, default=str),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _default_cache_root() -> Path:
    """A per-user cache location outside any portable/bundled app folder."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(
        Path.home() / ".cache"
    )
    return Path(base) / "RestTester" / "auth-cache"


def _default_cache_serializer_factory() -> Any:
    import msal  # local import: optional dependency, only touched on the real path

    return msal.SerializableTokenCache()


def _default_persistence_factory(path: Path) -> Any:
    from msal_extensions import build_encrypted_persistence

    # This returns an encrypted backend or raises; it never falls back to
    # plaintext.
    return build_encrypted_persistence(str(path))


def _default_persisted_cache_factory(persistence: Any) -> Any:
    from msal_extensions import PersistedTokenCache

    return PersistedTokenCache(persistence)


def _default_lock_factory(path: Path) -> Any:
    from msal_extensions import CrossPlatLock

    return CrossPlatLock(str(path))


def _default_msal_app_factory(
    kind: str, profile: AuthProfile, cache: Any, secret: Optional[str]
) -> Any:
    import msal  # local import: optional dependency, only touched on the real path

    if kind == "confidential":
        return msal.ConfidentialClientApplication(
            profile.client_id,
            client_credential=secret,
            authority=profile.authority,
            token_cache=cache,
        )
    return msal.PublicClientApplication(
        profile.client_id,
        authority=profile.authority,
        token_cache=cache,
    )


def _acquire_for_client(app: Any, profile: "AuthProfile", *, force_refresh: bool) -> Any:
    """Acquires an app-only token, evicting cached tokens when renewal is forced."""
    scopes = list(profile.scopes)
    if force_refresh:
        app.remove_tokens_for_client()
    return app.acquire_token_for_client(scopes=scopes)


def _call(fn: Callable[[], Any], fallback_message: str) -> Any:
    """Runs an MSAL call, converting any raw exception into a sanitized one."""
    try:
        return fn()
    except (AuthError, InteractionRequired):
        raise
    except Exception:
        # The original exception (and anything it carries: URLs, provider
        # payloads, secrets) is deliberately dropped, not chained.
        raise AuthError(fallback_message) from None


def _classify_error(result: dict[str, Any]) -> AuthError:
    code = str(result.get("error", ""))
    if code in {"interaction_required", "consent_required", "login_required"}:
        return InteractionRequired("Sign-in has expired or needs attention. Use Sign In again.")
    if code == "invalid_grant":
        return AuthError("The saved sign-in is no longer valid. Sign in again.")
    if code in {"invalid_client", "unauthorized_client"}:
        return AuthError(
            "The application registration was rejected. Check the client ID and authority."
        )
    return AuthError("Unable to acquire an access token. Sign in again.")


def _lower_lead(label: str) -> str:
    """Lowercases a label's first word unless it is an acronym such as "API"."""
    head, _, tail = label.partition(" ")
    if head.isupper() and len(head) > 1:
        return label
    return label[:1].lower() + label[1:]


def _missing_secret_message(strategy: Any) -> str:
    """A method-specific prompt naming what the user still has to enter.

    Only required fields are named. An optional secret - a public OAuth
    client's unused ``client_secret``, say - is not something the user has
    failed to supply.
    """
    fields = [
        field
        for field in getattr(strategy, "secret_fields", ())
        if getattr(field, "required", True)
    ]
    if not fields:
        return f"{strategy.label} is not configured yet."
    if len(fields) == 1:
        return (
            f"No {_lower_lead(fields[0].label)} is configured for this profile. "
            "Enter one, then retry."
        )
    names = ", ".join(_lower_lead(field.label) for field in fields)
    return (
        f"{strategy.label} needs its credentials ({names}) before it can be used. "
        "Enter them, then retry."
    )


def _decode_secret_bundle(stored: Any, profile: AuthProfile) -> dict[str, str]:
    """Reads a persisted secret payload into a named bundle.

    Files written before multi-field secrets existed hold a bare string, so
    those are mapped onto the method's primary secret field rather than being
    discarded — a remembered credential must survive the upgrade.
    """
    if not isinstance(stored, str) or not stored:
        return {}
    try:
        parsed = json.loads(stored)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("__schema__") == "secrets/1":
        values = parsed.get("values")
        if isinstance(values, dict):
            return {str(k): str(v) for k, v in values.items() if v}
        return {}
    fields = get_strategy(profile.method).secret_fields
    primary = fields[0].name if fields else "secret"
    return {primary: stored}


def _encode_secret_bundle(bundle: dict[str, str]) -> str:
    return json.dumps({"__schema__": "secrets/1", "values": dict(bundle)})


@dataclass
class _Session:
    """In-memory bookkeeping for one (environment, profile, fingerprint)."""

    app: Any
    cache: Any
    persistence_path: Optional[Path]
    lock: threading.Lock = field(default_factory=threading.Lock)


class AuthenticationManager:
    """Owns per-profile secrets, MSAL apps/caches, and status reporting.

    Hooks (``msal_app_factory``, ``persistence_factory``, ``cache_serializer_factory``,
    ``clock``) are injectable so tests never need the real ``msal``/``msal_extensions``
    packages, and so no test performs a live identity call.
    """

    def __init__(
        self,
        cache_root: Optional[Path] = None,
        *,
        msal_app_factory: Optional[Callable[[str, AuthProfile, Any, Optional[str]], Any]] = None,
        persistence_factory: Optional[Callable[[Path], Any]] = None,
        cache_serializer_factory: Optional[Callable[[], Any]] = None,
        persisted_cache_factory: Optional[Callable[[Any], Any]] = None,
        lock_factory: Optional[Callable[[Path], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
        device_code_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        activity_log: Optional[auth_log.AuthActivityLog] = None,
    ) -> None:
        self._cache_root = Path(cache_root) if cache_root is not None else _default_cache_root()
        self._app_factory = msal_app_factory or _default_msal_app_factory
        self._persistence_factory = persistence_factory or _default_persistence_factory
        self._cache_serializer_factory = cache_serializer_factory or _default_cache_serializer_factory
        self._persisted_cache_factory = persisted_cache_factory or _default_persisted_cache_factory
        self._lock_factory = lock_factory or _default_lock_factory
        self._clock = clock or time.time
        self._secrets: dict[tuple[str, str], dict[str, str]] = {}
        self._oauth2_tokens: dict[tuple[str, str], oauth2_client.TokenResponse] = {}
        self._device_code_callback = device_code_callback
        self._sessions: dict[str, _Session] = {}
        self._registry_lock = threading.Lock()
        self._log = activity_log if activity_log is not None else auth_log.activity_log

    @property
    def activity_log(self) -> auth_log.AuthActivityLog:
        """The event log this manager writes to."""
        return self._log

    # -- identifiers ---------------------------------------------------

    def _identity_key(self, environment_id: str, profile: AuthProfile) -> str:
        return _safe_key(str(environment_id), profile.id)

    def _session_key(self, environment_id: str, profile: AuthProfile) -> str:
        return _safe_key(str(environment_id), profile.id, _fingerprint(profile))

    def _cache_path(self, session_key: str) -> Path:
        return self._cache_root / f"{session_key}.bin"

    def _secret_path(self, session_key: str) -> Path:
        return self._cache_root / f"{session_key}.secret"

    @staticmethod
    def _lock_path(path: Path) -> Path:
        return Path(f"{path}.lockfile")

    def _get_secret(self, environment_id: str, profile: AuthProfile) -> Optional[str]:
        """The primary secret, for methods that hold exactly one."""
        bundle = self._get_secrets(environment_id, profile)
        if not bundle:
            return None
        fields = get_strategy(profile.method).secret_fields
        primary = fields[0].name if fields else "secret"
        return bundle.get(primary) or next(iter(bundle.values()), None)

    def _get_secrets(self, environment_id: str, profile: AuthProfile) -> dict[str, str]:
        """All named secrets for a profile, loading from disk when remembered."""
        key = (str(environment_id), profile.id)
        bundle = self._secrets.get(key)
        if bundle is not None or not profile.persistent:
            return dict(bundle or {})
        path = self._secret_path(self._session_key(environment_id, profile))
        if not path.exists():
            return {}
        try:
            persistence = self._persistence_factory(path)
            if persistence is None:
                raise RuntimeError("encrypted persistence unavailable")
            with self._lock_factory(self._lock_path(path)):
                stored = persistence.load()
        except Exception:
            raise AuthError("Unable to read the remembered credential securely.") from None
        loaded = _decode_secret_bundle(stored, profile)
        if not loaded:
            raise AuthError("The remembered credential is invalid. Clear it and enter it again.")
        self._secrets[key] = loaded
        return dict(loaded)

    # -- secrets ---------------------------------------------------------

    def set_secret(self, environment_id: str, profile: AuthProfile, secret: str) -> None:
        """Stores a single static secret against the method's primary field."""
        fields = get_strategy(profile.method).secret_fields
        primary = fields[0].name if fields else "secret"
        self.set_secrets(environment_id, profile, {primary: secret})

    def set_secrets(
        self, environment_id: str, profile: AuthProfile, values: dict[str, str]
    ) -> None:
        """Stores named secrets, encrypted on disk when ``persistent`` is set."""
        profile.validate()
        if profile.method not in _SECRET_METHODS:
            raise ValueError(
                f"{profile.method} does not accept a manually entered secret; "
                "use sign_in() instead."
            )
        if not isinstance(values, dict) or not values:
            raise ValueError("Secret must be a non-empty string.")
        allowed = {field.name for field in get_strategy(profile.method).secret_fields}
        bundle: dict[str, str] = {}
        for name, value in values.items():
            if name not in allowed:
                raise ValueError(f"{profile.method} has no secret named {name!r}.")
            if not isinstance(value, str):
                raise ValueError("Secret must be a non-empty string.")
            if value:
                bundle[name] = value
        if not bundle or not any(value.strip() for value in bundle.values()):
            raise ValueError("Secret must be a non-empty string.")
        get_strategy(profile.method).validate_secrets(bundle)
        if profile.persistent:
            session_key = self._session_key(environment_id, profile)
            self._invalidate_stale(environment_id, profile, session_key)
            path = self._secret_path(session_key)
            try:
                self._cache_root.mkdir(parents=True, exist_ok=True)
                persistence = self._persistence_factory(path)
                if persistence is None:
                    raise RuntimeError("encrypted persistence unavailable")
                with self._lock_factory(self._lock_path(path)):
                    persistence.save(_encode_secret_bundle(bundle))
            except Exception:
                raise AuthError("Unable to remember the credential securely.") from None
        self._secrets[(str(environment_id), profile.id)] = bundle
        if profile.method in {"client_credentials", "oauth2"}:
            # Force the confidential client to be rebuilt with the new secret
            # instead of reusing a session that was constructed with the old one.
            session_key = self._session_key(environment_id, profile)
            with self._registry_lock:
                self._sessions.pop(session_key, None)


    # -- session / cache lifecycle ---------------------------------------

    def _invalidate_stale(self, environment_id: str, profile: AuthProfile, session_key: str) -> None:
        """Atomically indexes this config and removes prior encrypted state."""
        if not profile.persistent:
            return
        identity_key = self._identity_key(environment_id, profile)
        index_path = self._cache_root / "index.json"
        try:
            self._cache_root.mkdir(parents=True, exist_ok=True)
            with self._lock_factory(self._lock_path(index_path)):
                try:
                    index = (
                        json.loads(index_path.read_text(encoding="utf-8"))
                        if index_path.exists()
                        else {}
                    )
                except ValueError:
                    index = {}
                previous = index.get(identity_key)
                index[identity_key] = session_key
                index_path.write_text(json.dumps(index), encoding="utf-8")
            if previous and previous != session_key:
                for path in (
                    self._cache_root / f"{previous}.bin",
                    self._cache_root / f"{previous}.secret",
                ):
                    with self._lock_factory(self._lock_path(path)):
                        path.unlink(missing_ok=True)
        except Exception:
            raise AuthError("Unable to update the encrypted authentication storage.") from None

    def _load_cache(self, profile: AuthProfile, session_key: str) -> tuple[Any, Optional[Path]]:
        if not profile.persistent:
            return self._cache_serializer_factory(), None
        path = self._cache_path(session_key)
        try:
            self._cache_root.mkdir(parents=True, exist_ok=True)
            persistence = self._persistence_factory(path)
            if persistence is None:
                raise RuntimeError("no encrypted persistence backend available")
            cache = self._persisted_cache_factory(persistence)
        except Exception:
            raise AuthError("Encrypted sign-in storage is unavailable.") from None
        return cache, path

    def _get_or_create_session(
        self, environment_id: str, profile: AuthProfile, *, secret: Optional[str]
    ) -> _Session:
        session_key = self._session_key(environment_id, profile)
        with self._registry_lock:
            session = self._sessions.get(session_key)
            if session is not None:
                return session
            self._invalidate_stale(environment_id, profile, session_key)
            cache, persistence_path = self._load_cache(profile, session_key)
            kind = "confidential" if profile.method == "client_credentials" else "public"
            try:
                app = self._app_factory(kind, profile, cache, secret)
            except Exception:
                raise AuthError("Unable to initialize the identity client.") from None
            session = _Session(app=app, cache=cache, persistence_path=persistence_path)
            self._sessions[session_key] = session
            return session

    def _persist(self, session: _Session) -> None:
        """PersistedTokenCache writes through its encrypted backend on mutation."""


    # -- headers -----------------------------------------------------------

    def headers(
        self, environment_id: str, profile: AuthProfile, *, force_refresh: bool = False
    ) -> dict[str, str]:
        """Returns request headers for ``profile``. Never opens a browser window."""
        return self.credentials(
            environment_id, profile, force_refresh=force_refresh
        ).headers

    def credentials(
        self,
        environment_id: str,
        profile: AuthProfile,
        *,
        context: Optional[RequestContext] = None,
        force_refresh: bool = False,
    ) -> AuthOutcome:
        """Produces everything ``profile`` contributes to one request.

        Unlike :meth:`headers` this can also return query parameters and a
        ``requests`` auth callable, which the API-key-in-query and Digest
        methods respectively require.
        """
        started = perf_counter()
        try:
            with auth_log.flow(
                profile.name or profile.id, profile.method,
                environment=str(environment_id), log=self._log,
            ):
                outcome = self._credentials(
                    environment_id, profile, context, force_refresh
                )
        except ValueError as exc:
            if not isinstance(exc, (AuthError, InteractionRequired)):
                exc = AuthError(str(exc))
            self._log.add(
                auth_log.FAILED, profile.name or profile.id, profile.method,
                environment=str(environment_id), detail=str(exc),
                duration_ms=(perf_counter() - started) * 1000, ok=False,
            )
            raise exc from None
        except (AuthError, InteractionRequired) as exc:
            self._log.add(
                auth_log.FAILED, profile.name or profile.id, profile.method,
                environment=str(environment_id), detail=str(exc),
                duration_ms=(perf_counter() - started) * 1000, ok=False,
            )
            raise
        self._log.add(
            auth_log.APPLIED, profile.name or profile.id, profile.method,
            environment=str(environment_id),
            detail="forced refresh" if force_refresh else "",
            duration_ms=(perf_counter() - started) * 1000,
            header_names=tuple(outcome.headers),
            query_names=tuple(name for name, _ in outcome.query),
        )
        return outcome

    def _credentials(
        self,
        environment_id: str,
        profile: AuthProfile,
        context: Optional[RequestContext],
        force_refresh: bool,
    ) -> AuthOutcome:
        """The unlogged body of :meth:`credentials`."""
        profile.validate()
        strategy = get_strategy(profile.method)
        if strategy.requires_request_context and context is None:
            raise AuthError(
                f"{strategy.label} signs each request and cannot be applied "
                "without the request details."
            )
        secrets_map = self._get_secrets(environment_id, profile)
        if not secrets_map and any(
            field.required for field in strategy.secret_fields
        ):
            raise AuthError(_missing_secret_message(strategy))
        strategy.validate_secrets(secrets_map)

        provider = None
        if strategy.is_token_based:
            def provider() -> str:  # noqa: F811 - narrow, local by design
                return self._access_token(
                    environment_id, profile, force_refresh=force_refresh
                )

        return strategy.apply(profile, secrets_map, context, provider)

    def _access_token(
        self, environment_id: str, profile: AuthProfile, *, force_refresh: bool
    ) -> str:
        """Returns a bare access token for the token-backed methods."""
        if profile.method == "oauth2":
            return self._generic_oauth_token(
                environment_id, profile, force_refresh=force_refresh
            )
        if profile.method == "client_credentials":
            return self._client_credentials_token(
                environment_id, profile, force_refresh=force_refresh
            )
        return self._silent_token(environment_id, profile, force_refresh=force_refresh)

    def _token_from_result(self, result: Optional[dict[str, Any]]) -> str:
        """Extracts the access token, translating provider errors on the way."""
        if not result:
            raise InteractionRequired("Not signed in. Use Sign In to continue.")
        if "error" in result:
            raise _classify_error(result)
        token = result.get("access_token")
        if not token:
            raise AuthError("The identity provider did not return an access token.")
        # ID tokens (if MSAL returned one) are intentionally never surfaced.
        return str(token)

    def _maybe_refresh_short_lived(
        self, app: Any, profile: AuthProfile, account: Any, result: Optional[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        if not result or "error" in result:
            return result
        expires_in = result.get("expires_in")
        if not isinstance(expires_in, (int, float)) or expires_in > _SHORT_LIFETIME_THRESHOLD_SECONDS:
            return result
        refreshed = _call(
            lambda: app.acquire_token_silent_with_error(
                list(profile.scopes), account=account, force_refresh=True
            ),
            "Unable to refresh the access token.",
        )
        if refreshed and "error" not in refreshed:
            return refreshed
        if refreshed:
            return refreshed
        if expires_in <= 0:
            return None
        return result

    def _silent_token(
        self, environment_id: str, profile: AuthProfile, *, force_refresh: bool
    ) -> str:
        session = self._get_or_create_session(environment_id, profile, secret=None)
        with session.lock:
            app = session.app
            accounts = _call(app.get_accounts, "Unable to read the cached sign-in.")
            if not accounts:
                raise InteractionRequired("Not signed in. Use Sign In to continue.")
            account = accounts[0]
            result = _call(
                lambda: app.acquire_token_silent_with_error(
                    list(profile.scopes), account=account, force_refresh=force_refresh
                ),
                "Unable to acquire an access token.",
            )
            result = self._maybe_refresh_short_lived(app, profile, account, result)
            self._persist(session)
            return self._token_from_result(result)

    def _client_credentials_token(
        self, environment_id: str, profile: AuthProfile, *, force_refresh: bool
    ) -> str:
        secret = self._get_secret(environment_id, profile)
        if not secret:
            raise AuthError(
                "No client secret is configured for this profile. Enter one, then retry."
            )
        session = self._get_or_create_session(environment_id, profile, secret=secret)
        with session.lock:
            result = _call(
                lambda: _acquire_for_client(session.app, profile, force_refresh=force_refresh),
                "Unable to acquire an application token.",
            )
            self._persist(session)
            return self._token_from_result(result)


    # -- interactive sign-in / renewal --------------------------------------

    def _generic_oauth_token(
        self, environment_id: str, profile: AuthProfile, *, force_refresh: bool
    ) -> str:
        """Returns an access token from a non-Microsoft OAuth 2.0 provider.

        Interactive grants are never started here -- a cached token is reused,
        refreshed if a refresh token is held, and otherwise the caller is told
        to sign in. That keeps :meth:`headers` free of browser side effects.
        """
        key = (str(environment_id), profile.id)
        config = self._oauth2_config(environment_id, profile)
        grant = str(profile.options.get("grant_type") or "authorization_code")
        with self._registry_lock:
            cached = self._oauth2_tokens.get(key)

        if cached is not None and not force_refresh and not cached.is_expired():
            remaining = (
                f", expires in {max(0, int(cached.expires_at - time.time()))}s"
                if cached.expires_at
                else ""
            )
            auth_log.trace(
                auth_log.CACHED, detail=f"reused cached {grant} token{remaining}"
            )
            return cached.access_token

        token: Optional[oauth2_client.TokenResponse] = None
        if cached is not None and cached.refresh_token:
            auth_log.trace(auth_log.STEP, detail="cached token expired; refreshing")
            try:
                token = oauth2_client.refresh_access_token(config, cached.refresh_token)
            except oauth2_client.OAuth2Error:
                token = None
        if token is None:
            if grant == "client_credentials":
                token = oauth2_client.request_client_credentials(config)
            elif grant == "password":
                username = str(profile.options.get("username") or "").strip()
                password = self._get_secrets(environment_id, profile).get("password", "")
                if not username or not password:
                    raise AuthError(
                        "The password grant needs a username and password before it can be used."
                    )
                token = oauth2_client.request_password_token(config, username, password)
            else:
                raise InteractionRequired("Not signed in. Use Sign In to continue.")

        with self._registry_lock:
            self._oauth2_tokens[key] = token
        return token.access_token

    def _oauth2_config(
        self, environment_id: str, profile: AuthProfile
    ) -> "oauth2_client.OAuth2Config":
        options = profile.options
        secrets_map = self._get_secrets(environment_id, profile)
        raw_scopes = options.get("scopes") or profile.scopes
        if isinstance(raw_scopes, str):
            scopes = tuple(s for s in raw_scopes.replace(",", " ").split() if s)
        else:
            scopes = tuple(str(s) for s in (raw_scopes or ()) if s)
        return oauth2_client.OAuth2Config(
            client_id=str(options.get("client_id") or profile.client_id or ""),
            token_url=str(options.get("token_url") or ""),
            authorization_url=str(options.get("authorization_url") or ""),
            device_authorization_url=str(options.get("device_authorization_url") or ""),
            client_secret=secrets_map.get("client_secret", ""),
            scopes=scopes,
            audience=str(options.get("audience") or ""),
            redirect_uri=str(options.get("redirect_uri") or profile.redirect_uri or "http://localhost"),
            client_auth=str(options.get("client_auth") or "basic"),
            login_timeout=int(options.get("login_timeout") or profile.login_timeout or 120),
        )

    def _generic_oauth_sign_in(self, environment_id: str, profile: AuthProfile) -> None:
        """Completes an interactive generic OAuth 2.0 grant and caches the token."""
        config = self._oauth2_config(environment_id, profile)
        grant = str(profile.options.get("grant_type") or "authorization_code")
        try:
            if grant == "device_code":
                started = oauth2_client.request_device_code(config)
                notify = self._device_code_callback
                if notify is not None:
                    notify(started)
                token = oauth2_client.poll_device_token(config, started)
            elif grant == "authorization_code":
                token = oauth2_client.run_authorization_code_flow(config)
            else:
                # Non-interactive grants already work through headers(); this
                # path just primes the cache so status() reports signed in.
                self._generic_oauth_token(environment_id, profile, force_refresh=True)
                return
        except oauth2_client.OAuth2InteractionRequired as exc:
            raise InteractionRequired(str(exc)) from None
        except oauth2_client.OAuth2Error as exc:
            raise AuthError(str(exc)) from None
        with self._registry_lock:
            self._oauth2_tokens[(str(environment_id), profile.id)] = token

    def sign_in(self, environment_id: str, profile: AuthProfile) -> None:
        """Opens an interactive (PKCE) browser sign-in, bounded by ``login_timeout``."""
        profile.validate()
        started = perf_counter()
        if profile.method not in _INTERACTIVE_METHODS:
            supported = ", ".join(sorted(_INTERACTIVE_METHODS))
            raise ValueError(
                f"{profile.method} does not support interactive sign-in. Only "
                f"{supported} use Sign In."
            )
        if profile.method == "oauth2":
            self._generic_oauth_sign_in(environment_id, profile)
            self._log.add(
                auth_log.SIGNED_IN, profile.name or profile.id, profile.method,
                environment=str(environment_id),
                duration_ms=(perf_counter() - started) * 1000,
            )
            return
        session = self._get_or_create_session(environment_id, profile, secret=None)
        with session.lock:
            app = session.app
            port = profile.redirect_port()
            result = _call(
                lambda: app.acquire_token_interactive(
                    scopes=list(profile.scopes),
                    timeout=profile.login_timeout,
                    port=port,
                ),
                "Sign-in did not complete. Try again.",
            )
            if not result or "error" in result:
                error = _classify_error(result or {"error": "interaction_required"})
                self._log.add(
                    auth_log.FAILED, profile.name or profile.id, profile.method,
                    environment=str(environment_id), detail=str(error),
                    duration_ms=(perf_counter() - started) * 1000, ok=False,
                )
                raise error
            self._persist(session)
        self._log.add(
            auth_log.SIGNED_IN, profile.name or profile.id, profile.method,
            environment=str(environment_id),
            duration_ms=(perf_counter() - started) * 1000,
        )

    def renew(self, environment_id: str, profile: AuthProfile) -> None:
        """Forces a silent/app-only token refresh. Never opens a browser window."""
        profile.validate()
        started = perf_counter()
        if profile.method not in _OAUTH_METHODS:
            raise ValueError(
                "Manual credentials cannot be renewed automatically. Update the secret instead."
            )
        if profile.method == "client_credentials":
            self._client_credentials_token(environment_id, profile, force_refresh=True)
        elif profile.method == "oauth2":
            self._generic_oauth_token(environment_id, profile, force_refresh=True)
        else:
            self._silent_token(environment_id, profile, force_refresh=True)
        self._log.add(
            auth_log.REFRESHED, profile.name or profile.id, profile.method,
            environment=str(environment_id),
            duration_ms=(perf_counter() - started) * 1000,
        )

    # -- clearing / status ---------------------------------------------------

    def clear_session(self, environment_id: str, profile: AuthProfile) -> None:
        """Clears the local secret/cache only. Does not call the provider's logout."""
        self._log.add(
            auth_log.SIGNED_OUT, profile.name or profile.id, profile.method,
            environment=str(environment_id), detail="local session cleared",
        )
        self._secrets.pop((str(environment_id), profile.id), None)
        self._oauth2_tokens.pop((str(environment_id), profile.id), None)
        session_key = self._session_key(environment_id, profile)
        with self._registry_lock:
            session = self._sessions.pop(session_key, None)
        if session is not None and session.app is not None:
            try:
                accounts = session.app.get_accounts()
            except Exception:
                accounts = []
            for account in accounts:
                try:
                    session.app.remove_account(account)
                except Exception:
                    pass
        identity_key = self._identity_key(environment_id, profile)
        index_path = self._cache_root / "index.json"
        stored_session_key = None
        try:
            self._cache_root.mkdir(parents=True, exist_ok=True)
            with self._lock_factory(self._lock_path(index_path)):
                if index_path.exists():
                    index = json.loads(index_path.read_text(encoding="utf-8"))
                    stored_session_key = index.pop(identity_key, None)
                    index_path.write_text(json.dumps(index), encoding="utf-8")
        except Exception:
            raise AuthError("Unable to clear the saved authentication data.") from None
        keys_to_clear = {session_key}
        if stored_session_key:
            keys_to_clear.add(stored_session_key)
        if stored_session_key and stored_session_key != session_key:
            with self._registry_lock:
                stale_session = self._sessions.pop(stored_session_key, None)
            if stale_session is not None and stale_session.app is not None:
                try:
                    stale_accounts = stale_session.app.get_accounts()
                except Exception:
                    stale_accounts = []
                for account in stale_accounts:
                    try:
                        stale_session.app.remove_account(account)
                    except Exception:
                        pass
        paths = [
            path
            for key in keys_to_clear
            for path in (self._cache_path(key), self._secret_path(key))
        ]
        for path in paths:
            try:
                with self._lock_factory(self._lock_path(path)):
                    path.unlink(missing_ok=True)
            except Exception:
                raise AuthError("Unable to clear the saved authentication data.") from None


    def status(self, environment_id: str, profile: AuthProfile) -> str:
        """A short, sanitized human-readable status string."""
        try:
            profile.validate()
        except ValueError:
            return "not configured"
        if profile.method in ("manual_bearer", "api_key"):
            return "configured" if self._get_secret(environment_id, profile) else "not signed in"
        if profile.method == "client_credentials":
            if not self._get_secret(environment_id, profile):
                return "not configured"
            return self._client_credentials_status(environment_id, profile)
        if profile.method == "oauth2":
            return self._generic_oauth_status(environment_id, profile)
        if profile.method not in _MSAL_METHODS:
            # Credential and signing methods are ready as soon as every
            # required secret has been entered; there is no session to check.
            required = [
                f.name for f in get_strategy(profile.method).secret_fields if f.required
            ]
            if not required:
                return "configured"
            held = self._get_secrets(environment_id, profile)
            return "configured" if all(held.get(n) for n in required) else "not configured"
        return self._oauth_status(environment_id, profile)

    def _generic_oauth_status(self, environment_id: str, profile: AuthProfile) -> str:
        with self._registry_lock:
            token = self._oauth2_tokens.get((str(environment_id), profile.id))
        if token is None:
            grant = str(profile.options.get("grant_type") or "authorization_code")
            if grant in ("client_credentials", "password"):
                return "configured"
            return "not signed in"
        if token.is_expired() and not token.refresh_token:
            return "expired"
        return "signed in"

    def _oauth_status(self, environment_id: str, profile: AuthProfile) -> str:
        try:
            session = self._get_or_create_session(environment_id, profile, secret=None)
        except AuthError:
            return "not signed in"
        try:
            accounts = session.app.get_accounts()
        except Exception:
            return "not signed in"
        if not accounts:
            return "not signed in"
        account = accounts[0]
        username = str(account.get("username", "")) if isinstance(account, dict) else ""
        label = f"signed in as {username}" if username else "signed in"
        try:
            result = session.app.acquire_token_silent_with_error(
                list(profile.scopes), account=account
            )
        except Exception:
            result = None
        if not result or "error" in result:
            return f"{label} (renewal needed)"
        expires_in = result.get("expires_in")
        if isinstance(expires_in, (int, float)):
            remaining = max(0, int(expires_in))
            detail = "expired" if remaining <= 0 else f"expires in {remaining}s"
        else:
            detail = "expiry unknown"
        return f"{label} ({detail})"

    def _client_credentials_status(self, environment_id: str, profile: AuthProfile) -> str:
        secret = self._get_secret(environment_id, profile)
        try:
            session = self._get_or_create_session(environment_id, profile, secret=secret)
            result = _acquire_for_client(session.app, profile, force_refresh=False)
        except Exception:
            return "not signed in"
        if not result or "error" in result:
            return "not signed in"
        expires_in = result.get("expires_in")
        if isinstance(expires_in, (int, float)):
            remaining = max(0, int(expires_in))
            return "expired" if remaining <= 0 else f"ready (expires in {remaining}s)"
        return "ready"


_manager_lock = threading.Lock()
_manager: Optional[AuthenticationManager] = None


def get_auth_manager() -> AuthenticationManager:
    """Returns the process-wide :class:`AuthenticationManager` singleton."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AuthenticationManager()
    return _manager
