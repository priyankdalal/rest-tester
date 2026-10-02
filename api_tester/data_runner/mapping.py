"""CSV-to-request mapping helpers for the Data Runner.

This module implements endpoint-aware mapping targets and row resolution for
the Data Runner design in
``platform architecture guide: load-testing-and-data-runner.md``
sections 9.4-9.9. It intentionally handles only the transform/application
step from the section 9.6 precedence chain; callers decide how mapped values,
template defaults, environment variables, and optional seeding are combined.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from api_tester.catalog import Catalog, Endpoint
from api_tester.execution.models import RequestTemplate
from api_tester.schema import FilterSchema, encode_filter, encode_sort

OMIT = object()

TRANSFORM_KINDS = (
    "trim",
    "required",
    "default",
    "to_integer",
    "to_number",
    "to_boolean",
    "date_format",
    "enum_validate",
    "json_parse",
    "split_list",
    "prefix_suffix",
    "env_fallback",
    "empty_is_null",
    "empty_is_omitted",
    "seed_when_empty",
)

_TRUTHY = {"true", "1", "yes", "skip"}
_SORT_DESCENDING = {"true", "1", "yes", "desc"}


@dataclass(frozen=True)
class MappingTarget:
    """One schema-aware destination a CSV column may populate.

    Filter targets are emitted as ``filter:<field-name>:eq`` by default so the
    UI can offer a concrete operator choice. A mapping author may substitute a
    different valid operator suffix (for example ``filter:Brand.Name:ct``).
    """

    key: str
    label: str
    group: str
    required: bool
    clr_type: str
    allowed_values: tuple[str, ...] = ()


def _title_group(group: str) -> str:
    titles = {
        "path": "Path",
        "query": "Query",
        "header": "Header",
        "form": "Form",
        "filter": "Filter",
        "sort": "Sort",
        "payload": "Payload",
        "meta": "Meta",
    }
    return titles.get(group, group.title())


def _parameter_targets(endpoint: Endpoint) -> list[MappingTarget]:
    targets: list[MappingTarget] = []
    for parameter in endpoint.parameters:
        if parameter.source not in {"path", "query", "header", "form"}:
            continue
        required = parameter.required or parameter.source == "path"
        label = f"{_title_group(parameter.source)} • {parameter.name}"
        if required:
            label += " (required)"
        targets.append(
            MappingTarget(
                key=f"{parameter.source}:{parameter.name}",
                label=label,
                group=parameter.source,
                required=required,
                clr_type=parameter.type,
                allowed_values=tuple(str(item) for item in parameter.values),
            )
        )
    return targets


def _payload_targets_from_node(
    node: dict[str, Any] | None,
    prefix: list[str],
    *,
    is_root: bool = False,
) -> list[MappingTarget]:
    if not node:
        return []
    kind = str(node.get("kind") or ("object" if node.get("fields") else "text")).lower()
    if is_root and kind in {"array", "json"}:
        clr_type = str(node.get("clr_type", ""))
        return [
            MappingTarget(
                key="payload:",
                label="Payload",
                group="payload",
                required=False,
                clr_type=clr_type,
                allowed_values=(),
            )
        ]
    if kind == "object" and node.get("fields"):
        targets: list[MappingTarget] = []
        field_prefix = prefix
        if not is_root and node.get("name"):
            field_prefix = prefix + [str(node.get("name"))]
        for field in node.get("fields", []):
            targets.extend(_payload_targets_from_node(field, field_prefix))
        return targets
    name = str(node.get("name") or "")
    path = prefix + ([name] if name else [])
    if kind == "array" and node.get("item") and name:
        path_text = ".".join(path)
        return [
            MappingTarget(
                key=f"payload:{path_text}",
                label=f"Payload • {path_text}",
                group="payload",
                required=bool(node.get("required")),
                clr_type=str(node.get("clr_type", "")),
                allowed_values=tuple(str(item) for item in (node.get("lov") or ())),
            )
        ]
    path_text = ".".join(path)
    return [
        MappingTarget(
            key=f"payload:{path_text}",
            label=f"Payload • {path_text}",
            group="payload",
            required=bool(node.get("required")),
            clr_type=str(node.get("clr_type", "")),
            allowed_values=tuple(str(item) for item in (node.get("lov") or ())),
        )
    ]


def _filter_targets(schema: FilterSchema | None) -> list[MappingTarget]:
    if schema is None:
        return []
    targets: list[MappingTarget] = []
    for field in schema.fields:
        operators = ", ".join(operator.value for operator in field.operators)
        label = f"Filter • {field.name}"
        if operators:
            label += f" ({operators})"
        targets.append(
            MappingTarget(
                key=f"filter:{field.name}:eq",
                label=label,
                group="filter",
                required=False,
                clr_type=field.clr_type or field.data_type,
                allowed_values=tuple(str(item) for item in field.lov),
            )
        )
        if field.sortable:
            targets.append(
                MappingTarget(
                    key=f"sort:{field.name}",
                    label=f"Sort • {field.name}",
                    group="sort",
                    required=False,
                    clr_type=field.clr_type or field.data_type,
                    allowed_values=(),
                )
            )
    return targets


def mapping_targets(endpoint: Endpoint, catalog: Catalog) -> list[MappingTarget]:
    targets = _parameter_targets(endpoint)
    targets.extend(_filter_targets(catalog.endpoint_filter_schema(endpoint)))
    targets.extend(
        _payload_targets_from_node(
            catalog.payload_schema(endpoint.payload_schema),
            [],
            is_root=True,
        )
    )
    targets.extend(
        [
            MappingTarget("expected_status", "Expected status", "meta", False, ""),
            MappingTarget("correlation_key", "Correlation key", "meta", False, ""),
            MappingTarget("skip_row", "Skip row", "meta", False, ""),
        ]
    )
    return targets


@dataclass(frozen=True)
class Transform:
    kind: str
    options: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in TRANSFORM_KINDS:
            raise ValueError(f"Unsupported transform kind: {self.kind}")
        object.__setattr__(self, "options", dict(self.options))


def _is_empty(value: Any) -> bool:
    return value is OMIT or value is None or value == "" or value == []


def _format_allowed_values(allowed_values: tuple[str, ...]) -> str:
    if len(allowed_values) <= 5:
        return ", ".join(allowed_values)
    return ", ".join(allowed_values[:5]) + ", ..."


def apply_transforms(
    raw_value: str,
    transforms: list[Transform],
    *,
    environment_variables: dict[str, str] | None = None,
    allowed_values: tuple[str, ...] = (),
) -> tuple[Any, str | None]:
    current: Any = raw_value
    environment_variables = environment_variables or {}
    for transform in transforms:
        if transform.kind == "trim":
            if isinstance(current, str):
                current = current.strip()
            continue
        if transform.kind == "required":
            if _is_empty(current):
                return None, "Value is required."
            continue
        if transform.kind == "default":
            if _is_empty(current):
                current = transform.options.get("default", "")
            continue
        if transform.kind == "env_fallback":
            if _is_empty(current):
                current = environment_variables.get(
                    str(transform.options.get("variable", "")),
                    "",
                )
            continue
        if transform.kind == "empty_is_null":
            if _is_empty(current):
                current = None
            continue
        if transform.kind == "empty_is_omitted":
            if _is_empty(current):
                current = OMIT
            continue
        if transform.kind == "seed_when_empty":
            # Real schema-aware seeding happens one layer above, where the caller
            # has access to the actual parameter/payload/filter-field metadata.
            continue
        if current is OMIT:
            continue
        if transform.kind == "to_integer":
            if isinstance(current, int) and not isinstance(current, bool):
                continue
            try:
                current = int(str(current))
            except (TypeError, ValueError):
                return None, f"'{raw_value}' is not a valid integer."
            continue
        if transform.kind == "to_number":
            if isinstance(current, (int, float)) and not isinstance(current, bool):
                current = float(current)
                continue
            try:
                current = float(str(current))
            except (TypeError, ValueError):
                return None, f"'{raw_value}' is not a valid number."
            continue
        if transform.kind == "to_boolean":
            if isinstance(current, bool):
                continue
            lowered = str(current).strip().lower()
            if lowered in {"true", "1", "yes"}:
                current = True
                continue
            if lowered in {"false", "0", "no"}:
                current = False
                continue
            return None, f"'{raw_value}' is not a valid boolean."
        if transform.kind == "date_format":
            format_text = str(transform.options.get("format", ""))
            try:
                current = datetime.strptime(str(current), format_text).isoformat()
            except (TypeError, ValueError):
                return None, f"'{raw_value}' does not match date format {format_text}."
            continue
        if transform.kind == "enum_validate":
            if allowed_values and str(current) not in allowed_values:
                return (
                    None,
                    "Value must be one of: " + _format_allowed_values(allowed_values) + ".",
                )
            continue
        if transform.kind == "json_parse":
            try:
                current = json.loads(str(current))
            except json.JSONDecodeError as exc:
                return None, f"Invalid JSON: {exc.msg}."
            continue
        if transform.kind == "split_list":
            delimiter = str(transform.options.get("delimiter", ","))
            current = [piece.strip() for piece in str(current).split(delimiter)]
            continue
        if transform.kind == "prefix_suffix":
            current = (
                str(transform.options.get("prefix", ""))
                + str(current)
                + str(transform.options.get("suffix", ""))
            )
            continue
    return current, None


@dataclass(frozen=True)
class ColumnMapping:
    column: str
    target_key: str
    transforms: tuple[Transform, ...] = ()


@dataclass
class RowValidationIssue:
    column: str
    target_key: str
    message: str
    severity: str


@dataclass
class ResolvedRow:
    row_number: int
    path_values: dict[str, str]
    query_values: dict[str, str]
    header_values: dict[str, str]
    form_values: dict[str, str]
    payload: Any
    expected_status: str
    correlation_key: str
    skip: bool
    issues: list[RowValidationIssue]

    @property
    def is_valid(self) -> bool:
        return not self.skip and not any(issue.severity == "error" for issue in self.issues)


def _looks_truthy(value: Any, truthy_values: set[str] | None = None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value is OMIT:
        return False
    return str(value).strip().lower() in (truthy_values or _TRUTHY)


def _parse_filter_target_key(target_key: str) -> tuple[str, str]:
    remainder = target_key[len("filter:") :]
    field_name, separator, operator = remainder.rpartition(":")
    if separator:
        return field_name, operator or "eq"
    return remainder, "eq"


def _path_get(container: Any, dotted_path: str) -> tuple[bool, Any]:
    current = container
    if dotted_path == "":
        return current is not None, current
    for segment in dotted_path.split("."):
        if not isinstance(current, dict) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _set_payload_value(payload: dict[str, Any], dotted_path: str, value: Any) -> None:
    segments = [segment for segment in dotted_path.split(".") if segment]
    current = payload
    for segment in segments[:-1]:
        child = current.get(segment)
        if not isinstance(child, dict):
            child = {}
            current[segment] = child
        current = child
    if segments:
        current[segments[-1]] = value


@dataclass(frozen=True)
class _ResolvedMapping:
    mapping: ColumnMapping
    target: MappingTarget
    filter_operator: str | None = None


_TEMPLATE_SOURCES = ("path", "query", "header", "form")


def template_request_values(values: dict[str, str] | None) -> dict[str, str]:
    """The sendable ``source:name`` values of a request template.

    API Explorer values carry ``enabled:source:name`` flags for optional
    parameters; flags are dropped, as are switched-off and blank values.
    """
    values = values or {}
    result: dict[str, str] = {}
    for key, value in values.items():
        source, _, name = key.partition(":")
        if source not in _TEMPLATE_SOURCES or not name:
            continue
        if values.get(f"enabled:{key}") == "false" or value is None or str(value) == "":
            continue
        result[key] = str(value)
    return result


class RowMapper:
    def __init__(
        self,
        endpoint: Endpoint,
        catalog: Catalog,
        mappings: list[ColumnMapping],
        *,
        default_expected_status: str = "200-299",
        environment_variables: dict[str, str] | None = None,
        template_values: dict[str, str] | None = None,
        template_payload: Any = None,
    ) -> None:
        self.endpoint = endpoint
        self.catalog = catalog
        self.mappings = list(mappings)
        self.default_expected_status = default_expected_status
        self.environment_variables = dict(environment_variables or {})
        # Request-template layer (precedence 3): every row starts from these
        # and mapped CSV values override them.
        self.template_values = template_request_values(template_values)
        self.template_payload = copy.deepcopy(template_payload)
        self.targets = mapping_targets(endpoint, catalog)
        self.targets_by_key = {target.key: target for target in self.targets}
        self.filter_schema = catalog.endpoint_filter_schema(endpoint)
        self.filter_fields = (
            {field.name: field for field in self.filter_schema.fields}
            if self.filter_schema is not None
            else {}
        )
        self._resolved_mappings = [
            self._validate_mapping(mapping) for mapping in self.mappings
        ]
        self._mappings_by_target_key: dict[str, list[ColumnMapping]] = {}
        for resolved in self._resolved_mappings:
            self._mappings_by_target_key.setdefault(resolved.target.key, []).append(
                resolved.mapping
            )

    def _validate_mapping(self, mapping: ColumnMapping) -> _ResolvedMapping:
        if mapping.target_key in self.targets_by_key:
            return _ResolvedMapping(mapping=mapping, target=self.targets_by_key[mapping.target_key])
        if mapping.target_key.startswith("filter:"):
            field_name, operator = _parse_filter_target_key(mapping.target_key)
            field = self.filter_fields.get(field_name)
            if field is None:
                raise ValueError(f"Unknown mapping target: {mapping.target_key}")
            allowed = {item.value for item in field.operators}
            if operator not in allowed:
                raise ValueError(
                    f"Invalid filter operator '{operator}' for {field_name}: {sorted(allowed)}"
                )
            default_key = f"filter:{field_name}:eq"
            return _ResolvedMapping(
                mapping=mapping,
                target=self.targets_by_key[default_key],
                filter_operator=operator,
            )
        raise ValueError(f"Unknown mapping target: {mapping.target_key}")

    def _initial_payload(self) -> Any:
        if self.template_payload is not None:
            return copy.deepcopy(self.template_payload)
        schema = self.catalog.payload_schema(self.endpoint.payload_schema)
        if not schema:
            return None
        kind = str(schema.get("kind") or ("object" if schema.get("fields") else "")).lower()
        if kind == "array":
            return None
        if kind == "object" or schema.get("fields"):
            return {}
        return None

    def resolve(self, row_number: int, row: dict[str, str]) -> ResolvedRow:
        buckets: dict[str, dict[str, str]] = {source: {} for source in _TEMPLATE_SOURCES}
        for key, value in self.template_values.items():
            source, _, name = key.partition(":")
            buckets[source][name] = value
        resolved = ResolvedRow(
            row_number=row_number,
            path_values=buckets["path"],
            query_values=buckets["query"],
            header_values=buckets["header"],
            form_values=buckets["form"],
            payload=self._initial_payload(),
            expected_status=self.default_expected_status,
            correlation_key=f"row-{row_number}",
            skip=False,
            issues=[],
        )
        filter_conditions: list[dict[str, Any]] = []
        sort_entries: list[dict[str, Any]] = []
        issue_targets: set[str] = set()

        for entry in self._resolved_mappings:
            mapping = entry.mapping
            target = entry.target
            raw_value = row.get(mapping.column, "")
            if raw_value is None:
                raw_value = ""
            wants_enum_validation = (
                target.allowed_values
                and any(transform.kind == "enum_validate" for transform in mapping.transforms)
            )
            value, error = apply_transforms(
                str(raw_value),
                list(mapping.transforms),
                environment_variables=self.environment_variables,
                allowed_values=target.allowed_values if wants_enum_validation else (),
            )
            if error:
                resolved.issues.append(
                    RowValidationIssue(
                        column=mapping.column,
                        target_key=mapping.target_key,
                        message=error,
                        severity="error",
                    )
                )
                issue_targets.add(target.key)
                continue

            if target.group in {"path", "query", "header", "form"}:
                if value is OMIT or value is None:
                    continue
                target_dict = {
                    "path": resolved.path_values,
                    "query": resolved.query_values,
                    "header": resolved.header_values,
                    "form": resolved.form_values,
                }[target.group]
                _, name = target.key.split(":", 1)
                target_dict[name] = value if isinstance(value, str) else str(value)
                continue

            if target.group == "filter":
                if _is_empty(value):
                    continue
                field_name, operator = _parse_filter_target_key(mapping.target_key)
                if isinstance(value, list):
                    filter_value = ",".join(str(item) for item in value)
                else:
                    filter_value = str(value)
                filter_conditions.append(
                    {
                        "name": field_name,
                        "operator": operator,
                        "value": filter_value,
                        "and": True,
                    }
                )
                continue

            if target.group == "sort":
                if _is_empty(value):
                    continue
                _, field_name = target.key.split(":", 1)
                sort_entries.append(
                    {
                        "name": field_name,
                        "descending": _looks_truthy(value, _SORT_DESCENDING),
                    }
                )
                continue

            if target.group == "payload":
                if value is OMIT:
                    continue
                path = target.key[len("payload:") :]
                if path == "":
                    # Top-level array/json payloads are edited as one JSON blob.
                    resolved.payload = value
                    continue
                if resolved.payload is None or not isinstance(resolved.payload, dict):
                    resolved.payload = {}
                _set_payload_value(resolved.payload, path, value)
                continue

            if target.key == "expected_status":
                if not _is_empty(value):
                    resolved.expected_status = str(value)
                continue

            if target.key == "correlation_key":
                if not _is_empty(value):
                    resolved.correlation_key = str(value)
                continue

            if target.key == "skip_row":
                if _looks_truthy(value):
                    resolved.skip = True

        if filter_conditions:
            resolved.query_values["Filter"] = encode_filter(filter_conditions)
        if sort_entries:
            resolved.query_values["Sort"] = encode_sort(sort_entries)

        for target in self.targets:
            if not target.required or target.group not in {"path", "query", "header", "form"}:
                continue
            mappings = self._mappings_by_target_key.get(target.key, [])
            _, name = target.key.split(":", 1)
            bucket = {
                "path": resolved.path_values,
                "query": resolved.query_values,
                "header": resolved.header_values,
                "form": resolved.form_values,
            }[target.group]
            if not mappings:
                # A request-template value satisfies an unmapped requirement.
                if bucket.get(name, "") != "":
                    continue
                resolved.issues.append(
                    RowValidationIssue(
                        column="",
                        target_key=target.key,
                        message="Required value is missing.",
                        severity="error",
                    )
                )
                continue
            if bucket.get(name, "") != "":
                continue
            if target.key in issue_targets:
                continue
            resolved.issues.append(
                RowValidationIssue(
                    column=mappings[0].column,
                    target_key=target.key,
                    message="Required value is missing.",
                    severity="error",
                )
            )

        for target in self.targets:
            if not target.required or target.group != "payload":
                continue
            mappings = self._mappings_by_target_key.get(target.key, [])
            if target.key in issue_targets:
                continue
            present, _ = _path_get(resolved.payload, target.key[len("payload:") :])
            if not mappings:
                if present:
                    continue
                resolved.issues.append(
                    RowValidationIssue(
                        column="",
                        target_key=target.key,
                        message="Required value is missing.",
                        severity="error",
                    )
                )
                continue
            if present:
                continue
            resolved.issues.append(
                RowValidationIssue(
                    column=mappings[0].column,
                    target_key=target.key,
                    message="Required value is missing.",
                    severity="error",
                )
            )

        return resolved

    def build_request_template(self, base: RequestTemplate, resolved: ResolvedRow) -> RequestTemplate:
        values: dict[str, str] = {}
        for source, bucket in (
            ("path", resolved.path_values),
            ("query", resolved.query_values),
            ("header", resolved.header_values),
            ("form", resolved.form_values),
        ):
            for name, value in bucket.items():
                values[f"{source}:{name}"] = value
        template = base.with_values(values, payload=resolved.payload if resolved.payload else None)
        return replace(template, expected_status=resolved.expected_status)
