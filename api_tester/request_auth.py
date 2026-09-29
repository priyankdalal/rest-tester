"""Immutable per-run authentication bindings shared by Explorer and suites."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .auth_strategies import RequestContext, get_strategy
from .authentication import (
    AuthBinding, AuthError, AuthProfile, AuthenticationManager, get_auth_manager,
)

SECRET_HEADER_NAMES = frozenset({
    "authorization", "proxy-authorization", "x-api-key", "api-key",
    "x-functions-key", "ocp-apim-subscription-key", "cookie", "set-cookie",
})
AUTH_CHOICES = (
    ("Use service authentication", "inherit"),
    ("No authentication (negative tests)", "none"),
    ("Manual headers only (no renewal)", "manual"),
)


@dataclass
class AppliedAuthentication:
    headers: dict[str, str] = field(repr=False)
    secret_names: frozenset[str] = SECRET_HEADER_NAMES
    secrets: tuple[str, ...] = field(default=(), repr=False)
    managed: bool = False
    renewable: bool = False
    #: Query parameters the method places on the URL, for API keys sent as
    #: query arguments rather than headers.
    query: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    #: A ``requests`` auth callable for challenge-based methods (Digest),
    #: which cannot produce a credential until the server has replied.
    request_auth: object = field(default=None, repr=False)

    def redact(self, text: str) -> str:
        for value in sorted(self.secrets, key=len, reverse=True):
            if value:
                text = text.replace(value, "******")
        return text


class AuthenticationContext:
    def __init__(self, environment_id: str, profiles: dict[str, AuthProfile],
                 bindings: dict[str, AuthBinding], base_urls: dict[str, str],
                 manager: AuthenticationManager | None = None) -> None:
        self.environment_id = environment_id
        self.profiles = deepcopy(profiles)
        self.bindings = deepcopy(bindings)
        self.base_urls = dict(base_urls)
        self.manager = manager if manager is not None else get_auth_manager()

    @classmethod
    def from_environment(cls, environment, manager=None):
        return cls(environment.id, environment.auth_profiles, environment.auth_bindings,
                   environment.base_urls, manager)

    @property
    def secret_names(self) -> frozenset[str]:
        """Header names whose values must never be echoed back to the user.

        Every managed header of every configured profile counts, not just the
        API-key one, so a method with a non-standard header still redacts.
        """
        managed: set[str] = set()
        for profile in self.profiles.values():
            try:
                managed.update(
                    name.lower() for name in get_strategy(profile.method).header_names(profile)
                )
            except (KeyError, ValueError):
                continue
        return SECRET_HEADER_NAMES | frozenset(managed)

    def is_disabled(self, service: str, mode: str) -> bool:
        return mode == "none" or (
            mode == "inherit" and self.bindings.get(service, AuthBinding()).disabled
        )

    def _selected(self, service: str, mode: str) -> list[AuthProfile]:
        if mode in {"none", "manual"} or self.is_disabled(service, mode):
            return []
        binding = self.bindings.get(service, AuthBinding())
        identity = binding.identity if mode == "inherit" else mode
        selected = []
        for reference, is_key in ((identity, False), (binding.api_key, True)):
            if not reference:
                continue
            profile = self.profiles.get(reference)
            if profile is None:
                raise AuthError("Authentication profile is missing in this environment. Check the service binding.")
            try:
                profile.validate()
            except ValueError as exc:
                raise AuthError(f"Invalid authentication profile: {exc}") from None
            if get_strategy(profile.method).is_api_key_like != is_key:
                raise AuthError("Invalid identity/API-key binding. Check Environment authentication settings.")
            selected.append(profile)
        return selected

    def managed_header_names(self, service: str, mode: str = "inherit") -> frozenset[str]:
        return frozenset(
            name.lower()
            for profile in self._selected(service, mode)
            for name in get_strategy(profile.method).header_names(profile)
        )

    def managed_query_names(self, service: str, mode: str = "inherit") -> frozenset[str]:
        """Query parameter names managed authentication will add to the URL."""
        return frozenset(
            name
            for profile in self._selected(service, mode)
            for name in get_strategy(profile.method).query_names(profile)
        )

    def apply(self, service: str, url: str, headers: dict[str, str], mode: str = "inherit",
              *, preview: bool = False, force_refresh: bool = False,
              context: RequestContext | None = None) -> AppliedAuthentication:
        """Produces the headers, query and auth callable for one request.

        ``context`` carries the finalized method/url/body and is required by
        the signing methods, which hash the request itself. The origin,
        transport and conflict checks below run for every method, including
        the challenge-based ones whose credential is produced later inside
        ``requests`` -- those would otherwise escape this gate entirely.
        """
        if self.is_disabled(service, mode):
            return AppliedAuthentication({
                name: value for name, value in headers.items()
                if name.lower() not in self.secret_names
            }, self.secret_names)
        profiles = self._selected(service, mode)
        if not profiles:
            return AppliedAuthentication(dict(headers), self.secret_names)
        target = urlsplit(url)
        allowed = urlsplit(self.base_urls.get(service, ""))
        if (target.scheme, target.netloc) != (allowed.scheme, allowed.netloc):
            raise AuthError("Request origin differs from the configured service. Credentials were not sent.")
        if target.username or target.password or (
            target.scheme != "https" and target.hostname not in {"localhost", "127.0.0.1", "::1"}
        ):
            raise AuthError("Managed credentials require HTTPS (except loopback development services).")

        strategies = [get_strategy(profile.method) for profile in profiles]
        names = [
            name
            for profile, strategy in zip(profiles, strategies)
            for name in strategy.header_names(profile)
        ]
        existing = {name.lower() for name, value in headers.items() if value.strip()}
        if len({name.lower() for name in names}) != len(names) or existing.intersection(
            name.lower() for name in names
        ):
            raise AuthError(
                "A manual header conflicts with managed authentication. Remove the conflicting "
                "header or select Manual headers only."
            )

        output = dict(headers)
        secrets: list[str] = []
        query: list[tuple[str, str]] = []
        request_auth = None
        # Methods that place a credential in the query string run first so a
        # co-applied signing method covers those parameters in its signature.
        ordered = sorted(
            zip(profiles, strategies), key=lambda pair: pair[1].requires_request_context
        )
        for profile, strategy in ordered:
            if preview:
                output.update({name: "******" for name in strategy.header_names(profile)})
                query.extend((name, "******") for name in strategy.query_names(profile))
                continue
            outcome = self.manager.credentials(
                self.environment_id, profile,
                context=_with_query(context, query), force_refresh=force_refresh,
            )
            output.update(outcome.headers)
            query.extend(outcome.query)
            secrets.extend(value for value in outcome.secrets if value)
            secrets.extend(value for value in outcome.headers.values() if value)
            secrets.extend(value for _, value in outcome.query if value)
            if outcome.request_auth is not None:
                if request_auth is not None:
                    raise AuthError(
                        "Only one challenge-based authentication method can be used per request."
                    )
                request_auth = outcome.request_auth
        return AppliedAuthentication(
            output, self.secret_names, tuple(dict.fromkeys(secrets)), True,
            any(strategy.is_token_based for strategy in strategies),
            tuple(query), request_auth,
        )


def _with_query(
    context: RequestContext | None, additions: list[tuple[str, str]]
) -> RequestContext | None:
    """Folds already-applied auth query parameters into the signing context."""
    if context is None or not additions:
        return context
    split = urlsplit(context.url)
    merged = parse_qsl(split.query, keep_blank_values=True) + list(additions)
    return replace(context, url=urlunsplit(split._replace(query=urlencode(merged))))


def populate_auth_choices(combo, profiles: dict[str, AuthProfile], selected: str = "inherit") -> None:
    blocked = combo.blockSignals(True)
    combo.clear()
    for label, value in AUTH_CHOICES:
        combo.addItem(label, value)
    for profile in profiles.values():
        if profile.method != "api_key":
            combo.addItem(profile.name, profile.id)
    index = combo.findData(selected)
    if index < 0:
        combo.addItem("Missing profile (configure in Settings)", selected)
        index = combo.count() - 1
    combo.setCurrentIndex(index)
    combo.blockSignals(blocked)
