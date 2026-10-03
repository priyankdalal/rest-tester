"""Checks a request plan against the catalog before the user ever sees it.

Harmless slips (wrong letter case, a filter written as a raw string, a body on
a GET) are fixed deterministically and recorded as assumptions. Everything else
becomes a :class:`PlanIssue` whose message lists the valid options, which is
what lets the repair round succeed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..schema import (
    MULTI_VALUE_OPERATORS,
    RANGE_OPERATORS,
    FilterSchema,
    decode_filter,
    decode_sort,
)
from .catalog_index import FILTER_PARAMETER, SORT_PARAMETER, CatalogIndex
from .request_plan import READ_METHODS, FilterSpec, RequestPlan, SortSpec

_STATUS = re.compile(r"^\d{3}(-\d{3})?(,\s*\d{3})*$")
#: ``{{name}}`` suite placeholders; resolved at run time, so they skip type and LOV checks.
VARIABLE = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


def has_variable(value: Any) -> bool:
    return isinstance(value, str) and VARIABLE.search(value) is not None

#: Spellings models commonly use for the platform operators.
OPERATOR_ALIASES: dict[str, str] = {
    "=": "eq", "==": "eq", "equals": "eq", "equal": "eq", "is": "eq",
    "!=": "neq", "<>": "neq", "ne": "neq", "not_equals": "neq", "notequals": "neq",
    ">": "gt", "<": "lt", ">=": "gte", "<=": "lte", "ge": "gte", "le": "lte",
    "contains": "ct", "like": "ct", "not_contains": "nct", "notcontains": "nct",
    "startswith": "sw", "starts_with": "sw", "endswith": "ew", "ends_with": "ew",
    "between": "bt", "range": "bt", "not_in": "nin", "notin": "nin",
}


@dataclass(frozen=True)
class PlanIssue:
    path: str
    code: str
    message: str

    def as_text(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass(frozen=True)
class ValidationResult:
    plan: RequestPlan
    issues: tuple[PlanIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


def _options(values: Any, limit: int = 30) -> str:
    items = [str(item) for item in values]
    text = ", ".join(items[:limit])
    return text + (f" (+{len(items) - limit} more)" if len(items) > limit else "")


def _canonical(value: str, allowed: Any) -> str | None:
    lowered = value.lower()
    return next((str(item) for item in allowed if str(item).lower() == lowered), None)


def validate_request_plan(plan: RequestPlan, index: CatalogIndex) -> ValidationResult:
    if plan.status != "plan":
        if not plan.question and plan.status == "clarify":
            return ValidationResult(
                plan, (PlanIssue("question", "missing_question", "status is clarify but question is empty"),)
            )
        return ValidationResult(plan, ())
    endpoint = index.get(plan.endpoint_id)
    if endpoint is None:
        return ValidationResult(
            plan,
            (
                PlanIssue(
                    "endpoint_id",
                    "unknown_endpoint",
                    f"'{plan.endpoint_id}' is not a catalog endpoint; choose one of the candidate ids",
                ),
            ),
        )

    issues: list[PlanIssue] = []
    assumptions = list(plan.assumptions)
    warnings = list(plan.warnings)
    schema = index.filter_schema(endpoint)
    filters = list(plan.filters)
    sort = list(plan.sort)

    # -- parameters ----------------------------------------------------------
    by_name = {item.name.lower(): item for item in endpoint.parameters}
    parameters: list[tuple[str, str]] = []
    for position, (name, value) in enumerate(plan.parameters):
        parameter = by_name.get(name.lower())
        if parameter is None:
            issues.append(
                PlanIssue(
                    f"parameters[{position}].name",
                    "unknown_parameter",
                    f"endpoint has no parameter '{name}'. Valid: {_options(item.name for item in endpoint.parameters) or 'none'}",
                )
            )
            continue
        if schema is not None and parameter.source == "query" and parameter.name == FILTER_PARAMETER:
            decoded = decode_filter(value)
            if decoded:
                filters.extend(
                    FilterSpec(item["name"], item["operator"], tuple(_split(item["value"], item["operator"])))
                    for item in decoded
                )
                assumptions.append("Moved the raw Filter text into structured filters.")
            elif value:
                issues.append(
                    PlanIssue(
                        f"parameters[{position}]",
                        "use_filters",
                        "do not set Filter as a parameter; describe conditions in 'filters' instead",
                    )
                )
            continue
        if schema is not None and parameter.source == "query" and parameter.name == SORT_PARAMETER:
            sort.extend(SortSpec(item["name"], item["descending"]) for item in decode_sort(value))
            continue
        if parameter.values and value and not has_variable(value):
            canonical = _canonical(value, parameter.values)
            if canonical is None:
                issues.append(
                    PlanIssue(
                        f"parameters[{position}].value",
                        "invalid_value",
                        f"'{value}' is not allowed for {parameter.name}. Valid: {_options(parameter.values)}",
                    )
                )
                continue
            value = canonical
        if value and not has_variable(value):
            typed = _coerce_scalar(value, parameter.type)
            if typed is None:
                issues.append(
                    PlanIssue(
                        f"parameters[{position}].value",
                        "invalid_type",
                        f"'{value}' is not a valid {parameter.type} for {parameter.name}",
                    )
                )
                continue
            if typed != value:
                assumptions.append(f"Read {parameter.name} '{value}' as {typed}.")
                value = typed
        parameters.append((parameter.name, value))
    given = {name for name, value in parameters if value}
    for parameter in endpoint.parameters:
        if parameter.source == "path" and parameter.name not in given:
            warnings.append(f"Fill in path parameter '{parameter.name}' before sending.")

    # -- filters and sort ----------------------------------------------------
    clean_filters = _validate_filters(filters, schema, issues)
    clean_sort = _validate_sort(sort, schema, issues)

    # -- payload -------------------------------------------------------------
    payload = plan.payload
    method = endpoint.method.upper()
    if method in READ_METHODS and payload is not None:
        payload = None
        assumptions.append(f"Dropped the request body: {method} requests do not send one.")
    elif payload is not None:
        payload = _validate_payload(payload, index.catalog.payload_schema(endpoint.payload_schema), issues, warnings)
        if (endpoint.payload_type or "").startswith("JsonPatchDocument"):
            payload = _validate_json_patch(payload, issues, assumptions)
    if method not in READ_METHODS:
        warning = f"This {method} request changes data on the server."
        if warning not in warnings:
            warnings.append(warning)

    expected = plan.expected_status
    if expected and not _STATUS.match(expected):
        assumptions.append(f"Replaced expected status '{expected}' with {endpoint.expected_status}.")
        expected = endpoint.expected_status

    fixed = plan.with_changes(
        parameters=tuple(parameters),
        filters=tuple(clean_filters),
        sort=tuple(clean_sort),
        payload=payload,
        expected_status=expected,
        assumptions=tuple(dict.fromkeys(assumptions)),
        warnings=tuple(dict.fromkeys(warnings)),
    )
    return ValidationResult(fixed, tuple(issues))


_INTEGER_TYPES = ("int", "long", "short", "byte")
_NUMBER_TYPES = ("decimal", "double", "float")


def _coerce_scalar(value: str, type_name: str) -> str | None:
    """Normalise a scalar parameter value for its C# type; ``None`` if it cannot be read."""

    base = (type_name or "").strip().rstrip("?").lower()
    text = str(value).strip()
    if base.startswith(("list<", "ilist<", "ienumerable<")) or base.endswith("[]"):
        inner = base.split("<", 1)[-1].rstrip(">").rstrip("[]").rstrip("?")
        parts = [_coerce_scalar(part, inner) for part in text.split(",") if part.strip()]
        return None if any(part is None for part in parts) else ",".join(parts)  # type: ignore[arg-type]
    if base in _INTEGER_TYPES:
        if re.fullmatch(r"[+-]?\d+", text):
            return str(int(text))
        found = re.findall(r"\d+", text)
        return found[0] if len(found) == 1 else None
    if base in _NUMBER_TYPES:
        try:
            float(text)
            return text
        except ValueError:
            found = re.findall(r"[+-]?\d+(?:\.\d+)?", text)
            return found[0] if len(found) == 1 else None
    if base == "bool":
        lowered = text.lower()
        if lowered in {"true", "yes", "1", "on"}:
            return "true"
        if lowered in {"false", "no", "0", "off"}:
            return "false"
        return None
    return text


def _split(value: str, operator: str) -> list[str]:
    if operator in MULTI_VALUE_OPERATORS or operator in RANGE_OPERATORS:
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value]


def _validate_filters(
    filters: list[FilterSpec], schema: FilterSchema | None, issues: list[PlanIssue]
) -> list[FilterSpec]:
    if not filters:
        return []
    if schema is None:
        issues.append(
            PlanIssue("filters", "filters_not_supported", "this endpoint does not support filters; return []")
        )
        return []
    clean: list[FilterSpec] = []
    for position, item in enumerate(filters):
        path = f"filters[{position}]"
        field = schema.field(item.field)
        if field is None:
            issues.append(
                PlanIssue(
                    f"{path}.field",
                    "unknown_filter_field",
                    f"'{item.field}' is not filterable. Valid: {_options(entry.name for entry in schema.fields)}",
                )
            )
            continue
        allowed = [entry.value for entry in field.operators]
        op = OPERATOR_ALIASES.get(item.op, item.op)
        if op == "notin" and "notin" not in allowed:
            op = "nin"
        if op not in allowed:
            issues.append(
                PlanIssue(
                    f"{path}.op",
                    "invalid_operator",
                    f"operator '{item.op}' is not allowed for {field.name}. Valid: {_options(allowed)}",
                )
            )
            continue
        values = [value for value in item.values if value != ""]
        if op in RANGE_OPERATORS and len(values) != 2:
            issues.append(PlanIssue(f"{path}.values", "range_needs_two", f"'{op}' needs exactly two values [low, high]"))
            continue
        if op in MULTI_VALUE_OPERATORS and not values:
            issues.append(PlanIssue(f"{path}.values", "missing_value", f"'{op}' needs at least one value"))
            continue
        if op not in RANGE_OPERATORS and op not in MULTI_VALUE_OPERATORS:
            if len(values) > 1:
                issues.append(
                    PlanIssue(f"{path}.values", "single_value", f"'{op}' takes one value; use 'in' for several")
                )
                continue
            if not values and not field.nullable:
                issues.append(PlanIssue(f"{path}.values", "missing_value", f"'{op}' on {field.name} needs a value"))
                continue
        if field.lov:
            canonical_values = [value if has_variable(value) else _canonical(value, field.lov) for value in values]
            invalid = [value for value, canonical in zip(values, canonical_values) if canonical is None]
            if invalid:
                issues.append(
                    PlanIssue(
                        f"{path}.values",
                        "invalid_value",
                        f"'{invalid[0]}' is not a valid {field.name}. Valid: {_options(field.lov)}",
                    )
                )
                continue
            values = [str(value) for value in canonical_values]
        clean.append(FilterSpec(field.name, op, tuple(values)))
    return clean


def _validate_sort(sort: list[SortSpec], schema: FilterSchema | None, issues: list[PlanIssue]) -> list[SortSpec]:
    if not sort:
        return []
    if schema is None:
        issues.append(PlanIssue("sort", "sort_not_supported", "this endpoint does not support sorting; return []"))
        return []
    sortable = schema.sortable_fields
    clean: list[SortSpec] = []
    for position, item in enumerate(sort):
        field = next((entry for entry in sortable if entry.name.lower() == item.field.lower()), None)
        if field is None:
            issues.append(
                PlanIssue(
                    f"sort[{position}].field",
                    "unknown_sort_field",
                    f"'{item.field}' is not sortable. Valid: {_options(entry.name for entry in sortable)}",
                )
            )
            continue
        clean.append(SortSpec(field.name, item.descending))
    return clean


PATCH_OPERATIONS = ("add", "remove", "replace", "move", "copy", "test")


def _validate_json_patch(payload: Any, issues: list[PlanIssue], assumptions: list[str]) -> Any:
    if isinstance(payload, dict) and payload and "op" not in {str(key).lower() for key in payload}:
        assumptions.append("Rewrote the PATCH body as RFC 6902 'replace' operations.")
        return [{"op": "replace", "path": f"/{key}", "value": value} for key, value in payload.items()]
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list) or not payload:
        issues.append(
            PlanIssue(
                "payload",
                "invalid_patch",
                'a PATCH body is an RFC 6902 array such as [{"op": "replace", "path": "/Name", "value": "x"}]',
            )
        )
        return payload
    clean: list[Any] = []
    for position, item in enumerate(payload):
        if not isinstance(item, dict):
            issues.append(PlanIssue(f"payload[{position}]", "invalid_patch", "each operation must be an object"))
            continue
        lowered = {str(key).lower(): value for key, value in item.items()}
        op = str(lowered.get("op", "")).lower()
        path = str(lowered.get("path", "")).strip()
        if op not in PATCH_OPERATIONS:
            issues.append(
                PlanIssue(f"payload[{position}].op", "invalid_patch", f"'{op}' is not a JSON Patch op. Valid: {_options(PATCH_OPERATIONS)}")
            )
            continue
        if not path:
            issues.append(PlanIssue(f"payload[{position}].path", "invalid_patch", "each operation needs a path such as /Name"))
            continue
        operation: dict[str, Any] = {"op": op, "path": path if path.startswith("/") else "/" + path}
        if "value" in lowered and op not in {"remove", "move", "copy"}:
            operation["value"] = lowered["value"]
        if "from" in lowered:
            operation["from"] = lowered["from"]
        clean.append(operation)
    return clean


def _validate_payload(
    payload: Any, schema: dict[str, Any] | None, issues: list[PlanIssue], warnings: list[str]
) -> Any:
    if schema is None or not isinstance(payload, dict) or schema.get("kind") == "array":
        return payload
    fields = {str(item.get("name", "")).lower(): item for item in schema.get("fields", ())}
    if not fields:
        return payload
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        field = fields.get(str(key).lower())
        if field is None:
            issues.append(
                PlanIssue(
                    f"payload.{key}",
                    "unknown_payload_field",
                    f"payload has no field '{key}'. Valid: {_options(item.get('name') for item in fields.values())}",
                )
            )
            continue
        name = str(field.get("name"))
        lov = field.get("lov") or []
        if lov and isinstance(value, str) and not has_variable(value):
            canonical = _canonical(value, lov)
            if canonical is None:
                issues.append(
                    PlanIssue(f"payload.{name}", "invalid_value", f"'{value}' is not allowed. Valid: {_options(lov)}")
                )
                continue
            value = canonical
        clean[name] = value
    missing = [str(item.get("name")) for item in fields.values() if item.get("required") and str(item.get("name")) not in clean]
    if missing:
        warnings.append("Required payload fields left for you to fill: " + ", ".join(missing))
    return clean
