"""Merged endpoint documentation from catalog, C# XML comments, and OpenAPI caches."""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Any

from .catalog import Endpoint, Service


def xml_summary(source: Path, source_line: int) -> str:
    if not source.exists() or source_line <= 1:
        return ""
    lines = source.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    before = lines[max(0, source_line - 20) : source_line - 1]
    comments: list[str] = []
    for line in reversed(before):
        stripped = line.strip()
        if stripped.startswith("["):
            continue
        if not stripped.startswith("///"):
            break
        comments.append(stripped.removeprefix("///").strip())
    block = "\n".join(reversed(comments))
    match = re.search(r"<summary>\s*(.*?)\s*</summary>", block, re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", match.group(1)))).strip()


def openapi_operation(
    cache_directory: Path, endpoint: Endpoint
) -> dict[str, Any] | None:
    path = cache_directory / f"{endpoint.service}.json"
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    route = endpoint.path
    operation = document.get("paths", {}).get(route, {}).get(endpoint.method.lower())
    return operation if isinstance(operation, dict) else None


def endpoint_documentation(
    endpoint: Endpoint,
    service: Service,
    workspace_root: Path,
    openapi_directory: Path,
) -> str:
    source = workspace_root / service.repository / endpoint.source_file
    operation = openapi_operation(openapi_directory, endpoint) or {}
    summary = (
        str(operation.get("summary") or "").strip()
        or xml_summary(source, endpoint.source_line)
        or endpoint.action
    )
    description = str(operation.get("description") or "").strip()
    parameters = "\n".join(
        f"- {item.name} ({item.source}, {item.type})"
        f"{' - required' if item.required else ' - optional'}"
        for item in endpoint.parameters
    ) or "- No parameters"
    responses = operation.get("responses", {})
    response_text = "\n".join(
        f"- {status}: {details.get('description', '') if isinstance(details, dict) else ''}"
        for status, details in responses.items()
    ) or f"- Expected: {endpoint.expected_status}"
    sections = [
        summary,
        description,
        f"Service: {endpoint.service}",
        f"Controller: {endpoint.controller}",
        f"Method: {endpoint.method}",
        f"Route: {endpoint.path}",
        f"Payload type: {endpoint.payload_type or 'None'}",
        f"Source: {endpoint.source_file}:{endpoint.source_line}",
        f"Parameters\n{parameters}",
        f"Responses\n{response_text}",
    ]
    return "\n\n".join(section for section in sections if section)

