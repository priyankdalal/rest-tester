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

#: Manager status prefixes that mean a method's own requirements are satisfied:
#: a stored key for API-key methods, a live session for token methods, and
#: every required secret for the signing methods.
_READY_PREFIXES = ("configured", "signed in", "ready")
#: Substrings that disqualify an otherwise-ready status, because the credential
#: exists but can no longer be produced without interaction.
_STALE_MARKERS = ("expired", "renewal needed")


def status_is_ready(status: str) -> bool:
    """Whether an :meth:`AuthenticationManager.status` string means "usable now"."""
    text = status.strip().lower()
    if not text.startswith(_READY_PREFIXES):
        return False
    return not any(marker in text for marker in _STALE_MARKERS)


@dataclass(frozen=True)
class ConnectionState:
    """Whether a service can produce a credential for the next request."""

    connected: bool
    #: Short reason, shown when the pill itself cannot carry the nuance.
    summary: str = ""
    #: Per-profile status lines, suitable for a tooltip.
    detail: str = ""

    @property
    def label(self) -> str:
        return "Connected" if self.connected else "Disconnected"


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

    def connection_state(
        self,
        service: str,
        mode: str = "inherit",
        manual_headers: dict[str, str] | None = None,
    ) -> ConnectionState:
        """Whether ``service`` can produce a credential right now under ``mode``.

        Readiness is decided by the selected method's own schema rather than by
        a generic reachability probe: an API-key method needs its key stored, a
        token method needs a live session, and a signing method needs every
        required secret. ``AuthenticationManager.status`` is the single source
        of truth for that per-method rule, so this never re-implements it.

        This can block (a token method may check its session), so callers on a
        GUI thread should run it in a worker.
        """
        if mode == "none":
            return ConnectionState(
                False,
                "Authentication off",
                "This request is sent without credentials (negative test).",
            )
        if self.is_disabled(service, mode):
            return ConnectionState(
                False,
                "Disabled for this service",
                f"Authentication is switched off for {service} in this environment.",
            )
        if mode == "manual":
            present = sorted(
                name
                for name, value in (manual_headers or {}).items()
                if value.strip() and name.lower() in SECRET_HEADER_NAMES
            )
            if present:
                return ConnectionState(
                    True,
                    "Manual headers",
                    "Manual credential headers are set and sent as-is (never renewed): "
                    + ", ".join(present),
                )
            return ConnectionState(
                False,
                "No manual credential",
                "Manual headers only is selected, but this environment has no "
                "credential header such as Authorization or x-api-key.",
            )
        try:
            profiles = self._selected(service, mode)
        except AuthError as exc:
            return ConnectionState(False, "Configuration error", str(exc))
        if not profiles:
            return ConnectionState(
                False,
                "No authentication bound",
                f"No authentication profile is bound to {service}. "
                "Bind one under Environments > Authentication.",
            )
        lines: list[str] = []
        connected = True
        for profile in profiles:
            label = get_strategy(profile.method).label
            try:
                status = self.manager.status(self.environment_id, profile)
            except AuthError as exc:
                status = str(exc)
            except Exception as exc:  # pragma: no cover - status is best effort
                status = f"unknown ({exc})"
            connected = connected and status_is_ready(status)
            lines.append(f"{label} — {profile.name}: {status}")
        return ConnectionState(connected, "", "\n".join(lines))

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
