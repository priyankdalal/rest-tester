"""Multipart file metadata, stored alongside the existing form values."""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path

from .headers import merge_headers, validate_headers
from .request_auth import SECRET_HEADER_NAMES

FILE_PREFIX = "file:"
RESERVED_PART_HEADERS = frozenset({"content-type", "content-disposition", "content-length"})
_TOKEN = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"
_PARAMETER_VALUE = rf'(?:{_TOKEN}|"(?:[^"\\\r\n]|\\[^\r\n])*")'
_MEDIA_TYPE = re.compile(
    rf"{_TOKEN}/{_TOKEN}(?:[ \t]*;[ \t]*{_TOKEN}[ \t]*=[ \t]*{_PARAMETER_VALUE})*[ \t]*"
)


def part_header_name(key: str) -> str | None:
    if key.startswith(FILE_PREFIX) and ":header:" in key:
        return key.split(":header:", 1)[1].lower()
    return None


def safe_file_values(
    values: dict[str, str], secret_names: frozenset[str] = SECRET_HEADER_NAMES,
) -> dict[str, str]:
    return {
        key: value for key, value in values.items()
        if part_header_name(key) not in secret_names
    }


@dataclass(frozen=True)
class FileOptions:
    filename: str
    content_type: str
    headers: dict[str, str] = field(default_factory=dict)


def resolve_file_options(
    name: str, path: str, values: dict[str, str],
    omitted_headers: frozenset[str] = frozenset(),
) -> FileOptions:
    prefix = f"{FILE_PREFIX}{name}:"
    filename = values.get(prefix + "filename", "").strip() or Path(path).name
    if re.search(r'[\x00-\x1f\x7f]', filename):
        raise ValueError(f"Upload filename for {name} cannot contain control characters.")
    content_type = values.get(prefix + "content_type", "").strip()
    if not content_type:
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    validate_headers({"Content-Type": content_type})
    if not _MEDIA_TYPE.fullmatch(content_type):
        raise ValueError(f"Content type for {name} must be a media type such as application/pdf.")
    headers: dict[str, str] = {}
    for key, value in values.items():
        if not key.startswith(prefix + "header:"):
            continue
        header = key[len(prefix + "header:"):]
        if header.lower() in omitted_headers:
            continue
        if header.lower() in RESERVED_PART_HEADERS:
            raise ValueError(
                f"File {name}: {header} is generated automatically. "
                "Use the filename/content type fields; do not set multipart structural headers."
            )
        headers = merge_headers(headers, {header: value})
    validate_headers(headers)
    return FileOptions(filename, content_type, headers)
