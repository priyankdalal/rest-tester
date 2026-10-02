"""Scanners for JVM APIs: Spring Boot and JAX-RS."""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    DiscoveredEndpoint,
    ScanResult,
    action_from,
    balanced_segment,
    canonical_path,
    iter_source_files,
    line_of,
    path_parameters,
    read_text,
    relative_to,
    titleize,
)


SPRING_CLASS = re.compile(
    r"@(?:RestController|Controller)\b[\s\S]{0,400}?class\s+(?P<name>\w+)"
)
SPRING_CLASS_MAPPING = re.compile(
    r"@RequestMapping\s*\(\s*(?P<args>[^)]*)\)\s*(?:@\w+[^\n]*\s*)*"
    r"(?:public\s+)?(?:abstract\s+)?class\b"
)
SPRING_METHOD = re.compile(
    r"@(?P<kind>Get|Post|Put|Patch|Delete)Mapping\s*(?:\(\s*(?P<args>[^)]*)\))?"
)
SPRING_REQUEST_MAPPING = re.compile(
    r"@RequestMapping\s*\(\s*(?P<args>[^)]*)\)(?![\s\S]{0,200}?class\b)"
)
SPRING_HANDLER = re.compile(
    r"(?:public|protected|private)\s+[\w<>,\[\]\?\s\.]+\s+(?P<name>\w+)\s*\("
)
SPRING_PARAM = re.compile(
    r"@Request(?P<kind>Param|Body|Header)\s*(?:\(\s*(?P<args>[^)]*)\))?\s*"
    r"(?:final\s+)?(?P<type>[\w<>,\[\]\.]+)\s+(?P<name>\w+)"
)

JAXRS_CLASS_PATH = re.compile(r"@Path\s*\(\s*\"(?P<path>[^\"]*)\"\s*\)[\s\S]{0,400}?class\s+(?P<name>\w+)")
JAXRS_METHOD = re.compile(
    r"@(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b\s*"
    r"(?:@Path\s*\(\s*\"(?P<path>[^\"]*)\"\s*\)\s*)?"
)


def _quoted(value: str) -> list[str]:
    return re.findall(r'"([^"]*)"', value or "")


def _mapping_path(arguments: str) -> str:
    values = _quoted(arguments)
    return values[0] if values else ""


def _request_method(arguments: str) -> list[str]:
    found = re.findall(r"RequestMethod\.(\w+)", arguments or "")
    return [item.upper() for item in found] or ["GET"]


def scan_spring(root: Path, service_name: str) -> ScanResult:
    result = ScanResult(framework="spring", root=root)
    for file_path in iter_source_files(root, {".java", ".kt"}):
        text = read_text(file_path)
        if "Controller" not in text:
            continue
        class_match = SPRING_CLASS.search(text)
        if class_match is None:
            continue
        relative = relative_to(file_path, root)
        controller = titleize(class_match.group("name").removesuffix("Controller"))
        class_mapping = SPRING_CLASS_MAPPING.search(text)
        prefix = _mapping_path(class_mapping.group("args")) if class_mapping else ""

        occurrences: list[tuple[int, list[str], str]] = []
        for match in SPRING_METHOD.finditer(text):
            occurrences.append(
                (
                    match.end(),
                    [match.group("kind").upper()],
                    _mapping_path(match.group("args") or ""),
                )
            )
        for match in SPRING_REQUEST_MAPPING.finditer(text):
            if class_mapping is not None and match.start() == class_mapping.start():
                continue
            arguments = match.group("args") or ""
            occurrences.append(
                (match.end(), _request_method(arguments), _mapping_path(arguments))
            )

        for position, methods, route in sorted(occurrences):
            tail = text[position : position + 700]
            handler = SPRING_HANDLER.search(tail)
            action = handler.group("name") if handler else ""
            full_path = canonical_path(prefix, route)
            signature = (
                balanced_segment(tail, handler.end() - 1) if handler else ""
            )

            parameters = path_parameters(full_path)
            payload_type = None
            for parameter in SPRING_PARAM.finditer(signature):
                kind = parameter.group("kind")
                if kind == "Body":
                    payload_type = parameter.group("type")
                    continue
                names = _quoted(parameter.group("args") or "")
                parameters.append(
                    {
                        "name": names[0] if names else parameter.group("name"),
                        "source": "query" if kind == "Param" else "header",
                        "type": parameter.group("type"),
                        "required": "required = false" not in (
                            parameter.group("args") or ""
                        ),
                    }
                )

            for method in methods:
                result.endpoints.append(
                    DiscoveredEndpoint(
                        service=service_name,
                        controller=controller or "Root",
                        action=action or action_from(method, full_path),
                        method=method,
                        path=full_path,
                        parameters=[dict(item) for item in parameters],
                        payload_type=payload_type,
                        payload={} if payload_type else None,
                        source_file=relative,
                        source_line=line_of(text, position),
                    )
                )

    if not result.endpoints:
        result.warnings.append(
            "No @RestController classes with mapping annotations were found."
        )
    return result


def scan_jaxrs(root: Path, service_name: str) -> ScanResult:
    result = ScanResult(framework="jaxrs", root=root)
    for file_path in iter_source_files(root, {".java", ".kt"}):
        text = read_text(file_path)
        if "@Path" not in text:
            continue
        class_match = JAXRS_CLASS_PATH.search(text)
        if class_match is None:
            continue
        relative = relative_to(file_path, root)
        prefix = class_match.group("path")
        controller = titleize(
            class_match.group("name").removesuffix("Resource").removesuffix("Endpoint")
        )
        for match in JAXRS_METHOD.finditer(text):
            method = match.group("method").upper()
            route = canonical_path(prefix, match.group("path") or "")
            tail = text[match.end() : match.end() + 500]
            handler = SPRING_HANDLER.search(tail)
            result.endpoints.append(
                DiscoveredEndpoint(
                    service=service_name,
                    controller=controller or "Root",
                    action=handler.group("name") if handler else action_from(method, route),
                    method=method,
                    path=route,
                    parameters=path_parameters(route),
                    source_file=relative,
                    source_line=line_of(text, match.start()),
                )
            )

    if not result.endpoints:
        result.warnings.append("No @Path annotated JAX-RS resources were found.")
    return result
