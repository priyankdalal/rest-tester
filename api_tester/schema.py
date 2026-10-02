"""Typed access to the Rest Tester filter, sort, and payload schemas."""

from __future__ import annotations

import random
import string
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Kept in sync with tools/schema_extractor.py, which derives them from
# TrialManagement.Commons.
FILTER_SEPARATOR = ":="
FILTER_AND = ";"
FILTER_OR = "|"
VALUE_DELIMITER = ","
SORT_DESCENDING_SUFFIX = "-"
NULLABLE_OPERATORS = ("eq", "neq")
MULTI_VALUE_OPERATORS = ("in", "nin", "notin")
RANGE_OPERATORS = ("bt",)


@dataclass(frozen=True)
class Operator:
    label: str
    value: str


@dataclass(frozen=True)
class FilterField:
    name: str
    display_name: str
    data_type: str
    clr_type: str
    nullable: bool
    sortable: bool
    lov: tuple[str, ...]
    operators: tuple[Operator, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FilterField":
        return cls(
            name=value["name"],
            display_name=value.get("display_name") or value["name"],
            data_type=value["data_type"],
            clr_type=value.get("clr_type", ""),
            nullable=value.get("nullable", False),
            sortable=value.get("sortable", True),
            lov=tuple(value.get("lov", ())),
            operators=tuple(Operator(**item) for item in value.get("operators", ())),
        )

    @property
    def label(self) -> str:
        suffix = f" ({self.data_type})"
        if self.display_name.lower() != self.name.lower():
            return f"{self.name} — {self.display_name}{suffix}"
        return f"{self.name}{suffix}"


@dataclass(frozen=True)
class FilterSchema:
    name: str
    fields: tuple[FilterField, ...]

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FilterSchema":
        return cls(
            name=value["name"],
            fields=tuple(FilterField.from_dict(item) for item in value["fields"]),
        )

    def field(self, name: str) -> FilterField | None:
        lowered = name.lower()
        return next((item for item in self.fields if item.name.lower() == lowered), None)

    @property
    def sortable_fields(self) -> tuple[FilterField, ...]:
        return tuple(item for item in self.fields if item.sortable)


def seed_filter_value(field: FilterField, operator: str) -> str:
    """Produces a realistic value for a field/operator pair."""
    if operator in RANGE_OPERATORS:
        low, high = _range_values(field)
        return f"{low}{VALUE_DELIMITER}{high}"
    count = 2 if operator in MULTI_VALUE_OPERATORS else 1
    values = [_single_value(field, operator, index) for index in range(count)]
    return VALUE_DELIMITER.join(values)


def _single_value(field: FilterField, operator: str, index: int = 0) -> str:
    if field.lov:
        return field.lov[index % len(field.lov)]
    data_type = field.data_type
    if data_type == "Number":
        return str(index + 1)
    if data_type == "Flag":
        return "true" if index % 2 == 0 else "false"
    if data_type == "Date":
        moment = datetime.now(UTC) - timedelta(days=30 * (index + 1))
        return moment.strftime("%Y-%m-%d")
    if "guid" in field.clr_type.lower():
        return str(uuid.uuid4())
    name = field.name.split(".")[-1]
    if operator in {"ct", "nct", "sw", "ew"}:
        # Partial-match operators need a fragment, not a whole value.
        return name[:3].lower()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{name}-{suffix}"


def _range_values(field: FilterField) -> tuple[str, str]:
    if field.data_type == "Date":
        now = datetime.now(UTC)
        return (
            (now - timedelta(days=90)).strftime("%Y-%m-%d"),
            now.strftime("%Y-%m-%d"),
        )
    return "1", "100"


def encode_filter(conditions: list[dict[str, Any]]) -> str:
    """Encodes conditions exactly like FilterService.EncodeFilters."""
    parts: list[str] = []
    for index, condition in enumerate(conditions):
        text = f"{condition['name']}__{condition['operator']}{FILTER_SEPARATOR}{condition.get('value', '')}"
        if index:
            text = (FILTER_AND if condition.get("and", True) else FILTER_OR) + text
        parts.append(text)
    return "".join(parts)


def decode_filter(filter_string: str) -> list[dict[str, Any]]:
    """Parses a filter string back into conditions."""
    conditions: list[dict[str, Any]] = []
    text = (filter_string or "").strip()
    if not text:
        return conditions
    tokens: list[str] = []
    delimiters: list[str] = []
    current = ""
    for char in text:
        if char in (FILTER_AND, FILTER_OR):
            tokens.append(current)
            delimiters.append(char)
            current = ""
        else:
            current += char
    tokens.append(current)
    for index, token in enumerate(tokens):
        if not token.strip():
            continue
        name_op, separator, value = token.partition(FILTER_SEPARATOR)
        if "__" not in name_op:
            continue
        name, _, operator = name_op.rpartition("__")
        conditions.append(
            {
                "name": name.strip(),
                "operator": operator.strip().lower(),
                "value": value if separator else "",
                "and": index == 0 or delimiters[index - 1] == FILTER_AND,
            }
        )
    return conditions


def encode_sort(entries: list[dict[str, Any]]) -> str:
    return VALUE_DELIMITER.join(
        f"{entry['name']}{SORT_DESCENDING_SUFFIX if entry.get('descending') else ''}"
        for entry in entries
        if entry.get("name")
    )


def decode_sort(sort_string: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for token in (sort_string or "").split(VALUE_DELIMITER):
        token = token.strip()
        if not token:
            continue
        descending = token.endswith(SORT_DESCENDING_SUFFIX)
        entries.append({"name": token[:-1] if descending else token, "descending": descending})
    return entries


def seed_payload_field(field: dict[str, Any], index: int = 0) -> Any:
    """Generates a value for one payload schema field."""
    kind = field.get("kind", "text")
    name = field.get("name", "value")
    # A documented value list wins over a generated sample whatever the kind:
    # an invented value outside the list only ever earns a 400.
    lov = field.get("lov") or []
    if lov and kind not in {"array", "json", "object"}:
        value = lov[index % len(lov)]
        if kind == "integer":
            try:
                return int(value)
            except (TypeError, ValueError):
                return value
        if kind == "number":
            try:
                return float(value)
            except (TypeError, ValueError):
                return value
        return value
    if kind == "enum":
        return None
    if kind == "boolean":
        return False
    if kind == "integer":
        return index + 1
    if kind == "number":
        return float(index + 1)
    if kind == "date":
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind == "guid":
        return str(uuid.uuid4())
    if kind == "json":
        return {}
    if kind == "object":
        return seed_payload(field)
    if kind == "array":
        item = field.get("item")
        return [seed_payload_field(item)] if item else []
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{name}-{suffix}"


def seed_payload(schema: dict[str, Any]) -> Any:
    """Generates a full payload object from a schema."""
    if schema.get("kind") == "array":
        item = schema.get("item")
        return [seed_payload_field(item)] if item else []
    return {
        field["name"]: seed_payload_field(field)
        for field in schema.get("fields", [])
    }
