"""Scanners for the PHP frameworks: Laravel, Symfony, Slim, and CodeIgniter."""

from __future__ import annotations

import re
from pathlib import Path

from .base import (
    DiscoveredEndpoint,
    ScanResult,
    action_from,
    canonical_path,
    controller_from_path,
    iter_source_files,
    line_of,
    normalise_path,
    path_parameters,
    read_text,
    relative_to,
    titleize,
)


#: ``Route::get('/users', [UserController::class, 'index'])``
LARAVEL_ROUTE = re.compile(
    r"Route::(?P<method>get|post|put|patch|delete|head|options|any|match)\s*\(\s*"
    r"(?P<args>.*?)\)\s*(?:->|;)",
    re.IGNORECASE | re.DOTALL,
)
LARAVEL_RESOURCE = re.compile(
    r"Route::(?P<kind>apiResource|resource)\s*\(\s*['\"](?P<prefix>[^'\"]+)['\"]\s*,\s*"
    r"(?P<controller>[\w\\]+)",
    re.IGNORECASE,
)
LARAVEL_GROUP_PREFIX = re.compile(
    r"Route::(?:group\s*\(\s*\[[^\]]*['\"]prefix['\"]\s*=>\s*['\"](?P<prefix>[^'\"]+)"
    r"['\"]|prefix\s*\(\s*['\"](?P<short>[^'\"]+)['\"])",
    re.IGNORECASE,
)

#: ``$app->get('/users', ...)`` used by Slim and Lumen.
SLIM_ROUTE = re.compile(
    r"\$(?:app|group|router)\s*->\s*(?P<method>get|post|put|patch|delete|head|options|map)"
    r"\s*\(\s*(?P<args>.*?)\)\s*(?:->|;)",
    re.IGNORECASE | re.DOTALL,
)

#: PHP 8 attributes and legacy annotations used by Symfony.
SYMFONY_ROUTE = re.compile(
    r"(?:#\[Route|@Route)\s*\(\s*(?P<args>.*?)\)\s*\]?",
    re.DOTALL,
)
SYMFONY_METHODS = re.compile(
    r"methods\s*[:=]\s*[\[{]?\s*(?P<methods>[^\]\})]*)", re.IGNORECASE
)
PHP_FUNCTION = re.compile(r"function\s+(\w+)\s*\(")
PHP_CLASS = re.compile(r"class\s+(\w+)")

RESOURCE_ROUTES = (
    ("GET", "", "index"),
    ("POST", "", "store"),
    ("GET", "{id}", "show"),
    ("PUT", "{id}", "update"),
    ("PATCH", "{id}", "patchUpdate"),
    ("DELETE", "{id}", "destroy"),
)


def _quoted(value: str) -> list[str]:
    return re.findall(r"['\"]([^'\"]*)['\"]", value)


def _handler_name(arguments: str) -> str:
    """Extracts a handler from ``[UserController::class, 'index']`` forms."""
    pair = re.search(r"\[\s*([\w\\]+)::class\s*,\s*['\"](\w+)['\"]", arguments)
    if pair:
        return f"{pair.group(1).rsplit('\\\\', 1)[-1]}@{pair.group(2)}"
    at_style = re.search(r"['\"]([\w\\]+)@(\w+)['\"]", arguments)
    if at_style:
        return f"{at_style.group(1).rsplit('\\\\', 1)[-1]}@{at_style.group(2)}"
    single = re.search(r"([\w\\]+)::class", arguments)
    return single.group(1).rsplit("\\", 1)[-1] if single else ""


def _split_handler(handler: str, route: str, fallback: str) -> tuple[str, str]:
    if "@" in handler:
        controller, action = handler.split("@", 1)
        return titleize(controller.removesuffix("Controller")), action
    if handler:
        return titleize(handler.removesuffix("Controller")), ""
    return controller_from_path(route, fallback), ""


def _add(
    result: ScanResult,
    service_name: str,
    method: str,
    route: str,
    controller: str,
    action: str,
    relative: str,
    line: int,
    payload: bool,
) -> None:
    result.endpoints.append(
        DiscoveredEndpoint(
            service=service_name,
            controller=controller or "Root",
            action=action or action_from(method, route),
            method=method,
            path=route,
            parameters=path_parameters(route),
            payload_type=None,
            payload={} if payload else None,
            source_file=relative,
            source_line=line,
        )
    )


def _block_end(text: str, start: int) -> int:
    """Returns the index just past the ``{ ... }`` block opened after ``start``."""
    open_index = text.find("{", start)
    if open_index < 0:
        return len(text)
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text)


def _laravel_prefix_ranges(text: str) -> list[tuple[int, int, str]]:
    """Locates every ``Route::prefix(...)->group`` block and its extent.

    A group prefix applies only to the routes inside its closure, so it must
    be matched by position rather than assumed to cover the whole file.
    """
    ranges: list[tuple[int, int, str]] = []
    for match in LARAVEL_GROUP_PREFIX.finditer(text):
        prefix = match.group("prefix") or match.group("short") or ""
        if not prefix:
            continue
        ranges.append((match.start(), _block_end(text, match.end()), prefix))
    return ranges


def _prefix_at(ranges: list[tuple[int, int, str]], index: int) -> str:
    """Concatenates the prefixes of every group enclosing ``index``."""
    return normalise_path(
        *[prefix for start, end, prefix in ranges if start <= index < end]
    )


def scan_laravel(root: Path, service_name: str) -> ScanResult:
    result = ScanResult(framework="laravel", root=root)
    files = [
        path
        for path in iter_source_files(root, {".php"})
        if "routes" in path.parts or path.stem in {"api", "web", "routes"}
    ] or list(iter_source_files(root, {".php"}))

    for file_path in files:
        text = read_text(file_path)
        if "Route::" not in text:
            continue
        relative = relative_to(file_path, root)
        prefix_ranges = _laravel_prefix_ranges(text)

        for match in LARAVEL_RESOURCE.finditer(text):
            base = match.group("prefix")
            controller = titleize(
                match.group("controller").rsplit("\\", 1)[-1].removesuffix("Controller")
            )
            for method, suffix, action in RESOURCE_ROUTES:
                if match.group("kind").lower() == "apiresource" and action == "patchUpdate":
                    continue
                route = canonical_path(
                    _prefix_at(prefix_ranges, match.start()), base, suffix
                )
                _add(
                    result,
                    service_name,
                    method,
                    route,
                    controller,
                    action,
                    relative,
                    line_of(text, match.start()),
                    method in {"POST", "PUT", "PATCH"},
                )

        for match in LARAVEL_ROUTE.finditer(text):
            arguments = match.group("args")
            strings = _quoted(arguments)
            if not strings:
                continue
            verb = match.group("method").upper()
            route = canonical_path(
                _prefix_at(prefix_ranges, match.start()), strings[0]
            )
            handler = _handler_name(arguments)
            controller, action = _split_handler(handler, route, titleize(file_path.stem))
            methods = ["GET", "POST"] if verb in {"ANY", "MATCH"} else [verb]
            for method in methods:
                _add(
                    result,
                    service_name,
                    method,
                    route,
                    controller,
                    action,
                    relative,
                    line_of(text, match.start()),
                    method in {"POST", "PUT", "PATCH"},
                )

    if not result.endpoints:
        result.warnings.append(
            "No Route:: definitions were found. Point the scan at the Laravel "
            "application root so routes/api.php is included."
        )
    return result


def scan_symfony(root: Path, service_name: str) -> ScanResult:
    result = ScanResult(framework="symfony", root=root)
    for file_path in iter_source_files(root, {".php"}):
        text = read_text(file_path)
        if "Route" not in text:
            continue
        relative = relative_to(file_path, root)
        class_match = PHP_CLASS.search(text)
        class_name = class_match.group(1) if class_match else file_path.stem
        controller = titleize(class_name.removesuffix("Controller"))

        matches = list(SYMFONY_ROUTE.finditer(text))
        class_prefix = ""
        if matches and class_match and matches[0].start() < class_match.start():
            paths = _quoted(matches[0].group("args"))
            class_prefix = paths[0] if paths else ""

        for match in matches:
            arguments = match.group("args")
            paths = _quoted(arguments)
            if not paths:
                continue
            route = paths[0]
            if route == class_prefix and match.start() < (
                class_match.start() if class_match else 0
            ):
                continue
            methods_match = SYMFONY_METHODS.search(arguments)
            methods = (
                [item.upper() for item in _quoted(methods_match.group("methods"))]
                if methods_match
                else ["GET"]
            )
            tail = text[match.end() : match.end() + 400]
            function_match = PHP_FUNCTION.search(tail)
            action = function_match.group(1) if function_match else ""
            full_path = canonical_path(class_prefix, route)
            for method in methods or ["GET"]:
                _add(
                    result,
                    service_name,
                    method,
                    full_path,
                    controller,
                    action,
                    relative,
                    line_of(text, match.start()),
                    method in {"POST", "PUT", "PATCH"},
                )

    if not result.endpoints:
        result.warnings.append("No #[Route] attributes or @Route annotations were found.")
    return result


def scan_slim(root: Path, service_name: str) -> ScanResult:
    result = ScanResult(framework="slim", root=root)
    for file_path in iter_source_files(root, {".php"}):
        text = read_text(file_path)
        if "->get(" not in text and "->post(" not in text and "->map(" not in text:
            continue
        relative = relative_to(file_path, root)
        for match in SLIM_ROUTE.finditer(text):
            arguments = match.group("args")
            strings = _quoted(arguments)
            if not strings:
                continue
            verb = match.group("method").upper()
            route = canonical_path(strings[0])
            handler = _handler_name(arguments)
            controller, action = _split_handler(handler, route, titleize(file_path.stem))
            methods = ["GET", "POST"] if verb == "MAP" else [verb]
            for method in methods:
                _add(
                    result,
                    service_name,
                    method,
                    route,
                    controller,
                    action,
                    relative,
                    line_of(text, match.start()),
                    method in {"POST", "PUT", "PATCH"},
                )

    if not result.endpoints:
        result.warnings.append("No $app->get()/post() route registrations were found.")
    return result
