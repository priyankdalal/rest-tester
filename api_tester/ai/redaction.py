"""Strips secrets from any text before it is sent to a language model."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .provider import ChatMessage

MASK = "[REDACTED]"

_PATTERNS = (
    # JSON Web Tokens.
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
    # "Bearer xyz" / "Basic xyz".
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    # key=value secrets in connection strings and query strings.
    re.compile(
        r"(?i)\b(password|pwd|accountkey|sharedaccesskey|client_secret|api[_-]?key|access_token|sig)"
        r"\s*[=:]\s*[^;&\s\"']+"
    ),
    # Long hex or base64 runs (keys, signatures). Base64 must contain a digit so
    # long PascalCase identifiers from the catalog are not mistaken for keys.
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z+/]*\d)[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/=])"),
)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    if not text:
        return text
    # Longest first, so a secret that contains another is masked whole.
    for secret in sorted({item for item in secrets if item and len(item) >= 4}, key=len, reverse=True):
        text = text.replace(secret, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(_mask_match, text)
    return text


def _mask_match(match: re.Match[str]) -> str:
    groups = match.groups()
    if groups and groups[0]:
        separator = "=" if "=" in match.group(0) else (":" if ":" in match.group(0) else " ")
        return f"{groups[0]}{separator}{MASK}"
    return MASK


def redact_messages(messages: list[ChatMessage], secrets: Iterable[str] = ()) -> list[ChatMessage]:
    secrets = tuple(secrets)
    return [
        ChatMessage(
            role=message.role,
            content=redact_text(message.content, secrets),
            tool_call_id=message.tool_call_id,
            tool_calls=message.tool_calls,
        )
        for message in messages
    ]
