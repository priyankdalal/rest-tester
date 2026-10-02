"""A mutable catalog document used by the visual catalog builder.

:mod:`api_tester.catalog` exposes a frozen, read-optimised view for the
request workspace. Building a catalog needs the opposite: an editable tree of
services, modules, and endpoints that round-trips to exactly the JSON shape
``load_catalog`` already understands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

from .scanners.base import DiscoveredEndpoint, ScanResult, endpoint_identity


SCHEMA_VERSION = 2

#: Default grammar for a brand-new catalog. Matches ``tools.generate_catalog``
#: exactly so a hand-built catalog and a generated one agree.
FILTER_GRAMMAR = {
    "separator": ":=",
    "and": ";",
    "or": "|",
    "value_delimiter": ",",
    "sort_descending_suffix": "-",
    "pattern": "Name__operator:=value[,value][;|]...",
}

HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
PARAMETER_SOURCES = ("path", "query", "header", "form", "body")

#: The four schema stores an endpoint can point at. ``enum`` is not referenced
#: by an endpoint directly; it supplies the ``lov`` of filter and payload
#: fields.
SCHEMA_KINDS = ("filter", "payload", "form", "response", "enum")

SCHEMA_LABELS = {
    "filter": "Filter entity",
    "payload": "Payload schema",
    "form": "Form schema",
    "response": "Response schema",
    "enum": "Enum",
}

#: ``FilterSortExtensions.GetDataType`` attribute types. Anything else is not
#: filterable, so the builder only offers these five.
FILTER_DATA_TYPES = ("Text", "Number", "Date", "Flag", "LOV")

#: ``FilterSortService.FilterService.SupportedOperations``. Mirrored from
#: ``tools.schema_extractor`` so the builder works without the generator
#: importable; ``tests/test_catalog_builder.py`` asserts the two stay equal.
FILTER_OPERATORS: dict[str, tuple[dict[str, str], ...]] = {
    "Text": (
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
        {"label": "Contains", "value": "ct"},
        {"label": "Does Not Contain", "value": "nct"},
        {"label": "Starts with", "value": "sw"},
        {"label": "Ends with", "value": "ew"},
        {"label": "In", "value": "in"},
        {"label": "Not in", "value": "nin"},
    ),
    "Number": (
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
        {"label": "Greater than", "value": "gt"},
        {"label": "Less than", "value": "lt"},
        {"label": "Greater than or equals to", "value": "gte"},
        {"label": "Less than or equals to", "value": "lte"},
        {"label": "In", "value": "in"},
        {"label": "Not in", "value": "nin"},
        {"label": "Between", "value": "bt"},
    ),
    "Date": (
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
        {"label": "Greater than", "value": "gt"},
        {"label": "Less than", "value": "lt"},
        {"label": "Greater than or equals to", "value": "gte"},
        {"label": "Less than or equals to", "value": "lte"},
        {"label": "Between", "value": "bt"},
    ),
    "Flag": (
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
    ),
    "LOV": (
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
    ),
}

#: Field kinds understood by :mod:`api_tester.builders`. ``array`` carries an
#: ``item``, ``object`` carries nested ``fields``, ``enum`` carries a ``lov``.
PAYLOAD_KINDS = (
    "text",
    "integer",
    "number",
    "boolean",
    "date",
    "guid",
    "json",
    "enum",
    "array",
    "object",
    "file",
    "file_list",
)

#: A reasonable CLR type to record for each kind when authoring by hand.
_KIND_CLR_TYPES = {
    "text": "string",
    "integer": "int",
    "number": "decimal",
    "boolean": "bool",
    "date": "DateTime",
    "guid": "Guid",
    "json": "object",
    "enum": "string",
    "array": "List<string>",
    "object": "object",
    "file": "IFormFile",
    "file_list": "List<IFormFile>",
}

_FILTER_CLR_TYPES = {
    "Text": "string",
    "Number": "int",
    "Date": "DateTime",
    "Flag": "bool",
    "LOV": "string",
}


class CatalogValidationError(ValueError):
    """Raised when a catalog document cannot be saved as-is."""


def operators_for(data_type: str) -> list[dict[str, str]]:
    """The operators the platform offers for a filter field of ``data_type``."""
    return [dict(item) for item in FILTER_OPERATORS.get(data_type, ())]


def default_clr_type(kind_or_data_type: str) -> str:
    """A sensible CLR type for a hand-authored field."""
    return _KIND_CLR_TYPES.get(
        kind_or_data_type, _FILTER_CLR_TYPES.get(kind_or_data_type, "string")
    )


def display_name_for(name: str) -> str:
    """``TrialLocationId`` -> ``Trial Location Id``, matching the extractor."""
    if not name:
        return ""
    words: list[str] = []
    current = name[0]
    for character in name[1:]:
        if character.isupper() and not current.endswith(" ") and not current[-1].isupper():
            words.append(current)
            current = character
        else:
            current += character
    words.append(current)
    return " ".join(word.strip() for word in words if word.strip())


def new_filter_field(name: str = "Id", data_type: str = "Number") -> dict[str, Any]:
    """A filter field with the operators the platform derives from its type."""
    if data_type not in FILTER_OPERATORS:
        data_type = "Text"
    return {
        "name": name,
        "display_name": display_name_for(name),
        "data_type": data_type,
        "clr_type": default_clr_type(data_type),
        "nullable": False,
        "sortable": True,
        "lov": [],
        "operators": operators_for(data_type),
    }


def new_filter_schema(name: str) -> dict[str, Any]:
    return {"name": name, "fields": [new_filter_field()]}


def new_payload_field(name: str = "Name", kind: str = "text") -> dict[str, Any]:
    """A payload/form field in the shape :mod:`api_tester.builders` expects."""
    if kind not in PAYLOAD_KINDS:
        kind = "text"
    field_value: dict[str, Any] = {
        "name": name,
        "clr_type": default_clr_type(kind),
        "nullable": False,
        "required": False,
        "display_name": display_name_for(name),
        "kind": kind,
    }
    if kind == "array":
        field_value["item"] = {"kind": "text", "clr_type": "string", "lov": []}
    elif kind == "object":
        field_value["fields"] = []
    elif kind == "enum":
        field_value["lov"] = []
    return field_value


def new_payload_schema(name: str) -> dict[str, Any]:
    return {
        "kind": "object",
        "name": name,
        "clr_type": name,
        "fields": [new_payload_field()],
    }


def new_form_schema(name: str) -> dict[str, Any]:
    """Form schemas have no ``kind``; they are always a flat object."""
    return {
        "name": name,
        "clr_type": name,
        "fields": [new_payload_field("File", "file")],
    }


def new_response_schema(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "clr_type": name,
        "fields": [new_payload_field()],
    }


def new_schema(kind: str, name: str) -> Any:
    """Builds an empty schema of ``kind`` in its store's native shape."""
    if kind == "filter":
        return new_filter_schema(name)
    if kind == "payload":
        return new_payload_schema(name)
    if kind == "form":
        return new_form_schema(name)
    if kind == "response":
        return new_response_schema(name)
    if kind == "enum":
        return []
    raise CatalogValidationError(f"Unknown schema kind: {kind}")


def default_sample(type_name: str, name: str) -> Any:
    """A placeholder request value matching the declared parameter type."""
    lowered = (type_name or "string").lower().rstrip("?")
    if lowered in {"int", "integer", "long", "short"}:
        return 1
    if lowered in {"double", "float", "decimal", "number"}:
        return 1.0
    if lowered in {"bool", "boolean"}:
        return True
    return f"test-{name}"


@dataclass
class EndpointDraft:
    """One editable endpoint. Field names mirror the catalog JSON exactly."""

    service: str
    controller: str
    action: str
    method: str = "GET"
    path: str = "/"
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
    #: The id as loaded from disk. Saved requests and suite cases reference
    #: endpoints by id, so a load/save cycle must never renumber them.
    identity: str = ""

    @property
    def id(self) -> str:
        return self.identity or self.computed_id()

    def computed_id(self) -> str:
        return endpoint_identity(
            self.service, self.controller, self.method, self.path, self.action
        )

    def refresh_identity(self) -> str:
        """Recomputes the id after the routing fields were edited."""
        self.identity = self.computed_id()
        return self.identity

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EndpointDraft":
        return cls(
            service=value.get("service", ""),
            controller=value.get("controller", ""),
            action=value.get("action", ""),
            method=str(value.get("method", "GET")).upper(),
            path=value.get("path", "/"),
            parameters=[dict(item) for item in value.get("parameters", [])],
            payload=value.get("payload"),
            payload_type=value.get("payload_type"),
            payload_schema=value.get("payload_schema"),
            form_schema=value.get("form_schema"),
            response_schema=value.get("response_schema"),
            filter_entity=value.get("filter_entity"),
            expected_status=value.get("expected_status", "200-299"),
            source_file=value.get("source_file", ""),
            source_line=value.get("source_line", 0),
            identity=value.get("id", ""),
        )

    @classmethod
    def from_discovered(cls, endpoint: DiscoveredEndpoint) -> "EndpointDraft":
        draft = cls.from_dict(endpoint.to_dict())
        # Scanners other than the .NET one do not invent sample values, but the
        # request workspace pre-fills inputs from them, so add them here rather
        # than in ``to_dict`` where they would rewrite a loaded catalog.
        for parameter in draft.parameters:
            if not parameter.get("sample"):
                parameter["sample"] = default_sample(
                    str(parameter.get("type", "string")),
                    str(parameter.get("name", "value")),
                )
        return draft

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
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

    def validate(self) -> list[str]:
        problems: list[str] = []
        # ASP.NET route constraints such as ``{id:int}`` are legal and the
        # request builder strips them, so compare on the bare name.
        placeholders = _path_placeholders(self.path)
        if not self.action.strip():
            problems.append(f"{self.label}: the action name is empty.")
        if self.method not in HTTP_METHODS:
            problems.append(f"{self.label}: '{self.method}' is not an HTTP method.")
        if not self.path.startswith("/"):
            problems.append(f"{self.label}: the path must start with '/'.")
        for parameter in self.parameters:
            name = str(parameter.get("name", "")).strip()
            if not name:
                problems.append(f"{self.label}: a parameter has no name.")
            source = str(parameter.get("source", ""))
            if source not in PARAMETER_SOURCES:
                problems.append(
                    f"{self.label}: parameter '{name}' has an unknown source "
                    f"'{source}'."
                )
            if source == "path" and name not in placeholders:
                problems.append(
                    f"{self.label}: path parameter '{name}' does not appear in "
                    f"the path as {{{name}}}."
                )
        for token in placeholders:
            if not any(
                str(item.get("name")) == token and item.get("source") == "path"
                for item in self.parameters
            ):
                problems.append(
                    f"{self.label}: the path contains {{{token}}} but no path "
                    f"parameter declares it, so the request can never be sent."
                )
        return problems


def _path_placeholders(path: str) -> list[str]:
    import re

    return [name.strip() for name in re.findall(r"\{([^{}:]+)", path)]


@dataclass
class ServiceDraft:
    """One editable service; modules are derived from endpoint controllers."""

    name: str
    repository: str = ""
    default_base_url: str = ""
    framework: str = ""
    endpoints: list[EndpointDraft] = field(default_factory=list)
    # Opt-in: the Filter/Sort builders assume the structured filter grammar.
    filter_sort_builders: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ServiceDraft":
        return cls(
            name=value.get("name", ""),
            repository=value.get("repository", ""),
            default_base_url=value.get("default_base_url", ""),
            framework=value.get("framework", ""),
            endpoints=[
                EndpointDraft.from_dict(item) for item in value.get("endpoints", [])
            ],
            filter_sort_builders=bool(value.get("filter_sort_builders", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "name": self.name,
            "repository": self.repository,
            "default_base_url": self.default_base_url,
            "endpoints": [endpoint.to_dict() for endpoint in self.endpoints],
        }
        if self.framework:
            document["framework"] = self.framework
        if self.filter_sort_builders:
            document["filter_sort_builders"] = True
        return document

    def sort_endpoints(self) -> None:
        self.endpoints.sort(
            key=lambda item: (item.controller, item.path, item.method, item.action)
        )

    def module_names(self) -> list[str]:
        return sorted({endpoint.controller for endpoint in self.endpoints})

    def module(self, name: str) -> list[EndpointDraft]:
        return [
            endpoint for endpoint in self.endpoints if endpoint.controller == name
        ]

    def rename_module(self, old: str, new: str) -> int:
        changed = 0
        for endpoint in self.endpoints:
            if endpoint.controller == old:
                endpoint.controller = new
                endpoint.refresh_identity()
                changed += 1
        return changed

    def remove_module(self, name: str) -> int:
        before = len(self.endpoints)
        self.endpoints = [
            endpoint for endpoint in self.endpoints if endpoint.controller != name
        ]
        return before - len(self.endpoints)


@dataclass
class CatalogDocument:
    """A whole catalog under construction."""

    services: list[ServiceDraft] = field(default_factory=list)
    filter_schemas: dict[str, Any] = field(default_factory=dict)
    payload_schemas: dict[str, Any] = field(default_factory=dict)
    form_schemas: dict[str, Any] = field(default_factory=dict)
    response_schemas: dict[str, Any] = field(default_factory=dict)
    enums: dict[str, list[str]] = field(default_factory=dict)
    name: str = ""
    workspace_root: str = ""
    filter_grammar: dict[str, Any] = field(
        default_factory=lambda: dict(FILTER_GRAMMAR)
    )
    path: Path | None = None

    # ---------------------------------------------------------------- loading

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> "CatalogDocument":
        return cls(
            services=[
                ServiceDraft.from_dict(item) for item in document.get("services", [])
            ],
            filter_schemas=dict(document.get("filter_schemas", {})),
            payload_schemas=dict(document.get("payload_schemas", {})),
            form_schemas=dict(document.get("form_schemas", {})),
            response_schemas=dict(document.get("response_schemas", {})),
            enums=dict(document.get("enums", {})),
            name=str(document.get("name") or "").strip(),
            workspace_root=document.get("workspace_root", ""),
            # Preserved verbatim: the generator writes a richer grammar block
            # than the builder's default, and rewriting it would show up as
            # catalog drift on the very next save.
            filter_grammar=dict(document.get("filter_grammar") or FILTER_GRAMMAR),
        )

    @classmethod
    def load(cls, path: Path) -> "CatalogDocument":
        path = Path(path)
        with path.open(encoding="utf-8") as stream:
            document = json.load(stream)
        if not isinstance(document, dict) or "services" not in document:
            raise CatalogValidationError(
                f"{path.name} is not an API catalog: no 'services' key was found."
            )
        built = cls.from_dict(document)
        built.path = path
        return built

    # ---------------------------------------------------------------- writing

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "name": self.name,
            "workspace_root": self.workspace_root,
            "filter_grammar": self.filter_grammar,
            "services": [service.to_dict() for service in self.services],
            "filter_schemas": self.filter_schemas,
            "payload_schemas": self.payload_schemas,
            "form_schemas": self.form_schemas,
            "response_schemas": self.response_schemas,
            "enums": self.enums,
        }

    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise CatalogValidationError("No destination path was given.")
        problems = self.validate()
        if problems:
            raise CatalogValidationError("\n".join(problems))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        self.path = target
        return target

    # ------------------------------------------------------------- inspection

    @property
    def endpoint_count(self) -> int:
        return sum(len(service.endpoints) for service in self.services)

    def service_names(self) -> list[str]:
        return [service.name for service in self.services]

    def service(self, name: str) -> ServiceDraft | None:
        for service in self.services:
            if service.name == name:
                return service
        return None

    def iter_endpoints(self) -> Iterator[EndpointDraft]:
        for service in self.services:
            yield from service.endpoints

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.services:
            problems.append("The catalog has no services.")
        seen_names: set[str] = set()
        for service in self.services:
            if not service.name.strip():
                problems.append("A service has an empty name.")
            elif service.name in seen_names:
                problems.append(f"Duplicate service name: {service.name}.")
            seen_names.add(service.name)
            if not service.endpoints:
                problems.append(f"Service '{service.name}' has no endpoints.")
            identities: dict[str, str] = {}
            for endpoint in service.endpoints:
                problems.extend(
                    f"{service.name}: {problem}" for problem in endpoint.validate()
                )
                identity = endpoint.id
                if identity in identities:
                    problems.append(
                        f"{service.name}: '{endpoint.label}' duplicates "
                        f"'{identities[identity]}'; endpoints must differ by "
                        f"method, path, or action."
                    )
                identities[identity] = endpoint.label
        problems.extend(self.validate_schemas())
        return problems

    # --------------------------------------------------------------- mutation

    def add_service(
        self,
        name: str,
        repository: str = "",
        default_base_url: str = "",
        framework: str = "",
    ) -> ServiceDraft:
        if self.service(name) is not None:
            raise CatalogValidationError(f"A service named '{name}' already exists.")
        service = ServiceDraft(
            name=name,
            repository=repository,
            default_base_url=default_base_url,
            framework=framework,
        )
        self.services.append(service)
        return service

    def rename_service(self, old: str, new: str) -> None:
        service = self.service(old)
        if service is None:
            raise CatalogValidationError(f"No service named '{old}'.")
        if old != new and self.service(new) is not None:
            raise CatalogValidationError(f"A service named '{new}' already exists.")
        service.name = new
        for endpoint in service.endpoints:
            endpoint.service = new
            endpoint.refresh_identity()

    def remove_service(self, name: str) -> bool:
        service = self.service(name)
        if service is None:
            return False
        self.services.remove(service)
        return True

    def add_endpoint(self, service_name: str, endpoint: EndpointDraft) -> EndpointDraft:
        service = self.service(service_name)
        if service is None:
            raise CatalogValidationError(f"No service named '{service_name}'.")
        endpoint.service = service_name
        service.endpoints.append(endpoint)
        return endpoint

    def remove_endpoint(self, service_name: str, endpoint: EndpointDraft) -> bool:
        service = self.service(service_name)
        if service is None or endpoint not in service.endpoints:
            return False
        service.endpoints.remove(endpoint)
        return True

    def merge_scan(
        self,
        service_name: str,
        result: ScanResult,
        repository: str = "",
        default_base_url: str = "",
        replace: bool = True,
    ) -> ServiceDraft:
        """Folds a :class:`ScanResult` into the document as one service.

        Shared schema dictionaries use first-definition-wins so a later scan
        can never silently redefine a schema an earlier service already
        depends on.
        """
        service = self.service(service_name)
        if service is None:
            service = self.add_service(
                service_name,
                repository=repository or str(result.root),
                default_base_url=default_base_url,
                framework=result.framework,
            )
        else:
            service.framework = result.framework or service.framework
            if repository:
                service.repository = repository
            if default_base_url:
                service.default_base_url = default_base_url

        drafts = [
            EndpointDraft.from_discovered(endpoint) for endpoint in result.endpoints
        ]
        for draft in drafts:
            draft.service = service_name
            draft.refresh_identity()
        if replace:
            service.endpoints = drafts
        else:
            known = {endpoint.id for endpoint in service.endpoints}
            service.endpoints.extend(
                draft for draft in drafts if draft.id not in known
            )
        service.sort_endpoints()

        for name, schema in result.filter_schemas.items():
            self.filter_schemas.setdefault(name, schema)
        for name, schema in result.payload_schemas.items():
            self.payload_schemas.setdefault(name, schema)
        for name, schema in result.form_schemas.items():
            self.form_schemas.setdefault(name, schema)
        for name, schema in result.response_schemas.items():
            self.response_schemas.setdefault(name, schema)
        for name, values in result.enums.items():
            self.enums.setdefault(name, values)
        return service

    def autodetect_response_schema(self, endpoint_id: str) -> str | None:
        service: ServiceDraft | None = None
        endpoint: EndpointDraft | None = None
        for candidate in self.services:
            for item in candidate.endpoints:
                if item.id == endpoint_id:
                    service = candidate
                    endpoint = item
                    break
            if endpoint is not None:
                break
        if service is None or endpoint is None:
            raise CatalogValidationError(f"No endpoint with id '{endpoint_id}' exists.")
        repository_value = service.repository.strip()
        if not repository_value:
            raise CatalogValidationError(
                f"Service '{service.name}' has no repository folder configured."
            )
        repository = Path(repository_value)
        if not repository.is_absolute() and self.workspace_root.strip():
            repository = Path(self.workspace_root) / repository
        if not repository.is_dir():
            raise CatalogValidationError(
                f"The repository folder does not exist:\n{repository}"
            )

        from .scanners.dotnet import find_shared_root
        from tools.generate_catalog import scan_dotnet_project

        scanned = scan_dotnet_project(service.name, repository, find_shared_root(repository))
        target = next(
            (
                item
                for item in scanned.get("endpoints", [])
                if item.get("id") == endpoint.id
                or (
                    endpoint.source_file
                    and endpoint.source_line
                    and item.get("source_file") == endpoint.source_file
                    and int(item.get("source_line", 0) or 0) == endpoint.source_line
                )
                or endpoint_identity(
                    service.name,
                    str(item.get("controller", "")),
                    str(item.get("method", "")),
                    str(item.get("path", "")),
                    str(item.get("action", "")),
                )
                == endpoint.id
            ),
            None,
        )
        if not isinstance(target, dict):
            return None
        schema_name = str(target.get("response_schema") or "").strip() or None
        schemas = scanned.get("response_schemas")
        if not schema_name or not isinstance(schemas, dict) or schema_name not in schemas:
            return None
        schema = schemas[schema_name]
        self.response_schemas[schema_name] = json.loads(json.dumps(schema))
        endpoint.response_schema = schema_name
        return schema_name

    # ----------------------------------------------------------- schema CRUD

    #: The endpoint attribute each schema kind is referenced by.
    _SCHEMA_REFERENCE = {
        "filter": "filter_entity",
        "payload": "payload_schema",
        "form": "form_schema",
        "response": "response_schema",
    }

    def schema_store(self, kind: str) -> dict[str, Any]:
        """The backing dictionary for a schema kind."""
        stores = {
            "filter": self.filter_schemas,
            "payload": self.payload_schemas,
            "form": self.form_schemas,
            "response": self.response_schemas,
            "enum": self.enums,
        }
        try:
            return stores[kind]
        except KeyError:
            raise CatalogValidationError(f"Unknown schema kind: {kind}") from None

    def schema_names(self, kind: str) -> list[str]:
        return sorted(self.schema_store(kind))

    def schema(self, kind: str, name: str) -> Any:
        return self.schema_store(kind).get(name)

    def schema_usage(self, kind: str, name: str) -> list[EndpointDraft]:
        """Endpoints that reference the schema, so deletes are never silent."""
        if kind == "enum":
            return []
        attribute = self._SCHEMA_REFERENCE[kind]
        return [
            endpoint
            for endpoint in self.iter_endpoints()
            if getattr(endpoint, attribute) == name
        ]

    def enum_usage(self, name: str) -> list[str]:
        """Field paths whose list of values comes from ``name``.

        An enum is referenced by value rather than by name: the extractor
        copies its members into a field's ``lov``. A field is therefore treated
        as a user when it declares the enum as its CLR type. A parameter, by
        contrast, references an enum by name and so is matched exactly.
        """
        users: list[str] = []
        for endpoint in self.iter_endpoints():
            for parameter in endpoint.parameters:
                if parameter.get("enum") == name:
                    users.append(f"parameter:{endpoint.label}.{parameter.get('name')}")
        for kind in ("filter", "payload", "form", "response"):
            for schema_name, schema in self.schema_store(kind).items():
                for path, enum in _schema_enum_references(schema):
                    if enum == name:
                        users.append(f"{kind}:{schema_name}.{path}")
        for schema_name, schema in self.filter_schemas.items():
            for item in schema.get("fields", []):
                if item.get("enum"):
                    continue  # Already reported by its explicit reference.
                if _names_type(item.get("clr_type"), name):
                    users.append(f"filter:{schema_name}.{item.get('name')}")
        for kind, store in (
            ("payload", self.payload_schemas),
            ("form", self.form_schemas),
            ("response", self.response_schemas),
        ):
            for schema_name, schema in store.items():
                for path in _enum_field_paths(schema, name):
                    entry = f"{kind}:{schema_name}.{path}"
                    if entry not in users:
                        users.append(entry)
        return users

    def add_schema(self, kind: str, name: str) -> Any:
        """Creates an empty schema, refusing to clobber an existing name."""
        name = name.strip()
        if not name:
            raise CatalogValidationError("A schema needs a name.")
        store = self.schema_store(kind)
        if name in store:
            raise CatalogValidationError(f"'{name}' already exists.")
        store[name] = new_schema(kind, name)
        return store[name]

    def set_schema(self, kind: str, name: str, value: Any) -> None:
        """Replaces a schema's body, keeping its inner ``name`` consistent."""
        store = self.schema_store(kind)
        if kind != "enum" and isinstance(value, dict):
            value["name"] = name
        store[name] = value

    def duplicate_schema(self, kind: str, name: str, new_name: str) -> Any:
        new_name = new_name.strip()
        store = self.schema_store(kind)
        if name not in store:
            raise CatalogValidationError(f"'{name}' does not exist.")
        if not new_name:
            raise CatalogValidationError("A schema needs a name.")
        if new_name in store:
            raise CatalogValidationError(f"'{new_name}' already exists.")
        copy = json.loads(json.dumps(store[name]))
        if isinstance(copy, dict):
            copy["name"] = new_name
        store[new_name] = copy
        return copy

    def rename_schema(self, kind: str, old: str, new: str) -> int:
        """Renames a schema and repoints every endpoint that referenced it."""
        new = new.strip()
        store = self.schema_store(kind)
        if old not in store:
            raise CatalogValidationError(f"'{old}' does not exist.")
        if not new:
            raise CatalogValidationError("A schema needs a name.")
        if new == old:
            return 0
        if new in store:
            raise CatalogValidationError(f"'{new}' already exists.")
        value = store.pop(old)
        if isinstance(value, dict):
            value["name"] = new
        store[new] = value
        if kind == "enum":
            # Parameters and schema fields reference enums by name, so they
            # must follow the rename.
            repointed = 0
            for endpoint in self.iter_endpoints():
                for parameter in endpoint.parameters:
                    if parameter.get("enum") == old:
                        parameter["enum"] = new
                        repointed += 1
            for store in (
                self.filter_schemas,
                self.payload_schemas,
                self.form_schemas,
                self.response_schemas,
            ):
                for schema in store.values():
                    repointed += _retarget_enum(schema, old, new, [])
            return repointed
        attribute = self._SCHEMA_REFERENCE[kind]
        repointed = 0
        for endpoint in self.iter_endpoints():
            if getattr(endpoint, attribute) == old:
                setattr(endpoint, attribute, new)
                repointed += 1
        return repointed

    def remove_schema(self, kind: str, name: str) -> int:
        """Deletes a schema and clears the references that would dangle."""
        store = self.schema_store(kind)
        if name not in store:
            return 0
        value = store[name]
        del store[name]
        if kind == "enum":
            # Freeze the members into each user rather than silently dropping
            # the value list the author chose. Schema fields already cache the
            # members in ``lov``, so only the reference has to go.
            members = [str(item) for item in (value or [])]
            cleared = 0
            for endpoint in self.iter_endpoints():
                for parameter in endpoint.parameters:
                    if parameter.get("enum") == name:
                        del parameter["enum"]
                        if members:
                            parameter["values"] = list(members)
                        cleared += 1
            for store in (
                self.filter_schemas,
                self.payload_schemas,
                self.form_schemas,
                self.response_schemas,
            ):
                for schema in store.values():
                    cleared += _retarget_enum(schema, name, None, members)
            return cleared
        attribute = self._SCHEMA_REFERENCE[kind]
        cleared = 0
        for endpoint in self.iter_endpoints():
            if getattr(endpoint, attribute) == name:
                setattr(endpoint, attribute, None)
                cleared += 1
        return cleared

    def validate_schemas(self) -> list[str]:
        """Reports dangling references and structurally unusable schemas."""
        problems: list[str] = []
        for kind, attribute in self._SCHEMA_REFERENCE.items():
            store = self.schema_store(kind)
            for endpoint in self.iter_endpoints():
                reference = getattr(endpoint, attribute)
                if reference and reference not in store:
                    problems.append(
                        f"{endpoint.service}: '{endpoint.label}' references "
                        f"missing {SCHEMA_LABELS[kind].lower()} '{reference}'."
                    )
        for endpoint in self.iter_endpoints():
            for parameter in endpoint.parameters:
                enum = parameter.get("enum")
                if enum and enum not in self.enums:
                    problems.append(
                        f"{endpoint.service}: '{endpoint.label}' parameter "
                        f"'{parameter.get('name')}' references missing enum "
                        f"'{enum}', so it would offer no values."
                    )
        for kind in ("filter", "payload", "form", "response"):
            for schema_name, schema in self.schema_store(kind).items():
                for path, enum in _schema_enum_references(schema):
                    if enum not in self.enums:
                        problems.append(
                            f"{SCHEMA_LABELS[kind]} '{schema_name}': field "
                            f"'{path}' references missing enum '{enum}', so it "
                            f"would offer no values."
                        )
        for name, schema in self.filter_schemas.items():
            fields = schema.get("fields") if isinstance(schema, dict) else None
            if not fields:
                problems.append(f"Filter entity '{name}' has no fields.")
                continue
            for item in fields:
                if not str(item.get("name", "")).strip():
                    problems.append(f"Filter entity '{name}' has an unnamed field.")
                if item.get("data_type") not in FILTER_OPERATORS:
                    problems.append(
                        f"Filter entity '{name}': field '{item.get('name')}' has "
                        f"unsupported data type '{item.get('data_type')}'."
                    )
                elif not item.get("operators"):
                    problems.append(
                        f"Filter entity '{name}': field '{item.get('name')}' has "
                        f"no operators, so it cannot be filtered on."
                    )
        for kind, store in (
            ("payload", self.payload_schemas),
            ("form", self.form_schemas),
            ("response", self.response_schemas),
        ):
            for name, schema in store.items():
                if not isinstance(schema, dict):
                    problems.append(f"{SCHEMA_LABELS[kind]} '{name}' is not an object.")
                    continue
                if schema.get("kind") == "array":
                    continue
                if not schema.get("fields"):
                    problems.append(f"{SCHEMA_LABELS[kind]} '{name}' has no fields.")
                    continue
                for item in schema["fields"]:
                    if not str(item.get("name", "")).strip():
                        problems.append(
                            f"{SCHEMA_LABELS[kind]} '{name}' has an unnamed field."
                        )
                    if item.get("kind") not in PAYLOAD_KINDS:
                        problems.append(
                            f"{SCHEMA_LABELS[kind]} '{name}': field "
                            f"'{item.get('name')}' has unknown kind "
                            f"'{item.get('kind')}'."
                        )
        return problems

    def prune_unused_schemas(self) -> int:
        """Drops schemas and enums no endpoint refers to."""
        used_filters = {
            endpoint.filter_entity
            for endpoint in self.iter_endpoints()
            if endpoint.filter_entity
        }
        used_payloads = {
            endpoint.payload_schema
            for endpoint in self.iter_endpoints()
            if endpoint.payload_schema
        }
        used_forms = {
            endpoint.form_schema
            for endpoint in self.iter_endpoints()
            if endpoint.form_schema
        }
        used_responses = {
            endpoint.response_schema
            for endpoint in self.iter_endpoints()
            if endpoint.response_schema
        }
        removed = 0
        for store, used in (
            (self.filter_schemas, used_filters),
            (self.payload_schemas, used_payloads),
            (self.form_schemas, used_forms),
            (self.response_schemas, used_responses),
        ):
            for name in [key for key in store if key not in used]:
                del store[name]
                removed += 1
        return removed


def _enum_references(
    node: Any, path: str = "", results: list[tuple[str, str]] | None = None
) -> list[tuple[str, str]]:
    """Every ``(field path, enum name)`` pair inside a schema's fields."""
    results = [] if results is None else results
    if isinstance(node, list):
        for item in node:
            _enum_references(item, path, results)
        return results
    if not isinstance(node, dict):
        return results
    name = str(node.get("name", "")).strip()
    here = f"{path}.{name}" if path and name else (name or path)
    if isinstance(node.get("enum"), str):
        results.append((here or "?", node["enum"]))
    for key in ("fields", "item"):
        if key in node:
            _enum_references(node[key], here, results)
    return results


def _schema_enum_references(schema: Any) -> list[tuple[str, str]]:
    """Enum references inside a schema, rooted at its fields."""
    if not isinstance(schema, dict):
        return []
    return _enum_references(schema.get("fields", []))


def _retarget_enum(node: Any, old: str, new: str | None, members: list[str]) -> int:
    """Renames (``new``) or freezes (``None``) every reference to ``old``.

    Freezing copies the members into ``lov`` so the field keeps offering the
    same values; the cached ``lov`` may be stale or empty, so it cannot be
    relied on.
    """
    changed = 0
    if isinstance(node, list):
        for item in node:
            changed += _retarget_enum(item, old, new, members)
        return changed
    if not isinstance(node, dict):
        return changed
    if node.get("enum") == old:
        if new is None:
            del node["enum"]
            node["lov"] = list(members)
        else:
            node["enum"] = new
        changed += 1
    for key in ("fields", "item"):
        if key in node:
            changed += _retarget_enum(node[key], old, new, members)
    return changed


def _names_type(clr_type: Any, name: str) -> bool:
    """True when ``clr_type`` denotes ``name``, bare or inside a collection."""
    if not isinstance(clr_type, str) or not clr_type:
        return False
    cleaned = clr_type.replace("global::", "").strip().rstrip("?")
    if cleaned.endswith("[]"):
        cleaned = cleaned[:-2]
    if "<" in cleaned and cleaned.endswith(">"):
        cleaned = cleaned[cleaned.index("<") + 1 : -1]
    return cleaned.split(".")[-1].strip().rstrip("?") == name


def _enum_field_paths(schema: Any, enum_name: str, prefix: str = "") -> list[str]:
    """Dotted paths of fields in ``schema`` typed as ``enum_name``."""
    found: list[str] = []
    if not isinstance(schema, dict):
        return found
    for item in schema.get("fields", []) or []:
        if not isinstance(item, dict):
            continue
        path = f"{prefix}{item.get('name', '')}"
        if _names_type(item.get("clr_type"), enum_name):
            found.append(path)
        nested = item.get("item")
        if isinstance(nested, dict) and _names_type(nested.get("clr_type"), enum_name):
            found.append(f"{path}[]")
        found.extend(_enum_field_paths(item, enum_name, f"{path}."))
        if isinstance(nested, dict):
            found.extend(_enum_field_paths(nested, enum_name, f"{path}[]."))
    return found


def merge_documents(documents: Iterable[CatalogDocument]) -> CatalogDocument:
    """Combines several catalogs, keeping the first definition of a name."""
    merged = CatalogDocument()
    for document in documents:
        if not merged.name and document.name:
            merged.name = document.name
        for service in document.services:
            if merged.service(service.name) is None:
                merged.services.append(service)
        for name, schema in document.filter_schemas.items():
            merged.filter_schemas.setdefault(name, schema)
        for name, schema in document.payload_schemas.items():
            merged.payload_schemas.setdefault(name, schema)
        for name, schema in document.form_schemas.items():
            merged.form_schemas.setdefault(name, schema)
        for name, schema in document.response_schemas.items():
            merged.response_schemas.setdefault(name, schema)
        for name, values in document.enums.items():
            merged.enums.setdefault(name, values)
    return merged
