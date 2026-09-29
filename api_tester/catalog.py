from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Parameter:
    name: str
    source: str
    type: str
    required: bool
    sample: Any = ""
    values: tuple[str, ...] = ()
    enum: str | None = None

    @classmethod
    def from_dict(
        cls, value: dict[str, Any], enums: dict[str, list[str]] | None = None
    ) -> "Parameter":
        enum = value.get("enum") or None
        allowed = value.get("values")
        if allowed is None and enum is not None:
            # An enum reference stays live: editing the enum updates the parameter.
            allowed = (enums or {}).get(enum, [])
        return cls(
            name=value["name"],
            source=value["source"],
            type=value["type"],
            required=value.get("required", False),
            sample=value.get("sample", ""),
            values=tuple(str(item) for item in (allowed or [])),
            enum=enum,
        )


@dataclass(frozen=True)
class Endpoint:
    id: str
    service: str
    controller: str
    action: str
    method: str
    path: str
    parameters: tuple[Parameter, ...] = field(default_factory=tuple)
    payload: Any = None
    payload_type: str | None = None
    payload_schema: str | None = None
    form_schema: str | None = None
    response_schema: str | None = None
    filter_entity: str | None = None
    expected_status: str = "200-299"
    source_file: str = ""
    source_line: int = 0

    @classmethod
    def from_dict(
        cls, value: dict[str, Any], enums: dict[str, list[str]] | None = None
    ) -> "Endpoint":
        return cls(
            id=value["id"],
            service=value["service"],
            controller=value["controller"],
            action=value["action"],
            method=value["method"],
            path=value["path"],
            parameters=tuple(
                Parameter.from_dict(item, enums) for item in value.get("parameters", [])
            ),
            payload=value.get("payload"),
            payload_type=value.get("payload_type"),
            payload_schema=value.get("payload_schema"),
            form_schema=value.get("form_schema"),
            response_schema=value.get("response_schema"),
            filter_entity=value.get("filter_entity"),
            expected_status=value.get("expected_status", "200-299"),
            source_file=value.get("source_file", ""),
            source_line=value.get("source_line", 0),
        )


@dataclass(frozen=True)
class Service:
    name: str
    repository: str
    default_base_url: str
    endpoints: tuple[Endpoint, ...]

    @classmethod
    def from_dict(
        cls, value: dict[str, Any], enums: dict[str, list[str]] | None = None
    ) -> "Service":
        return cls(
            name=value["name"],
            repository=value["repository"],
            default_base_url=value.get("default_base_url", ""),
            endpoints=tuple(
                Endpoint.from_dict(item, enums) for item in value["endpoints"]
            ),
        )


@dataclass(frozen=True)
class Catalog:
    services: tuple[Service, ...]
    filter_schemas: dict[str, Any]
    payload_schemas: dict[str, Any]
    enums: dict[str, list[str]]
    form_schemas: dict[str, Any] = field(default_factory=dict)
    response_schemas: dict[str, Any] = field(default_factory=dict)
    name: str = ""

    def filter_schema(self, name: str | None):
        from .schema import FilterSchema

        if not name or name not in self.filter_schemas:
            return None
        return FilterSchema.from_dict(self.filter_schemas[name])

    def payload_schema(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        return self.payload_schemas.get(name)

    def form_schema(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        return self.form_schemas.get(name)

    def response_schema(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        return self.response_schemas.get(name)


def _resolve_enum_references(node: Any, enums: dict[str, list[str]]) -> None:
    """Rewrites every ``enum`` reference in a schema into a concrete ``lov``.

    Doing this once at load time keeps the reference live — editing an enum
    updates every field that names it — while leaving every consumer of ``lov``
    untouched.
    """
    if isinstance(node, list):
        for item in node:
            _resolve_enum_references(item, enums)
        return
    if not isinstance(node, dict):
        return
    name = node.get("enum")
    if isinstance(name, str):
        node["lov"] = [str(value) for value in enums.get(name, [])]
    for key in ("fields", "item"):
        if key in node:
            _resolve_enum_references(node[key], enums)


def load_catalog(path: Path) -> Catalog:
    with path.open(encoding="utf-8") as stream:
        document = json.load(stream)
    enums = document.get("enums", {})
    filter_schemas = document.get("filter_schemas", {})
    payload_schemas = document.get("payload_schemas", {})
    form_schemas = document.get("form_schemas", {})
    response_schemas = document.get("response_schemas", {})
    for store in (filter_schemas, payload_schemas, form_schemas, response_schemas):
        for schema in store.values():
            _resolve_enum_references(schema, enums)
    return Catalog(
        services=tuple(Service.from_dict(item, enums) for item in document["services"]),
        filter_schemas=filter_schemas,
        payload_schemas=payload_schemas,
        enums=enums,
        form_schemas=form_schemas,
        response_schemas=response_schemas,
        name=str(document.get("name") or "").strip(),
    )
