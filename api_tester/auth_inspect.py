"""Answers "what credential is actually being sent right now?".

The request path deliberately hides credential values: they are redacted
from generated cURL, diagnostics, captured variables, and run history,
because those artifacts get exported, committed, and shared. That is the
right default, but it leaves a tester debugging a 401/403 with nothing to
look at.

This module is the one place that deliberately does not redact. It is only
ever driven by an explicit user action in the inspector panel, and its
output is never written to disk or folded into an export.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .auth_strategies import RequestContext, get_strategy


@dataclass(frozen=True)
class CredentialValue:
    """One name/value pair a method contributes to a request."""

    name: str
    value: str
    placement: str = "header"


@dataclass(frozen=True)
class TokenClaims:
    """Decoded JWT payload, when the credential happens to be a JWT."""

    header: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def expires_at(self) -> Optional[float]:
        value = self.payload.get("exp")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def issued_at(self) -> Optional[float]:
        value = self.payload.get("iat")
        return float(value) if isinstance(value, (int, float)) else None

    def expires_in(self, now: Optional[float] = None) -> Optional[float]:
        expiry = self.expires_at
        if expiry is None:
            return None
        return expiry - (time.time() if now is None else now)

    @property
    def subject(self) -> str:
        for key in ("sub", "oid", "appid", "azp", "client_id"):
            value = self.payload.get(key)
            if value:
                return str(value)
        return ""

    @property
    def scopes(self) -> tuple[str, ...]:
        raw = self.payload.get("scp") or self.payload.get("scope") or ""
        if isinstance(raw, list):
            return tuple(str(item) for item in raw)
        return tuple(str(raw).split()) if raw else ()

    @property
    def roles(self) -> tuple[str, ...]:
        raw = self.payload.get("roles") or []
        return tuple(str(item) for item in raw) if isinstance(raw, list) else ()


@dataclass
class CredentialSnapshot:
    """Everything the inspector can say about one profile right now."""

    profile: str
    method: str
    method_label: str
    status: str = ""
    values: tuple[CredentialValue, ...] = ()
    config: tuple[tuple[str, str], ...] = ()
    claims: Optional[TokenClaims] = None
    error: str = ""
    #: True when the method computes a fresh credential per request, so the
    #: values shown are a representative sample rather than a stored one.
    per_request: bool = False

    @property
    def ok(self) -> bool:
        return not self.error


def _b64_segment(segment: str) -> bytes:
    padded = segment + "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def decode_jwt(token: str) -> Optional[TokenClaims]:
    """Decodes a JWT's header/payload without verifying its signature.

    Verification is intentionally skipped: the inspector reports what the
    provider issued so a tester can compare claims against the API's
    expectations. It is a read-only diagnostic, never an access decision.
    """
    candidate = token.strip()
    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip()
    parts = candidate.split(".")
    if len(parts) != 3:
        return None
    try:
        header = json.loads(_b64_segment(parts[0]))
        payload = json.loads(_b64_segment(parts[1]))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return None
    return TokenClaims(header=header, payload=payload)


def _sample_context(strategy) -> Optional[RequestContext]:
    """A representative request for the methods that sign one.

    A signing method cannot produce a credential without a request, so the
    inspector supplies a clearly-labelled placeholder rather than refusing
    to show anything. The resulting signature is valid only for this sample.
    """
    if not strategy.requires_request_context:
        return None
    return RequestContext(method="GET", url="https://example.invalid/inspector")


def inspect(
    manager,
    environment_id: str,
    profile,
    *,
    service: str = "",
) -> CredentialSnapshot:
    """Reports the credential ``profile`` would contribute right now.

    Never opens a browser: it reuses whatever session already exists and
    reports the provider's own error when interaction is required.
    """
    strategy = get_strategy(profile.method)
    snapshot = CredentialSnapshot(
        profile=profile.name or profile.id,
        method=profile.method,
        method_label=strategy.label,
        per_request=strategy.requires_request_context or strategy.is_challenge_based,
        config=_config_rows(profile, strategy),
    )
    try:
        snapshot.status = manager.status(environment_id, profile)
    except Exception as exc:  # pragma: no cover - status is best effort
        snapshot.status = f"unknown ({exc})"

    if strategy.is_challenge_based:
        snapshot.error = (
            f"{strategy.label} answers a server challenge, so no credential "
            "exists until the request has been sent once."
        )
        return snapshot

    try:
        outcome = manager.credentials(
            environment_id, profile, context=_sample_context(strategy)
        )
    except Exception as exc:
        snapshot.error = str(exc)
        return snapshot

    values = [
        CredentialValue(name, value, "header")
        for name, value in outcome.headers.items()
    ]
    values.extend(
        CredentialValue(name, value, "query") for name, value in outcome.query
    )
    snapshot.values = tuple(values)
    for value in values:
        claims = decode_jwt(value.value)
        if claims is not None:
            snapshot.claims = claims
            break
    return snapshot


def _config_rows(profile, strategy) -> tuple[tuple[str, str], ...]:
    """The non-secret parameters in play, as the method declares them."""
    rows: list[tuple[str, str]] = []
    for field_def in strategy.config_fields:
        raw = getattr(profile, field_def.name, None)
        if raw is None:
            raw = profile.options.get(field_def.name, field_def.default)
        if isinstance(raw, (list, tuple)):
            raw = ", ".join(str(item) for item in raw)
        text = str(raw if raw is not None else "")
        if text:
            rows.append((field_def.label, text))
    return tuple(rows)
