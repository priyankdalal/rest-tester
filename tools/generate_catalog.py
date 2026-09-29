from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from tools.schema_extractor import (
    build_filter_fields,
    build_form_schema,
    build_payload_schema,
    build_response_schema,
    form_field_kind as _form_kind,
    parse_source_types,
    simple_name,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent

#: Shown beside the app icon in the tester. Hand-edited catalogs carry their
#: own name; this is the one the generated workspace catalog gets.
CATALOG_NAME = "Rest Tester Microservices"


def normalize_product_text(value: Any) -> Any:
    """Uses the app's product name in strings imported from service sources."""
    if isinstance(value, str):
        return re.sub(r"trial\s*wyze", "Rest Tester", value, flags=re.IGNORECASE)
    if isinstance(value, list):
        return [normalize_product_text(item) for item in value]
    if isinstance(value, dict):
        return {
            normalize_product_text(key): normalize_product_text(item)
            for key, item in value.items()
        }
    return value


SERVICES = {
    "MasterData": "TrialManagement.MasterData",
    "Reporting": "TrialManagement.Reporting",
    "TrialAuth": "TrialManagement.TrialAuth",
    "TrialMaster": "TrialManagement.TrialMaster",
    "CollectionMaster": "TrialManagment.CollectionMaster",
}
AZURE_FUNCTIONS_REPOSITORY = "TrialManagement.AzureFunctions"
HTTP_ATTRIBUTE = re.compile(r"\bHttp(Get|Post|Put|Patch|Delete|Head)\b(?:\s*\((.*?)\))?", re.DOTALL)
ATTRIBUTE_BLOCK = re.compile(r"(?:\s*\[[^\]]+\]\s*)+")
CLASS_PATTERN = re.compile(
    r"(?P<attrs>(?:\s*\[[^\]]+\]\s*)*)"
    r"public\s+(?P<abstract>abstract\s+)?(?:partial\s+)?class\s+"
    r"(?P<name>\w+)(?:<(?P<generic>[^>{]+)>)?\s*"
    r"(?:\:\s*(?P<bases>[^{]+))?\{",
    re.MULTILINE,
)
PROPERTY_PATTERN = re.compile(
    r"public\s+(?:virtual\s+|required\s+|override\s+|new\s+)*"
    r"(?P<type>[\w.<>,?\[\]\s]+?)\s+(?P<name>\w+)\s*\{\s*get\s*;"
    r"(?:\s*(?:private|protected|internal)\s+)?\s*set\s*;",
    re.MULTILINE,
)


@dataclass
class Model:
    name: str
    base: str | None
    properties: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Action:
    name: str
    method: str
    route: str
    parameters: list[dict[str, Any]]
    payload_type: str | None
    return_type: str | None
    source_file: str
    source_line: int
    body: str = ""
    attributes: str = ""


@dataclass
class Controller:
    name: str
    abstract: bool
    route: str
    generic_parameters: list[str]
    base_name: str | None
    base_arguments: list[str]
    actions: list[Action]
    source_file: str
    dependencies: list[str] = field(default_factory=list)


def split_top_level(value: str, separator: str = ",") -> list[str]:
    result: list[str] = []
    start = 0
    depths = {"<": 0, "(": 0, "[": 0, "{": 0}
    closing = {">": "<", ")": "(", "]": "[", "}": "{"}
    in_string = False
    escaped = False
    for index, char in enumerate(value):
        if char == '"' and not escaped:
            in_string = not in_string
        escaped = char == "\\" and not escaped
        if in_string:
            continue
        if char in depths:
            depths[char] += 1
        elif char in closing:
            depths[closing[char]] = max(0, depths[closing[char]] - 1)
        elif char == separator and not any(depths.values()):
            result.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        result.append(tail)
    return result


def first_string(expression: str | None) -> str:
    if not expression:
        return ""
    match = re.search(r'"((?:\\.|[^"\\])*)"', expression)
    return bytes(match.group(1), "utf-8").decode("unicode_escape") if match else ""


def route_from_attributes(attributes: str) -> str:
    match = re.search(r"\bRoute\s*\((.*?)\)", attributes, re.DOTALL)
    return first_string(match.group(1)) if match else ""


def find_matching(text: str, start: int, opening: str = "(", closing: str = ")") -> int:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if char == '"' and not escaped:
            in_string = not in_string
        escaped = char == "\\" and not escaped
        if in_string:
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
    raise ValueError(f"Unmatched {opening} at offset {start}")


def clean_type(type_name: str) -> str:
    return re.sub(r"\s+", "", type_name).replace("global::", "")


def simple_type(type_name: str) -> str:
    cleaned = clean_type(type_name).rstrip("?")
    if "<" in cleaned:
        return cleaned[: cleaned.index("<")].split(".")[-1]
    return cleaned.split(".")[-1]


def parse_parameter(raw: str, route: str, method: str) -> tuple[list[dict[str, Any]], str | None]:
    raw = raw.strip()
    if not raw:
        return [], None
    attributes = " ".join(re.findall(r"\[([^\]]+)\]", raw))
    declaration = re.sub(r"\[[^\]]+\]\s*", "", raw)
    declaration = split_top_level(declaration, separator="=")[0].strip()
    declaration = re.sub(r"\b(params|this|ref|out|in)\s+", "", declaration)
    match = re.match(r"(?P<type>.+?)\s+(?P<name>@?\w+)$", declaration)
    if not match:
        return [], None
    type_name = clean_type(match.group("type"))
    name = match.group("name").lstrip("@")
    nullable = "?" in type_name or "=" in raw
    if "FromServices" in attributes or simple_type(type_name) in {"CancellationToken", "HttpContext"}:
        return [], None
    if simple_type(type_name) in {"IFormFile", "FormFile"}:
        return [
            {"name": name, "source": "form", "type": type_name, "required": not nullable}
        ], None
    if "FromBody" in attributes:
        return [], type_name
    if "FromForm" in attributes:
        return [
            {"name": name, "source": "form", "type": type_name, "required": not nullable}
        ], None
    if "FromHeader" in attributes:
        header_name = first_string(attributes) or name
        return [
            {"name": header_name, "source": "header", "type": type_name, "required": not nullable}
        ], None

    route_names = {
        item.split(":", 1)[0].lower() for item in re.findall(r"\{([^}]+)\}", route)
    }
    if "FromRoute" in attributes or name.lower() in route_names:
        return [{"name": name, "source": "path", "type": type_name, "required": True}], None
    if "FromQuery" in attributes or method in {"GET", "HEAD", "DELETE"}:
        return [
            {"name": name, "source": "query", "type": type_name, "required": not nullable}
        ], None
    return [], type_name


def parse_actions(
    text: str, class_body_start: int, source_file: Path, repository: Path
) -> list[Action]:
    actions: list[Action] = []
    body = text[class_body_start:]
    for attribute_match in ATTRIBUTE_BLOCK.finditer(body):
        attributes = attribute_match.group(0)
        http_matches = list(HTTP_ATTRIBUTE.finditer(attributes))
        if not http_matches:
            continue
        after = attribute_match.end()
        declaration_match = re.match(
            r"\s*public\s+(?:(?:virtual|override|new|static|async)\s+)*"
            r"(?P<return_type>[\w.<>,?\[\]\s]+?)\s+(?P<name>\w+)\s*\(",
            body[after:],
        )
        if not declaration_match:
            continue
        open_parenthesis = after + declaration_match.end() - 1
        try:
            close_parenthesis = find_matching(body, open_parenthesis)
        except ValueError:
            continue
        action_name = declaration_match.group("name")
        return_type = clean_type(declaration_match.group("return_type"))
        action_body = ""
        brace_match = re.search(r"\s*(?:where\s+[^{]+?)?\{", body[close_parenthesis + 1 :], re.DOTALL)
        if brace_match:
            open_brace = close_parenthesis + 1 + brace_match.end() - 1
            try:
                close_brace = find_matching(body, open_brace, opening="{", closing="}")
                action_body = body[open_brace + 1 : close_brace]
            except ValueError:
                action_body = ""
        method_route = route_from_attributes(attributes)
        for http_match in http_matches:
            method = http_match.group(1).upper()
            http_route = first_string(http_match.group(2))
            route = http_route or method_route
            raw_parameters = body[open_parenthesis + 1 : close_parenthesis]
            parameters: list[dict[str, Any]] = []
            payload_types: list[str] = []
            for raw_parameter in split_top_level(raw_parameters):
                parsed_parameters, payload_type = parse_parameter(raw_parameter, route, method)
                parameters.extend(parsed_parameters)
                if payload_type:
                    payload_types.append(payload_type)
            absolute_offset = class_body_start + attribute_match.start()
            actions.append(
                Action(
                    name=action_name,
                    method=method,
                    route=route,
                    parameters=parameters,
                    payload_type=payload_types[0] if payload_types else None,
                    return_type=return_type,
                    source_file=str(source_file.relative_to(repository)).replace("\\", "/"),
                    source_line=text.count("\n", 0, absolute_offset) + 1,
                    body=action_body,
                    attributes=attributes,
                )
            )
    return actions


def parse_constructor_dependencies(body: str, controller_name: str) -> list[str]:
    """Collects the injected provider/service types declared on the constructor."""
    match = re.search(
        rf"public\s+{re.escape(controller_name)}\s*\(", body
    )
    if not match:
        return []
    try:
        close = find_matching(body, match.end() - 1)
    except ValueError:
        return []
    return [
        re.sub(r"\s+", "", parameter).rsplit(" ", 1)[0]
        for parameter in split_top_level(body[match.end() : close])
        if parameter.strip()
    ]


def parse_controllers(repository: Path) -> dict[str, Controller]:
    controllers: dict[str, Controller] = {}
    for source_file in repository.rglob("*Controller.cs"):
        if any(part in {"bin", "obj"} for part in source_file.parts):
            continue
        text = source_file.read_text(encoding="utf-8-sig")
        for match in CLASS_PATTERN.finditer(text):
            name = match.group("name")
            generic_parameters = (
                [item.strip() for item in split_top_level(match.group("generic") or "")]
            )
            bases = split_top_level(match.group("bases") or "")
            base_name = None
            base_arguments: list[str] = []
            if bases:
                base_declaration = re.sub(r"\s+where\s+.*", "", bases[0], flags=re.DOTALL).strip()
                base_match = re.match(r"(?P<name>[\w.]+)(?:<(?P<args>.*)>)?", base_declaration)
                if base_match:
                    base_name = base_match.group("name").split(".")[-1]
                    base_arguments = split_top_level(base_match.group("args") or "")
            route = route_from_attributes(match.group("attrs"))
            class_end = declarations_end(text, match.end())
            controllers[name] = Controller(
                name=name,
                abstract=bool(match.group("abstract")),
                route=route,
                generic_parameters=generic_parameters,
                base_name=base_name,
                base_arguments=base_arguments,
                actions=parse_actions(text, match.end(), source_file, repository),
                source_file=str(source_file.relative_to(repository)).replace("\\", "/"),
                dependencies=parse_constructor_dependencies(text[match.end() : class_end], name),
            )
    return controllers


def declarations_end(text: str, start: int) -> int:
    """Returns a conservative end offset for a class body starting at ``start``."""
    next_class = CLASS_PATTERN.search(text, start)
    return next_class.start() if next_class else len(text)


def parse_models(repository: Path) -> dict[str, Model]:
    models: dict[str, Model] = {}
    declaration = re.compile(
        r"\b(?:class|record)\s+(?P<name>\w+)(?:<[^>{]+>)?"
        r"(?:\s*:\s*(?P<base>[\w.<>]+))?[^{]*\{",
        re.MULTILINE,
    )
    for source_file in repository.rglob("*.cs"):
        if any(part in {"bin", "obj"} for part in source_file.parts):
            continue
        try:
            text = source_file.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        declarations = list(declaration.finditer(text))
        for index, match in enumerate(declarations):
            end = declarations[index + 1].start() if index + 1 < len(declarations) else len(text)
            body = text[match.end():end]
            properties = [
                (property_match.group("name"), clean_type(property_match.group("type")))
                for property_match in PROPERTY_PATTERN.finditer(body)
            ]
            if properties or match.group("name") not in models:
                models[match.group("name")] = Model(
                    name=match.group("name"),
                    base=simple_type(match.group("base")) if match.group("base") else None,
                    properties=properties,
                )
    return models


def substitute(type_name: str | None, mapping: dict[str, str]) -> str | None:
    if type_name is None:
        return None
    result = type_name
    for generic_name, concrete_name in mapping.items():
        result = re.sub(rf"\b{re.escape(generic_name)}\b", concrete_name, result)
    return result


def _split_generic_arguments(text: str) -> list[str]:
    return [item.strip() for item in split_top_level(text) if item.strip()]


def _unwrap_generic_type(type_name: str, outer_name: str) -> str | None:
    cleaned = clean_type(type_name)
    simple_outer = outer_name.split(".")[-1]
    if "<" not in cleaned or not cleaned.endswith(">"):
        return None
    base, arguments = cleaned.split("<", 1)
    if base.split(".")[-1] != simple_outer:
        return None
    values = _split_generic_arguments(arguments[:-1])
    return values[0] if values else None


def unwrap_response_type(type_name: str | None) -> str | None:
    if not type_name:
        return None
    task_inner = _unwrap_generic_type(type_name, "Task")
    if task_inner:
        action_result_inner = _unwrap_generic_type(task_inner, "ActionResult")
        if action_result_inner:
            return action_result_inner
    return _unwrap_generic_type(type_name, "ActionResult")


def _status_priority(expression: str) -> int | None:
    digits = re.search(r"\b(20[0-9])\b", expression)
    if digits:
        code = int(digits.group(1))
        if code == 200:
            return 0
        if code == 201:
            return 1
        if code == 202:
            return 2
        return 3
    normalized = re.sub(r"\s+", "", expression).lower()
    if normalized.endswith("status200ok") or normalized.endswith(".ok"):
        return 0
    if normalized.endswith("status201created") or normalized.endswith(".created"):
        return 1
    if normalized.endswith("status202accepted") or normalized.endswith(".accepted"):
        return 2
    return None


def response_type_from_attributes(attributes: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    for block in re.findall(r"\[(.*?)\]", attributes, re.DOTALL):
        for entry in split_top_level(block):
            match = re.match(r"\s*ProducesResponseType(?:Attribute)?\s*\((?P<args>.*)\)\s*$", entry, re.DOTALL)
            if not match:
                continue
            arguments = _split_generic_arguments(match.group("args"))
            if not arguments or not arguments[0].startswith("typeof("):
                continue
            type_match = re.match(r"typeof\(\s*(?P<type>.+?)\s*\)$", arguments[0], re.DOTALL)
            if not type_match:
                continue
            candidate_type = clean_type(type_match.group("type"))
            if simple_type(candidate_type).lower() == "void":
                continue
            priority = _status_priority(arguments[1]) if len(arguments) > 1 else None
            if priority is not None:
                candidates.append((priority, candidate_type))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def response_type_from_body(action_body: str) -> str | None:
    return_patterns = [
        r"return\s+Ok\(\s*(?P<name>\w+)\s*\)\s*;",
        r"return\s+new\s+OkObjectResult\(\s*(?P<name>\w+)\s*\)\s*;",
        r"return\s+StatusCode\(\s*[^,]+,\s*(?P<name>\w+)\s*\)\s*;",
        r"return\s+Created\(\s*[^,]+,\s*(?P<name>\w+)\s*\)\s*;",
        r"return\s+new\s+CreatedResult\(\s*[^,]+,\s*(?P<name>\w+)\s*\)\s*;",
        r"return\s+Accepted\(\s*[^,]+,\s*(?P<name>\w+)\s*\)\s*;",
        r"return\s+new\s+AcceptedResult\(\s*[^,]+,\s*(?P<name>\w+)\s*\)\s*;",
    ]
    declaration_pattern = re.compile(
        r"(?P<type>(?!var\b)[\w.<>,?\[\]\s]+?)\s+(?P<name>\w+)\s*(?:=|;)",
        re.MULTILINE,
    )
    for pattern in return_patterns:
        for return_match in re.finditer(pattern, action_body):
            name = return_match.group("name")
            prefix = action_body[: return_match.start()]
            for declaration in declaration_pattern.finditer(prefix):
                if declaration.group("name") == name:
                    declared_type = clean_type(declaration.group("type"))
                    if declared_type != "var":
                        return declared_type
    return None


def inherited_actions(
    controller: Controller,
    controllers: dict[str, Controller],
    mapping: dict[str, str] | None = None,
) -> Iterable[tuple[Action, dict[str, str]]]:
    mapping = mapping or {}
    for action in controller.actions:
        yield action, mapping
    if not controller.base_name or controller.base_name not in controllers:
        return
    base = controllers[controller.base_name]
    resolved_arguments = [substitute(argument, mapping) or argument for argument in controller.base_arguments]
    base_mapping = dict(zip(base.generic_parameters, resolved_arguments))
    yield from inherited_actions(base, controllers, base_mapping)


def controller_route(controller: Controller, controllers: dict[str, Controller]) -> str:
    current: Controller | None = controller
    while current is not None:
        if current.route:
            return current.route
        current = controllers.get(current.base_name or "")
    return "[controller]"


def model_properties(
    type_name: str, models: dict[str, Model], seen: set[str] | None = None
) -> list[tuple[str, str]]:
    seen = seen or set()
    name = simple_type(type_name)
    if name in seen or name not in models:
        return []
    seen.add(name)
    model = models[name]
    properties = model_properties(model.base, models, seen) if model.base else []
    properties.extend(model.properties)
    unique: dict[str, str] = {}
    for property_name, property_type in properties:
        unique[property_name] = property_type
    return list(unique.items())


def sample_value(
    type_name: str, name: str, models: dict[str, Model], depth: int = 0
) -> Any:
    cleaned = clean_type(type_name).rstrip("?")
    lowered = cleaned.lower()
    if depth > 2:
        return None
    if cleaned.startswith("JsonPatchDocument<"):
        return [{"op": "replace", "path": "/propertyName", "value": "test-value"}]
    dictionary_match = re.match(r"(?:Dictionary|IDictionary)<[^,]+,(.+)>", cleaned)
    if dictionary_match:
        return {"key": sample_value(dictionary_match.group(1), "value", models, depth + 1)}
    collection_match = re.match(
        r"(?:List|IEnumerable|ICollection|IList|HashSet)<(.+)>", cleaned
    )
    if collection_match:
        child = sample_value(collection_match.group(1), name, models, depth + 1)
        return [] if child is None else [child]
    if cleaned.endswith("[]"):
        child = sample_value(cleaned[:-2], name, models, depth + 1)
        return [] if child is None else [child]
    if cleaned in {"string", "char"}:
        return f"test-{name}"
    if cleaned in {"Guid"}:
        return "00000000-0000-0000-0000-000000000001"
    if cleaned in {"DateTime", "DateTimeOffset"}:
        return "2026-01-01T00:00:00Z"
    if cleaned in {"DateOnly"}:
        return "2026-01-01"
    if cleaned in {"bool", "Boolean"}:
        return False
    if cleaned in {
        "byte", "short", "int", "long", "float", "double", "decimal",
        "Byte", "Int16", "Int32", "Int64", "Single", "Double", "Decimal",
    }:
        return 1
    if simple_type(cleaned) in models:
        return {
            property_name: sample_value(property_type, property_name, models, depth + 1)
            for property_name, property_type in model_properties(cleaned, models)
        }
    return f"test-{name}"


def expand_query_parameter(
    parameter: dict[str, Any], models: dict[str, Model]
) -> list[dict[str, Any]]:
    if parameter["source"] != "query":
        return [parameter]
    properties = model_properties(parameter["type"], models)
    if not properties:
        parameter["sample"] = sample_value(parameter["type"], parameter["name"], models)
        return [parameter]
    return [
        {
            "name": name,
            "source": "query",
            "type": type_name,
            "required": False,
            "sample": sample_value(type_name, name, models),
        }
        for name, type_name in properties
    ]


def combine_route(controller_path: str, action_path: str, controller_name: str) -> str:
    if action_path.startswith("~/"):
        combined = action_path[2:]
    elif action_path.startswith("/"):
        combined = action_path[1:]
    else:
        combined = "/".join(part.strip("/") for part in (controller_path, action_path) if part)
    controller_token = controller_name.removesuffix("Controller")
    combined = re.sub(r"\[controller\]", controller_token, combined, flags=re.IGNORECASE)
    return "/" + re.sub(r"/+", "/", combined).strip("/")


def azure_function_payload(function_directory: Path, script_file: str) -> dict[str, Any] | None:
    source_file = function_directory / script_file
    if not source_file.exists():
        return None
    text = source_file.read_text(encoding="utf-8-sig")
    if "get_json(" not in text:
        return None
    payload: dict[str, Any] = {}
    for match in re.finditer(
        r"\bbody\.get\(\s*[\"'](?P<name>[^\"']+)[\"']"
        r"(?:\s*,\s*(?P<default>\[\]|None|True|False|\d+|[\"'][^\"']*[\"']))?\s*\)",
        text,
    ):
        name = match.group("name")
        default = match.group("default")
        if default == "[]":
            value: Any = []
        elif default in {"True", "False"}:
            value = default == "True"
        elif default and default.isdigit():
            value = int(default)
        elif default and default[:1] in {'"', "'"}:
            value = default[1:-1]
        elif name.lower().endswith("ids"):
            value = []
        else:
            value = f"test-{name}"
        payload[name] = value
    return payload or {}


def build_azure_functions_service(workspace_root: Path) -> dict[str, Any]:
    repository = workspace_root / AZURE_FUNCTIONS_REPOSITORY
    if not repository.exists():
        raise FileNotFoundError(f"Azure Functions repository not found: {repository}")
    host_path = repository / "host.json"
    host = json.loads(host_path.read_text(encoding="utf-8-sig")) if host_path.exists() else {}
    route_prefix = host.get("extensions", {}).get("http", {}).get("routePrefix", "api").strip("/")
    endpoints: list[dict[str, Any]] = []
    for function_config in repository.glob("*/function.json"):
        document = json.loads(function_config.read_text(encoding="utf-8-sig"))
        trigger = next(
            (binding for binding in document.get("bindings", []) if binding.get("type") == "httpTrigger"),
            None,
        )
        if trigger is None:
            continue
        function_name = function_config.parent.name
        route = trigger.get("route", function_name).strip("/")
        path = "/" + "/".join(part for part in (route_prefix, route) if part)
        script_file = document.get("scriptFile", "__init__.py")
        payload = azure_function_payload(function_config.parent, script_file)
        for raw_method in trigger.get("methods", ["get", "post"]):
            method = raw_method.upper()
            identity = f"AzureFunctions:{function_name}:{method}:{path}"
            endpoints.append(
                {
                    "id": hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12],
                    "service": "AzureFunctions",
                    "controller": function_name,
                    "action": "main",
                    "method": method,
                    "path": path,
                    "parameters": [],
                    "payload": payload if method not in {"GET", "HEAD"} else None,
                    "payload_type": "JSON object" if payload is not None and method not in {"GET", "HEAD"} else None,
                    "expected_status": "200-299",
                    "source_file": str(function_config.relative_to(repository)).replace("\\", "/"),
                    "source_line": 1,
                }
            )
    endpoints.sort(key=lambda item: (item["controller"], item["path"], item["method"]))
    return {
        "name": "AzureFunctions",
        "repository": AZURE_FUNCTIONS_REPOSITORY,
        "default_base_url": "",
        "endpoints": endpoints,
    }


def resolve_entity(
    controller: Controller,
    mapping: dict[str, str],
    types: dict[str, Any],
) -> str | None:
    """Finds the domain entity whose properties back a module's filter/sort schema.

    Generic collection controllers (``StaticController<Model, Filters>``,
    ``OnFarmDataController<TModel, TFilters>``, ``LabBookController<...>``)
    carry the entity as a generic argument. Conventional controllers are
    matched by name, falling back to the ``*Response`` DTO a module returns.
    """
    for argument in mapping.values():
        candidate = simple_name(argument)
        if candidate in types and not candidate.endswith(("QueryFilters", "Filters")):
            return candidate
    base = controller.name.removesuffix("Controller")
    for candidate in (base, f"{base}Response", f"{base}GetAllResponse", f"{base}Book"):
        if candidate in types:
            return candidate
    for dependency in controller.dependencies:
        generic = re.search(r"<([^,>]+)", dependency)
        if generic:
            candidate = simple_name(generic.group(1))
            if candidate in types and not candidate.endswith(("QueryFilters", "Filters")):
                return candidate
        interface = re.match(r"I(\w+?)Provider$", simple_name(dependency))
        if interface and interface.group(1) in types:
            return interface.group(1)
    return None


def entity_for_action(
    controller: Controller,
    controllers: dict[str, Controller],
    mapping: dict[str, str],
    types: dict[str, Any],
) -> str | None:
    entity = resolve_entity(controller, mapping, types)
    if entity is not None:
        return entity
    base_name = controller.base_name
    while base_name and base_name in controllers:
        base = controllers[base_name]
        entity = resolve_entity(base, mapping, types)
        if entity is not None:
            return entity
        base_name = base.base_name
    return None


def scan_dotnet_project(
    service_name: str,
    repository: Path,
    shared_root: Path | None = None,
    filter_schemas: dict[str, Any] | None = None,
    payload_schemas: dict[str, Any] | None = None,
    form_schemas: dict[str, Any] | None = None,
    response_schemas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parses one ASP.NET Core project into catalog fragments.

    ``shared_root`` points at a solution-level package/library folder whose
    models, DTOs, and enums are referenced by the project.  The schema
    dictionaries may be supplied so several projects in one solution share a
    single set of resolved types; the first definition of a name always wins.
    """
    filter_schemas = {} if filter_schemas is None else filter_schemas
    payload_schemas = {} if payload_schemas is None else payload_schemas
    form_schemas = {} if form_schemas is None else form_schemas
    response_schemas = {} if response_schemas is None else response_schemas

    if shared_root is not None and shared_root.exists():
        shared_types, shared_enums = parse_source_types(shared_root)
        shared_models = parse_models(shared_root)
    else:
        shared_types, shared_enums, shared_models = {}, {}, {}

    controllers = parse_controllers(repository)
    models = dict(shared_models)
    models.update(parse_models(repository))
    types = dict(shared_types)
    enums = dict(shared_enums)
    repository_types, repository_enums = parse_source_types(repository)
    types.update(repository_types)
    enums.update(repository_enums)

    endpoints: list[dict[str, Any]] = []
    for controller in controllers.values():
        if controller.abstract or not controller.name.endswith("Controller"):
            continue
        base_route = controller_route(controller, controllers)
        for action, mapping in inherited_actions(controller, controllers):
            path = combine_route(base_route, action.route, controller.name)
            parameters: list[dict[str, Any]] = []
            for original in action.parameters:
                parameter = dict(original)
                parameter["type"] = substitute(parameter["type"], mapping)
                parameters.extend(expand_query_parameter(parameter, models))
            payload_type = substitute(action.payload_type, mapping)
            payload = (
                sample_value(payload_type, "payload", models)
                if payload_type is not None
                else None
            )
            has_filter = any(
                parameter["source"] == "query" and parameter["name"].lower() == "filter"
                for parameter in parameters
            )
            has_sort = any(
                parameter["source"] == "query" and parameter["name"].lower() == "sort"
                for parameter in parameters
            )
            entity = (
                entity_for_action(controller, controllers, mapping, types)
                if has_filter or has_sort
                else None
            )
            if entity is not None and entity not in filter_schemas:
                fields = build_filter_fields(entity, types, enums)
                if fields:
                    filter_schemas[entity] = {"name": entity, "fields": fields}
            if entity is not None and entity not in filter_schemas:
                entity = None

            payload_schema_ref = None
            if payload_type is not None:
                schema = build_payload_schema(payload_type, types, enums)
                if schema is not None:
                    payload_schema_ref = simple_name(payload_type)
                    payload_schemas.setdefault(payload_schema_ref, schema)

            response_schema_ref = None
            response_type = substitute(unwrap_response_type(action.return_type), mapping)
            if response_type is None:
                response_type = substitute(response_type_from_attributes(action.attributes), mapping)
            if response_type is None:
                response_type = substitute(response_type_from_body(action.body), mapping)
            if response_type is not None and simple_type(response_type).lower() != "void":
                schema = build_response_schema(response_type, types, enums)
                if schema is not None:
                    response_schema_ref = simple_name(response_type)
                    response_schemas.setdefault(response_schema_ref, schema)

            form_schema_ref = None
            complex_form = next(
                (
                    parameter
                    for parameter in parameters
                    if parameter["source"] == "form"
                    and simple_name(parameter["type"]) in types
                ),
                None,
            )
            if complex_form is not None:
                schema = build_form_schema(complex_form["type"], types, enums)
                if schema is not None:
                    form_schema_ref = simple_name(complex_form["type"])
                    form_schemas.setdefault(form_schema_ref, schema)
                    # The DTO is bound property-by-property, so the single
                    # parameter is replaced by its real form fields.
                    parameters = [
                        parameter
                        for parameter in parameters
                        if parameter is not complex_form
                    ] + [
                        {
                            "name": field["name"],
                            "source": "form",
                            "type": field["clr_type"],
                            "required": bool(field.get("required")),
                        }
                        for field in schema["fields"]
                    ]
            if form_schema_ref is None:
                # Plain form parameters (e.g. a bare IFormFile) still get a
                # schema so the Form tab can render them by type.
                plain = [p for p in parameters if p["source"] == "form"]
                if plain:
                    form_schema_ref = f"{controller.name.removesuffix('Controller')}.{action.name}"
                    form_schemas.setdefault(
                        form_schema_ref,
                        {
                            "name": form_schema_ref,
                            "clr_type": "",
                            "fields": [
                                {
                                    "name": p["name"],
                                    "clr_type": p["type"],
                                    "nullable": not p["required"],
                                    "required": p["required"],
                                    "display_name": p["name"],
                                    "kind": _form_kind(p["type"], enums),
                                }
                                for p in plain
                            ],
                        },
                    )

            identity = f"{service_name}:{controller.name}:{action.method}:{path}:{action.name}"
            endpoint_id = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:12]
            endpoints.append(
                {
                    "id": endpoint_id,
                    "service": service_name,
                    "controller": controller.name.removesuffix("Controller"),
                    "action": action.name,
                    "method": action.method,
                    "path": path,
                    "parameters": parameters,
                    "payload": payload,
                    "payload_type": payload_type,
                    "payload_schema": payload_schema_ref,
                    "response_schema": response_schema_ref,
                    "form_schema": form_schema_ref,
                    "filter_entity": entity,
                    "expected_status": "200-299",
                    "source_file": action.source_file,
                    "source_line": action.source_line,
                }
            )
    endpoints.sort(
        key=lambda item: (item["controller"], item["path"], item["method"], item["action"])
    )
    return {
        "endpoints": endpoints,
        "filter_schemas": filter_schemas,
        "payload_schemas": payload_schemas,
        "form_schemas": form_schemas,
        "response_schemas": response_schemas,
        "enums": enums,
    }


def build_catalog(
    workspace_root: Path = WORKSPACE_ROOT, name: str = CATALOG_NAME
) -> dict[str, Any]:
    services: list[dict[str, Any]] = []
    filter_schemas: dict[str, Any] = {}
    payload_schemas: dict[str, Any] = {}
    form_schemas: dict[str, Any] = {}
    response_schemas: dict[str, Any] = {}
    all_enums: dict[str, list[str]] = {}
    shared_root = workspace_root / "TrialManagement.Packages"
    for service_name, repository_name in SERVICES.items():
        repository = workspace_root / repository_name
        if not repository.exists():
            raise FileNotFoundError(f"Microservice repository not found: {repository}")
        scanned = scan_dotnet_project(
            service_name,
            repository,
            shared_root,
            filter_schemas,
            payload_schemas,
            form_schemas,
            response_schemas,
        )
        all_enums.update(scanned["enums"])
        services.append(
            {
                "name": service_name,
                "repository": repository_name,
                "default_base_url": "",
                "endpoints": scanned["endpoints"],
            }
        )
    services.append(build_azure_functions_service(workspace_root))
    document = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "name": name or CATALOG_NAME,
        "workspace_root": str(workspace_root),
        "filter_grammar": {
            "separator": ":=",
            "and": ";",
            "or": "|",
            "value_delimiter": ",",
            "sort_descending_suffix": "-",
            "pattern": "Name__operator:=value[,value][;|]...",
        },
        "services": services,
        "filter_schemas": dict(sorted(filter_schemas.items())),
        "payload_schemas": dict(sorted(payload_schemas.items())),
        "form_schemas": dict(sorted(form_schemas.items())),
        "response_schemas": dict(sorted(response_schemas.items())),
        "enums": dict(sorted(all_enums.items())),
    }
    return normalize_product_text(document)


def comparable_catalog(document: dict[str, Any]) -> dict[str, Any]:
    result = dict(document)
    result.pop("generated_at", None)
    result.pop("workspace_root", None)
    # The catalog's display name is authored in the Catalog Builder, not
    # recovered from source, so renaming a catalog is not drift.
    result.pop("name", None)
    return result


def catalog_differences(
    committed: dict[str, Any], generated: dict[str, Any]
) -> list[str]:
    """Reports how a committed catalog drifts from freshly extracted sources.

    The comparison is deliberately **one-sided**: the Catalog Builder lets an
    author enrich a catalog with detail no source parser can recover — an
    ``enum`` reference on a parameter or field, a hand-written ``values`` or
    ``lov`` list, an explicitly cleared schema reference. That enrichment is
    always *additive*, so a key that exists only on the committed side is
    ignored. Everything the extractor does produce is still compared, so a
    changed route, verb, parameter or field shape is caught as before.
    """
    problems: list[str] = []

    def walk(mine: Any, theirs: Any, path: str) -> None:
        if isinstance(theirs, dict):
            if not isinstance(mine, dict):
                problems.append(f"{path}: expected an object")
                return
            # An enum reference deliberately rebinds the member list, so the
            # extracted list is no longer the authority for this field.
            rebound = isinstance(mine.get("enum"), str)
            for key, value in theirs.items():
                if key not in mine:
                    problems.append(f"{path}.{key}: missing from the committed catalog")
                elif key == "lov" and rebound:
                    continue
                else:
                    walk(mine[key], value, f"{path}.{key}")
            return
        if isinstance(theirs, list):
            if not isinstance(mine, list):
                problems.append(f"{path}: expected a list")
                return
            if len(mine) != len(theirs):
                problems.append(
                    f"{path}: {len(mine)} entries committed, {len(theirs)} extracted"
                )
                return
            for index, (a, b) in enumerate(zip(mine, theirs)):
                walk(a, b, f"{path}[{index}]")
            return
        if mine != theirs:
            problems.append(f"{path}: {mine!r} committed, {theirs!r} extracted")

    walk(comparable_catalog(committed), comparable_catalog(generated), "catalog")
    return problems


def render_markdown(document: dict[str, Any]) -> str:
    total = sum(len(service["endpoints"]) for service in document["services"])
    lines = [
        "# Rest Tester API Endpoint Inventory",
        "",
        f"Generated from ASP.NET controllers. **{total} executable endpoint test cases** are cataloged.",
        "",
        "Regenerate with `python -m tools.generate_catalog` after controller changes.",
        "",
    ]
    for service in document["services"]:
        lines.extend(
            [
                f"## {service['name']} ({len(service['endpoints'])})",
                "",
                "| Method | Path | Action | Query parameters | Payload |",
                "|---|---|---|---|---|",
            ]
        )
        for endpoint in service["endpoints"]:
            query = ", ".join(
                parameter["name"]
                for parameter in endpoint["parameters"]
                if parameter["source"] == "query"
            ) or "-"
            payload = f"`{endpoint['payload_type']}`" if endpoint["payload_type"] else "-"
            lines.append(
                f"| {endpoint['method']} | `{endpoint['path']}` | "
                f"{endpoint['controller']}.{endpoint['action']} | {query} | {payload} |"
            )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate API test cases from C# controllers.")
    parser.add_argument("--workspace-root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--check", action="store_true", help="Fail when the committed catalog is stale.")
    args = parser.parse_args()
    catalog_path = PROJECT_ROOT / "data" / "api_catalog.json"
    # The display name is hand-authored, so a rewrite must not discard it.
    existing_name = ""
    if catalog_path.exists():
        try:
            existing_name = str(
                json.loads(catalog_path.read_text(encoding="utf-8")).get("name") or ""
            ).strip()
        except (OSError, ValueError):
            existing_name = ""
    document = build_catalog(args.workspace_root.resolve(), existing_name)
    if args.check:
        if not catalog_path.exists():
            print("API catalog is missing. Run: python -m tools.generate_catalog")
            return 1
        current = json.loads(catalog_path.read_text(encoding="utf-8"))
        problems = catalog_differences(current, document)
        if problems:
            print("API catalog is stale. Run: python -m tools.generate_catalog")
            for problem in problems[:20]:
                print(f"  - {problem}")
            if len(problems) > 20:
                print(f"  ... and {len(problems) - 20} more")
            return 1
        print("API catalog matches the current microservice controllers.")
        return 0

    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    catalog_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    (PROJECT_ROOT / "ENDPOINTS.md").write_text(render_markdown(document), encoding="utf-8")
    count = sum(len(service["endpoints"]) for service in document["services"])
    print(f"Generated {count} endpoint test cases in {catalog_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
