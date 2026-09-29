"""Imports an existing OpenAPI/Swagger document as a catalog service.

This is the highest-fidelity source available: rather than inferring routes
from source code, it reads the contract the service already publishes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .base import (
    DiscoveredEndpoint,
    HTTP_METHODS,
    ScanResult,
    action_from,
    controller_from_path,
    relative_to,
    titleize,
)


DOCUMENT_NAMES = (
    "openapi.json",
    "openapi.yaml",
    "openapi.yml",
    "swagger.json",
    "swagger.yaml",
    "swagger.yml",
)

_TYPE_ALIASES = {
    "integer": "int",
    "number": "double",
    "boolean": "bool",
    "string": "string",
    "array": "array",
    "object": "object",
}


def find_documents(root: Path) -> list[Path]:
    """Finds candidate OpenAPI documents under ``root``."""
    if root.is_file():
        return [root]
    found: list[Path] = []
    for name in DOCUMENT_NAMES:
        found.extend(sorted(root.rglob(name)))
    seen: list[Path] = []
    for path in found:
        if path not in seen and ".venv" not in path.parts and "node_modules" not in path.parts:
            seen.append(path)
    return seen


def load_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore import-not-found
        except ModuleNotFoundError as error:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Reading YAML OpenAPI documents requires PyYAML. "
                "Install it or export the document as JSON."
            ) from error
        document = yaml.safe_load(text)
    else:
        document = json.loads(text)
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} does not contain an OpenAPI document.")
    return document


def _reference_name(reference: str) -> str:
    return reference.rsplit("/", 1)[-1]


def _resolve(document: dict[str, Any], node: Any) -> dict[str, Any]:
    seen: set[str] = set()
    while isinstance(node, dict) and "$ref" in node:
        reference = str(node["$ref"])
        if reference in seen:
            return {}
        seen.add(reference)
        target: Any = document
        for piece in reference.lstrip("#/").split("/"):
            if not isinstance(target, dict):
                return {}
            target = target.get(piece, {})
        node = target
    return node if isinstance(node, dict) else {}


def _schema_type(schema: dict[str, Any]) -> str:
    if "$ref" in schema:
        return _reference_name(str(schema["$ref"]))
    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((item for item in declared if item != "null"), "string")
    if declared == "array":
        items = schema.get("items")
        inner = _schema_type(items) if isinstance(items, dict) else "string"
        return f"{inner}[]"
    return _TYPE_ALIASES.get(str(declared or "string"), str(declared or "string"))


def _flatten_schema(document: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    resolved = _resolve(document, schema)
    properties = resolved.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = set(resolved.get("required") or [])
    fields: dict[str, Any] = {}
    for name, definition in properties.items():
        if not isinstance(definition, dict):
            continue
        enum_values = definition.get("enum")
        fields[name] = {
            "type": _schema_type(definition),
            "required": name in required,
            "nullable": bool(definition.get("nullable")),
            "description": definition.get("description") or "",
            "enum": [str(value) for value in enum_values] if enum_values else None,
        }
    return fields


def _catalog_kind(schema: dict[str, Any]) -> str:
    declared = schema.get("type")
    if isinstance(declared, list):
        declared = next((item for item in declared if item != "null"), "string")
    return {
        "string": "text",
        "integer": "integer",
        "number": "number",
        "boolean": "boolean",
        "array": "array",
        "object": "object",
    }.get(str(declared or "object"), "text")


def _catalog_schema(
    document: dict[str, Any],
    schema: dict[str, Any],
    name: str = "",
    seen: set[str] | None = None,
) -> dict[str, Any] | None:
    """Converts an OpenAPI schema into the catalog's recursive field format."""
    seen = set() if seen is None else set(seen)
    reference = str(schema.get("$ref") or "")
    if reference:
        if reference in seen:
            return None
        seen.add(reference)
        name = name or _reference_name(reference)
    resolved = _resolve(document, schema)
    kind = _catalog_kind(resolved)
    nullable = bool(resolved.get("nullable")) or (
        isinstance(resolved.get("type"), list) and "null" in resolved["type"]
    )
    if kind == "array":
        raw_item = resolved.get("items")
        item = (
            _catalog_schema(document, raw_item, seen=seen)
            if isinstance(raw_item, dict)
            else None
        )
        return {
            "kind": "array",
            "name": name,
            "clr_type": _schema_type(schema),
            "nullable": nullable,
            "item": item or {"kind": "text", "clr_type": "string"},
        }
    properties = resolved.get("properties")
    if not isinstance(properties, dict):
        enum_values = resolved.get("enum")
        scalar: dict[str, Any] = {
            "kind": "enum" if enum_values else kind,
            "clr_type": _schema_type(schema),
            "nullable": nullable,
        }
        if enum_values:
            scalar["lov"] = [str(value) for value in enum_values]
        return scalar
    required = set(resolved.get("required") or [])
    fields: list[dict[str, Any]] = []
    for field_name, raw in properties.items():
        if not isinstance(raw, dict):
            continue
        child = _catalog_schema(document, raw, seen=seen) or {
            "kind": "text",
            "clr_type": "string",
        }
        child.update(
            {
                "name": str(field_name),
                "display_name": str(field_name),
                "required": field_name in required,
                "nullable": bool(child.get("nullable")),
            }
        )
        fields.append(child)
    return {
        "kind": "object",
        "name": name,
        "clr_type": name or _schema_type(schema),
        "nullable": nullable,
        "fields": fields,
    }


def _successful_response_schema(
    document: dict[str, Any], operation: dict[str, Any], action: str
) -> tuple[str | None, dict[str, Any] | None]:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return None, None
    successful = [
        (str(code), response)
        for code, response in responses.items()
        if str(code).isdigit()
        and str(code).startswith("2")
        and isinstance(response, dict)
    ]
    successful.sort(
        key=lambda item: (
            {"200": 0, "201": 1, "202": 2}.get(item[0], 3),
            item[0],
        )
    )
    for _, raw_response in successful:
        response = _resolve(document, raw_response)
        content = response.get("content")
        if not isinstance(content, dict):
            continue
        media = content.get("application/json") or content.get("*/*")
        if not isinstance(media, dict):
            media = next(
                (
                    value
                    for media_type, value in content.items()
                    if isinstance(value, dict)
                    and ("json" in str(media_type) or media_type == "*/*")
                ),
                None,
            )
        schema = media.get("schema") if isinstance(media, dict) else None
        if not isinstance(schema, dict):
            continue
        name = (
            _reference_name(str(schema["$ref"]))
            if "$ref" in schema
            else f"{titleize(str(action))}Response"
        )
        converted = _catalog_schema(document, schema, name)
        if converted is not None:
            return name, converted
    return None, None


def _sample_value(field: dict[str, Any]) -> Any:
    if field.get("enum"):
        return field["enum"][0]
    kind = str(field.get("type", "string"))
    if kind.endswith("[]"):
        return []
    if kind in {"int", "long"}:
        return 0
    if kind in {"double", "float", "decimal"}:
        return 0.0
    if kind == "bool":
        return False
    if kind == "object":
        return {}
    return ""


def scan(root: Path, service_name: str, document_path: Path | None = None) -> ScanResult:
    """Builds a :class:`ScanResult` from an OpenAPI document under ``root``."""
    result = ScanResult(framework="openapi", root=root)
    candidates = [document_path] if document_path else find_documents(root)
    if not candidates:
        result.warnings.append(
            "No openapi.json/yaml or swagger.json/yaml document was found."
        )
        return result

    path = candidates[0]
    if len(candidates) > 1:
        result.warnings.append(
            f"{len(candidates)} documents found; imported {relative_to(path, root)}."
        )
    document = load_document(path)
    source_file = relative_to(path, root)
    paths = document.get("paths")
    if not isinstance(paths, dict):
        result.warnings.append("The document has no 'paths' section.")
        return result

    for route, operations in sorted(paths.items()):
        if not isinstance(operations, dict):
            continue
        shared = operations.get("parameters") or []
        for verb, operation in operations.items():
            method = verb.upper()
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                continue

            tags = operation.get("tags") or []
            controller = (
                titleize(str(tags[0]))
                if tags
                else controller_from_path(str(route), service_name)
            )
            action = operation.get("operationId") or action_from(method, str(route))

            parameters: list[dict[str, Any]] = []
            for raw in list(shared) + list(operation.get("parameters") or []):
                parameter = _resolve(document, raw) if isinstance(raw, dict) else {}
                name = parameter.get("name")
                if not name:
                    continue
                schema = parameter.get("schema")
                schema = schema if isinstance(schema, dict) else {}
                enum_values = schema.get("enum")
                entry: dict[str, Any] = {
                    "name": str(name),
                    "source": str(parameter.get("in") or "query"),
                    "type": _schema_type(schema),
                    "required": bool(parameter.get("required")),
                }
                if parameter.get("description"):
                    entry["description"] = str(parameter["description"])
                if enum_values:
                    entry["enum"] = [str(value) for value in enum_values]
                parameters.append(entry)

            payload_schema_name = None
            form_schema_name = None
            payload: Any = None
            payload_type = None
            body = _resolve(document, operation.get("requestBody") or {})
            content = body.get("content")
            if isinstance(content, dict):
                for media_type, media in content.items():
                    if not isinstance(media, dict):
                        continue
                    schema = media.get("schema")
                    if not isinstance(schema, dict):
                        continue
                    name = (
                        _reference_name(str(schema["$ref"]))
                        if "$ref" in schema
                        else f"{titleize(str(action))}Request"
                    )
                    fields = _flatten_schema(document, schema)
                    if not fields:
                        continue
                    if "form" in media_type or "multipart" in media_type:
                        form_schema_name = name
                        result.form_schemas.setdefault(name, fields)
                    else:
                        payload_schema_name = name
                        payload_type = name
                        result.payload_schemas.setdefault(name, fields)
                        payload = {
                            key: _sample_value(value) for key, value in fields.items()
                        }
                    break

            statuses = [
                str(code)
                for code in (operation.get("responses") or {})
                if str(code).isdigit() and str(code).startswith("2")
            ]
            expected = statuses[0] if statuses else "200-299"
            response_schema_name, response_schema = _successful_response_schema(
                document, operation, str(action)
            )
            if response_schema_name and response_schema:
                result.response_schemas.setdefault(
                    response_schema_name, response_schema
                )

            result.endpoints.append(
                DiscoveredEndpoint(
                    service=service_name,
                    controller=controller or "Root",
                    action=str(action),
                    method=method,
                    path=str(route),
                    parameters=parameters,
                    payload=payload,
                    payload_type=payload_type,
                    payload_schema=payload_schema_name,
                    form_schema=form_schema_name,
                    response_schema=response_schema_name,
                    expected_status=expected,
                    source_file=source_file,
                    source_line=0,
                )
            )

    schemas = (document.get("components") or {}).get("schemas")
    if isinstance(schemas, dict):
        for name, schema in schemas.items():
            if not isinstance(schema, dict):
                continue
            enum_values = schema.get("enum")
            if enum_values:
                result.enums.setdefault(
                    str(name), [str(value) for value in enum_values]
                )

    if not result.endpoints:
        result.warnings.append("The document declared no usable operations.")
    return result
