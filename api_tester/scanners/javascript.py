"""Scanners for JavaScript and TypeScript APIs: Express, Koa, and NestJS."""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    DiscoveredEndpoint,
    ScanResult,
    action_from,
    balanced_segment,
    canonical_path,
    controller_from_path,
    iter_source_files,
    line_of,
    path_parameters,
    read_text,
    relative_to,
    titleize,
)


#: ``app.get('/users', handler)`` / ``router.post("/users", handler)``
EXPRESS_ROUTE = re.compile(
    r"\b(?P<holder>app|router|server|api|routes)\s*\.\s*"
    r"(?P<method>get|post|put|patch|delete|head|options|all)\s*\(\s*"
    r"(?P<quote>['\"`])(?P<path>[^'\"`]*)(?P=quote)",
    re.IGNORECASE,
)
#: ``router.route('/users').get(handler).post(handler)``
EXPRESS_CHAIN = re.compile(
    r"\.route\s*\(\s*(?P<quote>['\"`])(?P<path>[^'\"`]*)(?P=quote)\s*\)"
    r"(?P<chain>(?:\s*\.\s*\w+\s*\([^)]*\))+)"
)
EXPRESS_USE_PREFIX = re.compile(
    r"\b(?:app|server)\s*\.\s*use\s*\(\s*(?P<quote>['\"`])(?P<path>/[^'\"`]*)(?P=quote)"
    r"\s*,\s*(?P<target>[\w.]+)"
)

NEST_CONTROLLER = re.compile(
    r"@Controller\s*\(\s*(?:(?P<quote>['\"`])(?P<path>[^'\"`]*)(?P=quote))?[^)]*\)\s*"
    r"(?:export\s+)?class\s+(?P<name>\w+)"
)
NEST_METHOD = re.compile(
    r"@(?P<method>Get|Post|Put|Patch|Delete|Head|Options)\s*\(\s*"
    r"(?:(?P<quote>['\"`])(?P<path>[^'\"`]*)(?P=quote))?[^)]*\)"
)
NEST_HANDLER = re.compile(r"(?:async\s+)?(?P<name>\w+)\s*\(")
NEST_BODY = re.compile(r"@Body\s*\([^)]*\)\s*\w+\s*:\s*(?P<type>\w+)")
NEST_QUERY = re.compile(
    r"@Query\s*\(\s*(?:['\"`](?P<named>\w+)['\"`])?[^)]*\)\s*(?P<argument>\w+)"
    r"(?:\s*:\s*(?P<type>[\w\[\]<>]+))?"
)

METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
SOURCE_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}


def _add(
    result: ScanResult,
    service_name: str,
    method: str,
    route: str,
    controller: str,
    action: str,
    relative: str,
    line: int,
    parameters: list[dict] | None = None,
    payload_type: str | None = None,
) -> None:
    body = payload_type if method in {"POST", "PUT", "PATCH"} else None
    result.endpoints.append(
        DiscoveredEndpoint(
            service=service_name,
            controller=controller or "Root",
            action=action or action_from(method, route),
            method=method,
            path=route,
            parameters=parameters if parameters is not None else path_parameters(route),
            payload_type=body,
            payload={} if body else None,
            source_file=relative,
            source_line=line,
        )
    )


def scan_express(root: Path, service_name: str) -> ScanResult:
    result = ScanResult(framework="express", root=root)
    for file_path in iter_source_files(root, SOURCE_SUFFIXES):
        if file_path.name.endswith((".d.ts", ".spec.ts", ".test.js", ".test.ts")):
            continue
        text = read_text(file_path)
        if not text or (".get(" not in text and ".post(" not in text and ".route(" not in text):
            continue
        relative = relative_to(file_path, root)
        # A mount prefix belongs to the router it was mounted with, so routes
        # registered directly on ``app`` must not inherit it.
        mounts = {
            match.group("target").split(".")[-1]: match.group("path")
            for match in EXPRESS_USE_PREFIX.finditer(text)
        }

        for match in EXPRESS_ROUTE.finditer(text):
            verb = match.group("method").upper()
            holder = match.group("holder")
            route = canonical_path(mounts.get(holder, ""), match.group("path"))
            methods = sorted(METHODS) if verb == "ALL" else [verb]
            controller = controller_from_path(route, titleize(file_path.stem))
            for method in methods:
                _add(
                    result,
                    service_name,
                    method,
                    route,
                    controller,
                    "",
                    relative,
                    line_of(text, match.start()),
                )

        for match in EXPRESS_CHAIN.finditer(text):
            holder_match = re.search(r"(\w+)\s*$", text[: match.start()])
            holder = holder_match.group(1) if holder_match else ""
            route = canonical_path(mounts.get(holder, ""), match.group("path"))
            controller = controller_from_path(route, titleize(file_path.stem))
            for verb in re.findall(r"\.\s*(\w+)\s*\(", match.group("chain")):
                method = verb.upper()
                if method not in METHODS:
                    continue
                _add(
                    result,
                    service_name,
                    method,
                    route,
                    controller,
                    "",
                    relative,
                    line_of(text, match.start()),
                )

    if not result.endpoints:
        result.warnings.append(
            "No app/router HTTP registrations were found. Express routes built "
            "dynamically at runtime cannot be read statically."
        )
    return result


def scan_nestjs(root: Path, service_name: str) -> ScanResult:
    result = ScanResult(framework="nestjs", root=root)
    for file_path in iter_source_files(root, SOURCE_SUFFIXES):
        text = read_text(file_path)
        if "@Controller" not in text:
            continue
        relative = relative_to(file_path, root)
        controllers = list(NEST_CONTROLLER.finditer(text))
        for index, controller_match in enumerate(controllers):
            start = controller_match.end()
            end = (
                controllers[index + 1].start()
                if index + 1 < len(controllers)
                else len(text)
            )
            block = text[start:end]
            prefix = controller_match.group("path") or ""
            controller = titleize(
                controller_match.group("name").removesuffix("Controller")
            )
            for method_match in NEST_METHOD.finditer(block):
                method = method_match.group("method").upper()
                route = canonical_path(prefix, method_match.group("path") or "")
                tail = block[method_match.end() : method_match.end() + 600]
                handler_match = NEST_HANDLER.search(tail)
                action = handler_match.group("name") if handler_match else ""

                signature = (
                    balanced_segment(tail, handler_match.end() - 1)
                    if handler_match
                    else ""
                )
                parameters = path_parameters(route)
                for query_match in NEST_QUERY.finditer(signature):
                    name = query_match.group("named") or query_match.group("argument")
                    parameters.append(
                        {
                            "name": name,
                            "source": "query",
                            "type": query_match.group("type") or "string",
                            "required": False,
                        }
                    )
                body_match = NEST_BODY.search(signature)
                _add(
                    result,
                    service_name,
                    method,
                    route,
                    controller,
                    action,
                    relative,
                    line_of(text, start + method_match.start()),
                    parameters,
                    body_match.group("type") if body_match else None,
                )

    if not result.endpoints:
        result.warnings.append("No @Controller classes with HTTP decorators were found.")
    return result
