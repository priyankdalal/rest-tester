"""Content-type classification and body formatting helpers.

Kept free of Qt so the rules can be unit tested directly.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.message import Message
from typing import Any
from urllib.parse import unquote, urlparse

#: Body categories the viewers know how to render.
TEXT = "text"
JSON = "json"
XML = "xml"
HTML = "html"
IMAGE = "image"
BINARY = "binary"
EMPTY = "empty"

_JSON_HINTS = ("json", "+json")
_XML_HINTS = ("xml", "+xml")
_TEXT_TYPES = {
    "text",
    "application/javascript",
    "application/x-www-form-urlencoded",
    "application/graphql",
}
#: Types that carry no real information about the payload.
_UNKNOWN_TYPES = {"application/octet-stream", "application/binary", "binary/octet-stream"}
MAX_FORMAT_BYTES = 5 * 1024 * 1024

EXTENSION_BY_TYPE = {
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "text/html": ".html",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/octet-stream": ".bin",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
}


def media_type(content_type: str) -> str:
    """Returns the bare media type, dropping parameters such as charset."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def charset_of(content_type: str, default: str = "utf-8") -> str:
    match = re.search(r"charset\s*=\s*\"?([\w\-]+)", content_type or "", re.IGNORECASE)
    return match.group(1).lower() if match else default


def looks_binary(payload: bytes) -> bool:
    """Heuristic for bodies whose content type is unhelpful (e.g. octet-stream)."""
    if not payload:
        return False
    if b"\x00" in payload[:8192]:
        return True
    try:
        payload[:8192].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def classify(content_type: str, payload: bytes) -> str:
    """Categorises a body from its declared type, falling back to sniffing."""
    if not payload:
        return EMPTY
    mime = media_type(content_type)
    if any(hint in mime for hint in _JSON_HINTS):
        return JSON
    if any(hint in mime for hint in _XML_HINTS):
        return XML
    if mime == "text/html":
        return HTML
    if mime.startswith("image/"):
        return IMAGE
    if mime.startswith("text/") or mime in _TEXT_TYPES:
        return TEXT
    if mime in _UNKNOWN_TYPES:
        # The server declined to say — sniff before assuming binary.
        return BINARY if looks_binary(payload) else TEXT
    if mime:
        return BINARY
    # No content type at all — sniff the bytes.
    if looks_binary(payload):
        return BINARY
    text = payload.lstrip()[:1]
    if text in (b"{", b"["):
        return JSON
    if text == b"<":
        return XML
    return TEXT


def filename_from_disposition(disposition: str) -> str:
    """Extracts a filename from a Content-Disposition header.

    Handles both ``filename="x"`` and RFC 5987 ``filename*=UTF-8''x`` forms.
    """
    if not disposition:
        return ""
    extended = re.search(
        r"filename\*\s*=\s*([\w\-]+)'([^']*)'([^;]+)", disposition, re.IGNORECASE
    )
    if extended:
        return sanitize_filename(unquote(extended.group(3).strip().strip('"')))
    message = Message()
    message["content-disposition"] = disposition
    value = message.get_filename()
    return sanitize_filename(value) if value else ""


def sanitize_filename(name: str) -> str:
    """Strips path separators and characters Windows rejects."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip().strip(".")
    return name[:180]


def suggested_filename(
    content_type: str, disposition: str, url: str = "", fallback: str = "response"
) -> str:
    """Best available download name: disposition, then URL, then a default."""
    from_disposition = filename_from_disposition(disposition)
    if from_disposition:
        return from_disposition

    candidate = ""
    if url:
        tail = sanitize_filename(unquote(urlparse(url).path.rsplit("/", 1)[-1]))
        if tail and "." in tail:
            candidate = tail
    if candidate:
        return candidate

    extension = EXTENSION_BY_TYPE.get(media_type(content_type), "")
    if not extension:
        mime = media_type(content_type)
        subtype = mime.split("/")[-1] if "/" in mime else ""
        subtype = subtype.split("+")[-1]
        extension = f".{subtype}" if re.fullmatch(r"[\w.\-]+", subtype or "") else ".bin"
    return f"{fallback}{extension}"


def human_size(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def decode_text(payload: bytes, content_type: str = "") -> str:
    """Decodes a text body, never raising on undecodable bytes."""
    try:
        return payload.decode(charset_of(content_type))
    except (UnicodeDecodeError, LookupError):
        return payload.decode("utf-8", errors="replace")


def pretty_json(text: str) -> tuple[str, str]:
    """Returns ``(formatted, error)`` — formatted is the input when invalid."""
    stripped = (text or "").strip()
    if not stripped:
        return "", ""
    try:
        document = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return text, f"Line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return json.dumps(document, indent=2, ensure_ascii=False), ""


def minify_json(text: str) -> tuple[str, str]:
    stripped = (text or "").strip()
    if not stripped:
        return "", ""
    try:
        document = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return text, f"Line {exc.lineno}, column {exc.colno}: {exc.msg}"
    return json.dumps(document, separators=(",", ":"), ensure_ascii=False), ""


def pretty_xml(text: str) -> tuple[str, str]:
    from xml.dom import minidom
    from xml.parsers.expat import ExpatError

    stripped = (text or "").strip()
    if not stripped:
        return "", ""
    try:
        parsed = minidom.parseString(stripped)
    except (ExpatError, ValueError) as exc:
        return text, str(exc)
    formatted = parsed.toprettyxml(indent="  ")
    lines = [line for line in formatted.splitlines() if line.strip()]
    return "\n".join(lines), ""


def describe_json(document: Any) -> str:
    """Short structural summary shown above a JSON body."""
    if isinstance(document, list):
        return f"array · {len(document)} item{'s' if len(document) != 1 else ''}"
    if isinstance(document, dict):
        return f"object · {len(document)} field{'s' if len(document) != 1 else ''}"
    if document is None:
        return "null"
    return type(document).__name__


@dataclass(frozen=True)
class BodyInfo:
    """Everything the viewers need to render or save a body."""

    kind: str
    content_type: str
    size: int
    filename: str
    text: str = ""
    error: str = ""

    @property
    def is_saveable(self) -> bool:
        return self.kind in (BINARY, IMAGE) or self.size > 0

    @property
    def is_binary(self) -> bool:
        return self.kind in (BINARY, IMAGE)


def inspect_body(
    payload: bytes, content_type: str, disposition: str = "", url: str = ""
) -> BodyInfo:
    """Classifies a body and pre-formats it when it is textual."""
    kind = classify(content_type, payload)
    filename = suggested_filename(content_type, disposition, url)
    if kind == EMPTY:
        return BodyInfo(kind, content_type, 0, filename)
    if kind in (BINARY, IMAGE):
        return BodyInfo(kind, content_type, len(payload), filename)

    text = decode_text(payload, content_type)
    error = ""
    if kind in (JSON, XML) and len(payload) > MAX_FORMAT_BYTES:
        error = (
            f"Formatting skipped because the response exceeds "
            f"{human_size(MAX_FORMAT_BYTES)}. Raw text remains available."
        )
    elif kind == JSON:
        text, error = pretty_json(text)
    elif kind == XML:
        text, error = pretty_xml(text)
    return BodyInfo(kind, content_type, len(payload), filename, text=text, error=error)


#: Header groups, so the response header table can be ordered meaningfully.
HEADER_GROUPS: dict[str, tuple[str, ...]] = {
    "Content": (
        "content-type",
        "content-length",
        "content-disposition",
        "content-encoding",
        "content-language",
        "content-range",
    ),
    "Caching": ("cache-control", "etag", "expires", "last-modified", "age", "vary", "pragma"),
    "Security": (
        "strict-transport-security",
        "x-content-type-options",
        "x-frame-options",
        "content-security-policy",
        "referrer-policy",
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "access-control-allow-headers",
        "access-control-allow-methods",
        "www-authenticate",
    ),
    "Tracing": (
        "request-id",
        "x-request-id",
        "correlation-id",
        "x-correlation-id",
        "traceparent",
        "x-trace-id",
        "x-azure-ref",
        "x-ms-request-id",
    ),
    "Rate limiting": (
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    ),
}


def group_headers(headers: dict[str, str]) -> list[tuple[str, list[tuple[str, str]]]]:
    """Buckets headers into meaningful groups, with the rest under 'Other'."""
    remaining = dict(headers)
    grouped: list[tuple[str, list[tuple[str, str]]]] = []
    for group, names in HEADER_GROUPS.items():
        rows = []
        for name in names:
            for key in list(remaining):
                if key.lower() == name:
                    rows.append((key, remaining.pop(key)))
        if rows:
            grouped.append((group, rows))
    if remaining:
        grouped.append(("Other", sorted(remaining.items(), key=lambda item: item[0].lower())))
    return grouped
