"""Shared types and helpers for the framework scanners."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


#: Directories that never contain first-party route definitions.
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vs",
        ".vscode",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        "node_modules",
        "bower_components",
        "vendor",
        "bin",
        "obj",
        "dist",
        "build",
        "out",
        "target",
        "coverage",
        "migrations",
        "site-packages",
    }
)

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

_BRACE_PARAMETER = re.compile(r"\{([^{}:?]+)")
_COLON_PARAMETER = re.compile(r"[:<]([A-Za-z_][\w]*)")
_ANGLE_PARAMETER = re.compile(r"<(?:[^:<>]+:)?([A-Za-z_][\w]*)>")

#: ``<int:trial_id>`` and ``<slug>`` used by Flask and Django ``path()``.
_ANGLE_SYNTAX = re.compile(r"<(?:[^:<>]+:)?([A-Za-z_][\w]*)>")
#: ``:productId`` used by Express, Slim, and Laravel-lite routers.
_COLON_SYNTAX = re.compile(r"(?<=/):([A-Za-z_][\w]*)\??")
#: ``(?P<report_id>[0-9]+)`` used by Django ``re_path`` and raw regex routes.
_NAMED_GROUP = re.compile(r"\(\?P<([A-Za-z_][\w]*)>[^)]*\)")
#: Unnamed regex groups, which cannot be filled in and become a wildcard.
_UNNAMED_GROUP = re.compile(r"\([^()]*\)")


@dataclass
class DiscoveredEndpoint:
    """One HTTP endpoint found in source, in catalog field order."""

    service: str
    controller: str
    action: str
    method: str
    path: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    payload: Any = None
    payload_type: str | None = None
    payload_schema: str | None = None
    form_schema: str | None = None
    response_schema: str | None = None
    filter_entity: str | None = None
    expected_status: str = "200-299"
    source_file: str = ""
    source_line: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": endpoint_identity(
                self.service, self.controller, self.method, self.path, self.action
            ),
            "service": self.service,
            "controller": self.controller,
            "action": self.action,
            "method": self.method,
            "path": self.path,
            "parameters": [dict(item) for item in self.parameters],
            "payload": self.payload,
            "payload_type": self.payload_type,
            "payload_schema": self.payload_schema,
            "form_schema": self.form_schema,
            "response_schema": self.response_schema,
            "filter_entity": self.filter_entity,
            "expected_status": self.expected_status,
            "source_file": self.source_file,
            "source_line": self.source_line,
        }


@dataclass
class ScanResult:
    """Everything a single project scan contributes to a catalog."""

    framework: str
    root: Path
    endpoints: list[DiscoveredEndpoint] = field(default_factory=list)
    payload_schemas: dict[str, Any] = field(default_factory=dict)
    filter_schemas: dict[str, Any] = field(default_factory=dict)
    form_schemas: dict[str, Any] = field(default_factory=dict)
    response_schemas: dict[str, Any] = field(default_factory=dict)
    enums: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.endpoints)

    def sorted_endpoints(self) -> list[DiscoveredEndpoint]:
        return sorted(
            self.endpoints,
            key=lambda item: (item.controller, item.path, item.method, item.action),
        )


def endpoint_identity(
    service: str, controller: str, method: str, path: str, action: str
) -> str:
    """Stable 12-character id, matching ``tools.generate_catalog``."""
    identity = f"{service}:{controller}Controller:{method}:{path}:{action}"
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]


def normalise_path(*segments: str) -> str:
    """Joins route segments into a single leading-slash path."""
    parts: list[str] = []
    for segment in segments:
        if not segment:
            continue
        for piece in str(segment).replace("\\", "/").split("/"):
            piece = piece.strip()
            if piece and piece != ".":
                parts.append(piece)
    return "/" + "/".join(parts) if parts else "/"


def canonical_path(*segments: str) -> str:
    """Joins segments and rewrites every route syntax to ``{name}``.

    The request builder in :mod:`api_tester.client` substitutes path values by
    matching ``{name}`` only, so a scanner that emitted ``:id``, ``<int:id>``
    or a regex group would produce endpoints whose path could never be filled
    in. Every scanner must therefore return canonical paths.
    """
    path = normalise_path(*segments)
    path = _NAMED_GROUP.sub(lambda match: "{" + match.group(1) + "}", path)
    path = _ANGLE_SYNTAX.sub(lambda match: "{" + match.group(1) + "}", path)
    path = _COLON_SYNTAX.sub(lambda match: "{" + match.group(1) + "}", path)
    path = _UNNAMED_GROUP.sub("", path)
    path = path.replace("^", "").replace("$", "").replace("\\", "")
    path = re.sub(r"\{([^{}]*)\}", lambda m: "{" + m.group(1).strip("?*+ ") + "}", path)
    return normalise_path(path)


def path_parameters(path: str) -> list[dict[str, Any]]:
    """Extracts path parameters from any of the common route syntaxes."""
    names: list[str] = []
    for pattern in (_BRACE_PARAMETER, _ANGLE_PARAMETER):
        names.extend(pattern.findall(path))
    if not names:
        # ``/users/:id`` style used by Express, Laravel-lite routers, and Slim.
        names.extend(
            name for name in _COLON_PARAMETER.findall(path) if not name.isdigit()
        )
    seen: list[str] = []
    for name in names:
        clean = name.strip().lstrip("*").rstrip("?")
        if clean and clean not in seen:
            seen.append(clean)
    return [
        {"name": name, "source": "path", "type": "string", "required": True}
        for name in seen
    ]


def iter_source_files(root: Path, suffixes: Iterable[str]) -> Iterator[Path]:
    """Walks ``root`` yielding files with the given suffixes, skipping noise."""
    wanted = {suffix.lower() for suffix in suffixes}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        if any(part in IGNORED_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() in wanted:
            yield path


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def balanced_segment(text: str, open_index: int) -> str:
    """Returns the text inside the parentheses starting at ``open_index``.

    Handler signatures routinely contain nested calls -- a Spring
    ``@RequestParam(required = false) String region`` or a NestJS
    ``@Query('include') include: string`` -- so truncating at the first ``)``
    silently drops every parameter after the first decorator.
    """
    if open_index < 0 or open_index >= len(text) or text[open_index] != "(":
        return ""
    depth = 0
    for index in range(open_index, len(text)):
        character = text[index]
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return text[open_index + 1 : index]
    return text[open_index + 1 :]


def line_of(text: str, index: int) -> int:    return text.count("\n", 0, max(index, 0)) + 1


def relative_to(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def titleize(value: str) -> str:
    """Turns ``user_profile`` or ``user-profile`` into ``UserProfile``."""
    pieces = re.split(r"[^0-9A-Za-z]+", value)
    return "".join(piece[:1].upper() + piece[1:] for piece in pieces if piece)


def controller_from_path(path: str, fallback: str = "Root") -> str:
    """Derives a module name from the first literal segment of a route."""
    for piece in path.strip("/").split("/"):
        if piece and not piece.startswith((":", "{", "<", "*")):
            return titleize(piece) or fallback
    return fallback


def action_from(method: str, path: str, name: str = "") -> str:
    if name:
        return titleize(name) if "_" in name or "-" in name else name
    tail = [
        piece
        for piece in path.strip("/").split("/")
        if piece and not piece.startswith((":", "{", "<", "*"))
    ]
    suffix = titleize(tail[-1]) if tail else "Index"
    return f"{method.capitalize()}{suffix}"
