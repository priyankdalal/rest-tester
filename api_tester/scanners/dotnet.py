"""ASP.NET Core scanner.

Delegates to :mod:`tools.generate_catalog`, which already resolves generic base
controllers, inherited actions, route templates, DTO payload schemas, form
schemas, and ``[Filterable]``/``[Sort]`` metadata.  Reusing it means a project
scanned from the builder produces exactly what the committed catalog contains.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import DiscoveredEndpoint, ScanResult, relative_to


#: Folder names that indicate a shared class library in the same solution.
SHARED_LIBRARY_HINTS = ("packages", "shared", "common", "core", "domain", "contracts")


def find_shared_root(root: Path) -> Path | None:
    """Finds a sibling or child class library that holds shared DTOs."""
    candidates: list[Path] = []
    for sibling in (root.parent, root):
        if not sibling.exists():
            continue
        try:
            entries = sorted(entry for entry in sibling.iterdir() if entry.is_dir())
        except OSError:
            continue
        for entry in entries:
            if entry.resolve() == root.resolve():
                continue
            lowered = entry.name.lower()
            if any(hint in lowered for hint in SHARED_LIBRARY_HINTS):
                candidates.append(entry)
    return candidates[0] if candidates else None


def scan(root: Path, service_name: str) -> ScanResult:
    from tools.generate_catalog import scan_dotnet_project

    result = ScanResult(framework="dotnet", root=root)
    shared_root = find_shared_root(root)
    scanned: dict[str, Any] = scan_dotnet_project(service_name, root, shared_root)

    for item in scanned["endpoints"]:
        source_file = item.get("source_file", "")
        result.endpoints.append(
            DiscoveredEndpoint(
                service=service_name,
                controller=item["controller"],
                action=item["action"],
                method=item["method"],
                path=item["path"],
                parameters=list(item.get("parameters", [])),
                payload=item.get("payload"),
                payload_type=item.get("payload_type"),
                payload_schema=item.get("payload_schema"),
                form_schema=item.get("form_schema"),
                response_schema=item.get("response_schema"),
                filter_entity=item.get("filter_entity"),
                source_file=source_file,
                source_line=item.get("source_line", 0),
            )
        )
    result.filter_schemas = dict(scanned["filter_schemas"])
    result.payload_schemas = dict(scanned["payload_schemas"])
    result.form_schemas = dict(scanned["form_schemas"])
    result.response_schemas = dict(scanned.get("response_schemas", {}))
    result.enums = dict(scanned["enums"])
    if shared_root is not None:
        result.warnings.append(
            f"Resolved shared types from {relative_to(shared_root, root.parent)}."
        )
    if not result.endpoints:
        result.warnings.append(
            "No public *Controller classes with Http* actions were found. "
            "Point the scan at the project folder that contains Controllers/."
        )
    return result
