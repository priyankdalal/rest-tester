"""Persisted environment profiles.

Access tokens and API keys are entered as custom headers on an environment so
they travel with it. That means the settings document can contain secrets, so
the editor warns before saving and the request log still redacts them.

Environments can additionally hold managed authentication profiles
(``auth_profiles``) and per-service bindings (``auth_bindings``) implemented
by :mod:`api_tester.authentication`. Those are additive: existing manual
``custom_headers`` credentials keep working exactly as before, and an
environment with no managed profiles behaves exactly like the legacy shape.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .authentication import AuthBinding, AuthProfile


DEFAULT_ENVIRONMENT = "Development"

#: Header names treated as credentials: warned about, and always redacted.
SECRET_HEADERS = frozenset(
    {"authorization", "x-api-key", "api-key", "proxy-authorization"}
)


def header_value(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return ""


def has_secret_headers(headers: dict[str, str]) -> bool:
    return any(name.lower() in SECRET_HEADERS for name in headers)


def _timeout(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 30
    return max(1, min(3600, parsed))


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class EnvironmentProfile:
    name: str
    base_urls: dict[str, str] = field(default_factory=dict)
    verify_ssl: bool = True
    request_timeout: int = 30
    variables: dict[str, str] = field(default_factory=dict)
    custom_headers: dict[str, str] = field(default_factory=dict)
    #: Stable identity used to key managed secrets/sessions in the
    #: authentication manager. Persisted once assigned; a clone gets a fresh
    #: one so it never shares another environment's stored secrets/session.
    id: str = field(default_factory=_new_id)
    #: Managed authentication profiles, keyed by ``AuthProfile.id``.
    auth_profiles: dict[str, AuthProfile] = field(default_factory=dict)
    #: Per-service bindings, keyed by service name.
    auth_bindings: dict[str, AuthBinding] = field(default_factory=dict)

    @property
    def access_token(self) -> str:
        return header_value(self.custom_headers, "Authorization")

    @property
    def api_key(self) -> str:
        return header_value(self.custom_headers, "x-api-key")

    @classmethod
    def from_dict(cls, name: str, value: dict[str, Any]) -> "EnvironmentProfile":
        auth_profiles: dict[str, AuthProfile] = {}
        for item in value.get("auth_profiles", {}).values():
            if not isinstance(item, dict):
                continue
            profile = AuthProfile.from_dict(item)
            auth_profiles[profile.id] = profile
        auth_bindings = {
            str(service): AuthBinding.from_dict(item)
            for service, item in value.get("auth_bindings", {}).items()
            if isinstance(item, dict)
        }
        return cls(
            name=name,
            base_urls={
                str(service): str(url)
                for service, url in value.get("base_urls", {}).items()
            },
            verify_ssl=bool(value.get("verify_ssl", True)),
            request_timeout=_timeout(value.get("request_timeout", 30)),
            variables={
                str(name): str(item)
                for name, item in value.get("variables", {}).items()
            },
            custom_headers={
                str(name): str(item)
                for name, item in value.get("custom_headers", {}).items()
            },
            # A legacy document has no "id": mint one now. Once this profile
            # is saved back, the same id round-trips, so managed
            # secrets/sessions keyed on it stay valid across restarts.
            id=str(value.get("id") or _new_id()),
            auth_profiles=auth_profiles,
            auth_bindings=auth_bindings,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_urls": dict(self.base_urls),
            "verify_ssl": self.verify_ssl,
            "request_timeout": self.request_timeout,
            "variables": dict(self.variables),
            "custom_headers": dict(self.custom_headers),
            "id": self.id,
            "auth_profiles": {
                profile_id: profile.to_dict()
                for profile_id, profile in self.auth_profiles.items()
            },
            "auth_bindings": {
                service: binding.to_dict()
                for service, binding in self.auth_bindings.items()
            },
        }

    def clone(self, name: str) -> "EnvironmentProfile":
        """Returns a copy named ``name`` with a fresh environment id.

        Authentication profile and binding *configuration* (method,
        authority, client id, scopes, and so on) is deep-copied so the new
        environment starts pre-wired. Secrets and signed-in sessions never
        follow: the authentication manager keys them by environment id, and
        this clone gets a brand new one, so nothing needs to be scrubbed.
        Callers that want to tell the user about this should check
        ``bool(clone.auth_profiles)`` and show a hint to sign in again.
        """
        return EnvironmentProfile(
            name,
            dict(self.base_urls),
            self.verify_ssl,
            self.request_timeout,
            dict(self.variables),
            dict(self.custom_headers),
            _new_id(),
            copy.deepcopy(self.auth_profiles),
            copy.deepcopy(self.auth_bindings),
        )


@dataclass
class AppSettings:
    environments: dict[str, EnvironmentProfile]
    active_environment: str = DEFAULT_ENVIRONMENT
    active_service: str = ""
    theme: str = "Light"
    #: Absolute path of a catalog loaded from Settings; empty uses the bundled one.
    catalog_path: str = ""
    splitter_sizes: list[int] = field(default_factory=lambda: [330, 1170])
    request_response_sizes: list[int] = field(default_factory=lambda: [560, 340])
    request_builder_sizes: list[int] = field(default_factory=lambda: [360, 760])
    endpoint_column_widths: list[int] = field(default_factory=lambda: [255, 65])
    accordion_states: dict[str, bool] = field(default_factory=dict)
    expanded_nodes: list[str] = field(default_factory=list)
    favorites: list[str] = field(default_factory=list)
    recent_endpoints: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        service_defaults: dict[str, str],
    ) -> "AppSettings":
        environment_values = value.get("environments")
        if isinstance(environment_values, dict) and environment_values:
            environments = {
                str(name): EnvironmentProfile.from_dict(str(name), item)
                for name, item in environment_values.items()
                if isinstance(item, dict)
            }
        else:
            # Migration from the original single-environment settings shape.
            urls = dict(service_defaults)
            urls.update(
                {
                    str(name): str(url)
                    for name, url in value.get("base_urls", {}).items()
                }
            )
            environments = {
                DEFAULT_ENVIRONMENT: EnvironmentProfile(
                    name=DEFAULT_ENVIRONMENT,
                    base_urls=urls,
                    verify_ssl=bool(value.get("verify_ssl", True)),
                    request_timeout=_timeout(value.get("request_timeout", 30)),
                )
            }

        if not environments:
            environments = {
                DEFAULT_ENVIRONMENT: EnvironmentProfile(
                    DEFAULT_ENVIRONMENT, dict(service_defaults)
                )
            }
        for profile in environments.values():
            for service, default_url in service_defaults.items():
                profile.base_urls.setdefault(service, default_url)

        active = str(value.get("active_environment", DEFAULT_ENVIRONMENT))
        if active not in environments:
            active = next(iter(environments))
        return cls(
            environments=environments,
            active_environment=active,
            active_service=str(value.get("active_service", "")),
            theme=str(value.get("theme", "Light")),
            catalog_path=str(value.get("catalog_path", "")),
            splitter_sizes=[
                int(item) for item in value.get("splitter_sizes", [330, 1170])
            ],
            request_response_sizes=[
                int(item)
                for item in value.get("request_response_sizes", [560, 340])
            ],
            request_builder_sizes=[
                int(item)
                for item in value.get("request_builder_sizes", [360, 760])
            ],
            endpoint_column_widths=[
                int(item)
                for item in value.get("endpoint_column_widths", [255, 65])
            ],
            accordion_states={
                str(name): bool(expanded)
                for name, expanded in value.get("accordion_states", {}).items()
            },
            expanded_nodes=[
                str(item) for item in value.get("expanded_nodes", [])
            ],
            favorites=[str(item) for item in value.get("favorites", [])],
            recent_endpoints=[
                str(item) for item in value.get("recent_endpoints", [])
            ],
        )

    @property
    def active(self) -> EnvironmentProfile:
        return self.environments[self.active_environment]

    def to_dict(self) -> dict[str, Any]:
        document = {
            "active_environment": self.active_environment,
            "active_service": self.active_service,
            "theme": self.theme,
            "catalog_path": self.catalog_path,
            "environments": {
                name: profile.to_dict()
                for name, profile in self.environments.items()
            },
            "splitter_sizes": list(self.splitter_sizes),
            "request_response_sizes": list(self.request_response_sizes),
            "request_builder_sizes": list(self.request_builder_sizes),
            "endpoint_column_widths": list(self.endpoint_column_widths),
            "accordion_states": dict(self.accordion_states),
            "expanded_nodes": list(self.expanded_nodes),
            "favorites": list(self.favorites),
            "recent_endpoints": list(self.recent_endpoints),
        }
        # Keep the original fields as read-compatible aliases for existing scripts.
        document.update(self.active.to_dict())
        return document


def validate_base_url(value: str) -> tuple[bool, str]:
    """Validates URL shape without making a network request."""
    text = value.strip()
    if not text:
        return False, "No base URL"
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False, "Invalid URL"
    return True, "Configured"
