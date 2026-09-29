"""Extracts Rest Tester-specific filter, sort, and payload schemas from C# sources.

The rules implemented here mirror the platform's own filter engine in
``TrialManagement.Packages/source/TrialManagement.Commons``:

* ``FilterSortService.FilterService.EncodeFilters`` defines the wire grammar
  ``Name__op:=value[,value]`` joined by ``;`` (AND) or ``|`` (OR).
* ``FilterSortExtensions.GetDataType`` maps CLR type names onto the five
  attribute types (``Text``/``Number``/``Date``/``Flag``/``LOV``).
* ``FilterSortService.FilterService.SupportedOperations`` defines which
  operators are valid for each attribute type.
* ``FilterSortExtensions.IsFilterableType`` decides which properties can be
  filtered/sorted at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Mirrors TrialManagement.Commons.Constants.FILTER_EQUALS_SEPERATOR and the
# LogicalOperators/MultiValueOperator defaults on ResourceResponse.
FILTER_SEPARATOR = ":="
FILTER_AND = ";"
FILTER_OR = "|"
FILTER_VALUE_DELIMITER = ","
SORT_DESCENDING_SUFFIX = "-"

ATTRIBUTE_TYPE_TEXT = "Text"
ATTRIBUTE_TYPE_NUMBER = "Number"
ATTRIBUTE_TYPE_DATE = "Date"
ATTRIBUTE_TYPE_FLAG = "Flag"
ATTRIBUTE_TYPE_LOV = "LOV"

# FilterSortService.FilterService.SupportedOperations
SUPPORTED_OPERATIONS: dict[str, list[dict[str, str]]] = {
    ATTRIBUTE_TYPE_TEXT: [
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
        {"label": "Contains", "value": "ct"},
        {"label": "Does Not Contain", "value": "nct"},
        {"label": "Starts with", "value": "sw"},
        {"label": "Ends with", "value": "ew"},
        {"label": "In", "value": "in"},
        {"label": "Not in", "value": "nin"},
    ],
    ATTRIBUTE_TYPE_NUMBER: [
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
        {"label": "Greater than", "value": "gt"},
        {"label": "Less than", "value": "lt"},
        {"label": "Greater than or equals to", "value": "gte"},
        {"label": "Less than or equals to", "value": "lte"},
        {"label": "In", "value": "in"},
        {"label": "Not in", "value": "nin"},
        {"label": "Between", "value": "bt"},
    ],
    ATTRIBUTE_TYPE_DATE: [
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
        {"label": "Greater than", "value": "gt"},
        {"label": "Less than", "value": "lt"},
        {"label": "Greater than or equals to", "value": "gte"},
        {"label": "Less than or equals to", "value": "lte"},
        {"label": "Between", "value": "bt"},
    ],
    ATTRIBUTE_TYPE_FLAG: [
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
    ],
    ATTRIBUTE_TYPE_LOV: [
        {"label": "Equals to", "value": "eq"},
        {"label": "Not equals to", "value": "neq"},
    ],
}

# FilterSortService.FilterService.NullableOperators — the only operators that
# may be sent with an empty value to perform a null/empty check.
NULLABLE_OPERATORS = ("eq", "neq")
MULTI_VALUE_OPERATORS = ("in", "nin", "notin")
RANGE_OPERATORS = ("bt",)

_NUMBER_TYPES = {
    "int", "int16", "int32", "int64", "long", "short", "byte", "sbyte",
    "uint", "ulong", "ushort", "single", "float", "double", "decimal",
}
_TEXT_TYPES = {"string", "char", "json", "guid"}
_FLAG_TYPES = {"bool", "boolean"}
_DATE_TYPES = {"datetime", "datetimeoffset", "dateonly"}

_COLLECTION_PATTERN = re.compile(
    r"^(?:List|IList|ICollection|IEnumerable|HashSet|Collection)<(?P<inner>.+)>$"
)

PROPERTY_PATTERN = re.compile(
    r"(?P<attrs>(?:^[ \t]*\[[^\]]*\][ \t]*\r?\n)*)"
    r"[ \t]*public\s+(?P<modifiers>(?:virtual\s+|required\s+|override\s+|new\s+|static\s+)*)"
    r"(?P<type>[\w.<>,?\[\]\s]+?)\s+(?P<name>\w+)\s*\{\s*get\s*;"
    r"(?:\s*(?:(?:private|protected|internal)\s+)?(?:set|init)\s*;)?",
    re.MULTILINE,
)
ENUM_PATTERN = re.compile(
    r"\benum\s+(?P<name>\w+)(?:\s*:\s*\w+)?\s*\{(?P<body>[^}]*)\}",
    re.MULTILINE,
)
TYPE_PATTERN = re.compile(
    r"(?P<attrs>(?:^[ \t]*\[[^\]]*\][ \t]*\r?\n)*)"
    r"[ \t]*(?:public|internal)\s+(?:abstract\s+|sealed\s+|partial\s+|static\s+)*"
    r"(?:class|record|interface)\s+(?P<name>\w+)(?:<(?P<generic>[^>{]+)>)?\s*"
    r"(?:\:\s*(?P<bases>[^{]+?))?\s*(?:where\s+[^{]+?)?\{",
    re.MULTILINE,
)


def strip_nullable(type_name: str) -> str:
    return type_name.strip().rstrip("?").strip()


def simple_name(type_name: str) -> str:
    cleaned = strip_nullable(type_name).replace("global::", "")
    if "<" in cleaned:
        cleaned = cleaned[: cleaned.index("<")]
    return cleaned.split(".")[-1]


def element_type(type_name: str) -> str | None:
    """Returns the element type for a collection, otherwise None."""
    cleaned = strip_nullable(type_name).replace("global::", "")
    if cleaned.endswith("[]"):
        return cleaned[:-2]
    match = _COLLECTION_PATTERN.match(cleaned)
    if match:
        return match.group("inner").strip()
    return None


def data_type_for(type_name: str, enums: dict[str, list[str]]) -> str:
    """Mirrors FilterSortExtensions.GetDataType / GetDataTypeName."""
    inner = element_type(type_name)
    if inner is not None:
        return data_type_for(inner, enums)
    name = simple_name(type_name)
    lowered = name.lower()
    if name in enums:
        return ATTRIBUTE_TYPE_LOV
    if lowered in _NUMBER_TYPES:
        return ATTRIBUTE_TYPE_NUMBER
    if lowered in _FLAG_TYPES:
        return ATTRIBUTE_TYPE_FLAG
    if lowered in _DATE_TYPES:
        return ATTRIBUTE_TYPE_DATE
    if lowered in _TEXT_TYPES:
        return ATTRIBUTE_TYPE_TEXT
    return name


def is_filterable_type(type_name: str, enums: dict[str, list[str]]) -> bool:
    """Mirrors FilterSortExtensions.IsFilterableType."""
    return data_type_for(type_name, enums) in SUPPORTED_OPERATIONS


def parse_attributes(block: str) -> dict[str, str]:
    """Returns attribute name -> raw argument text for a property/class block."""
    attributes: dict[str, str] = {}
    for match in re.finditer(r"\[([^\]\[]+)\]", block or ""):
        for entry in _split_attribute_entries(match.group(1)):
            name_match = re.match(r"\s*(\w+)\s*(?:\((?P<args>.*)\))?\s*$", entry, re.DOTALL)
            if name_match:
                attributes[name_match.group(1)] = (name_match.group("args") or "").strip()
    return attributes


def _split_attribute_entries(text: str) -> list[str]:
    entries: list[str] = []
    depth = 0
    current = ""
    in_string = False
    escaped = False
    for char in text:
        if char == '"' and not escaped:
            in_string = not in_string
        escaped = char == "\\" and not escaped
        if not in_string:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                entries.append(current)
                current = ""
                continue
        current += char
    if current.strip():
        entries.append(current)
    return entries


def attribute_string_argument(arguments: str, key: str | None = None) -> str | None:
    """Reads ``Name = "x"`` when key is given, else the first positional string."""
    if key:
        match = re.search(rf"\b{re.escape(key)}\s*=\s*\"((?:\\.|[^\"\\])*)\"", arguments)
        return match.group(1) if match else None
    match = re.match(r"\s*\"((?:\\.|[^\"\\])*)\"", arguments)
    return match.group(1) if match else None


@dataclass
class Property:
    name: str
    type: str
    attributes: dict[str, str] = field(default_factory=dict)
    required_modifier: bool = False

    @property
    def nullable(self) -> bool:
        return self.type.strip().endswith("?")

    @property
    def required(self) -> bool:
        return self.required_modifier or "Required" in self.attributes


@dataclass
class TypeInfo:
    name: str
    bases: list[str] = field(default_factory=list)
    properties: list[Property] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    source_file: str = ""
    type_parameters: list[str] = field(default_factory=list)


def parse_source_types(repository: Path) -> tuple[dict[str, TypeInfo], dict[str, list[str]]]:
    """Parses every class/record and enum in a repository."""
    types: dict[str, TypeInfo] = {}
    enums: dict[str, list[str]] = {}
    for source_file in repository.rglob("*.cs"):
        if any(part in {"bin", "obj", ".venv"} for part in source_file.parts):
            continue
        try:
            text = source_file.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError):
            continue
        text = _strip_comments(text)

        for match in ENUM_PATTERN.finditer(text):
            values = []
            for entry in match.group("body").split(","):
                entry = entry.strip()
                if not entry:
                    continue
                member = re.match(r"(\w+)", entry)
                if member:
                    values.append(member.group(1))
            if values:
                enums[match.group("name")] = values

        declarations = list(TYPE_PATTERN.finditer(text))
        for index, match in enumerate(declarations):
            end = declarations[index + 1].start() if index + 1 < len(declarations) else len(text)
            body = text[match.end() : end]
            properties = [
                Property(
                    name=property_match.group("name"),
                    type=re.sub(r"\s+", "", property_match.group("type")),
                    attributes=parse_attributes(property_match.group("attrs")),
                    required_modifier="required" in (property_match.group("modifiers") or ""),
                )
                for property_match in PROPERTY_PATTERN.finditer(body)
            ]
            bases = [
                re.sub(r"\s+", "", item)
                for item in _split_bases(match.group("bases") or "")
                if item.strip()
            ]
            name = match.group("name")
            type_parameters = [
                item.strip()
                for item in _split_top_level(match.group("generic") or "")
                if item.strip()
            ]
            if properties or name not in types:
                types[name] = TypeInfo(
                    name=name,
                    bases=bases,
                    properties=properties,
                    attributes=parse_attributes(match.group("attrs")),
                    source_file=str(source_file.relative_to(repository)).replace("\\", "/"),
                    type_parameters=type_parameters,
                )
    return types, enums


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"^[ \t]*//.*$", "", text, flags=re.MULTILINE)


def _split_bases(text: str) -> list[str]:
    text = re.sub(r"\s+where\s+.*", "", text, flags=re.DOTALL)
    return _split_top_level(text)


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    entries: list[str] = []
    depths = {"<": 0, "(": 0, "[": 0, "{": 0}
    closing = {">": "<", ")": "(", "]": "[", "}": "{"}
    current = ""
    for char in text:
        if char in depths:
            depths[char] += 1
        elif char in closing:
            depths[closing[char]] = max(0, depths[closing[char]] - 1)
        elif char == separator and not any(depths.values()):
            if current.strip():
                entries.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        entries.append(current.strip())
    return entries


def _substitute_generics(type_name: str, generics: dict[str, str] | None) -> str:
    if not generics:
        return type_name
    result = type_name
    for generic_name, concrete_name in generics.items():
        result = re.sub(rf"\b{re.escape(generic_name)}\b", concrete_name, result)
    return result


def _generic_arguments(type_name: str) -> list[str]:
    cleaned = strip_nullable(type_name).replace("global::", "")
    start = cleaned.find("<")
    if start == -1 or not cleaned.endswith(">"):
        return []
    return [item.strip() for item in _split_top_level(cleaned[start + 1 : -1]) if item.strip()]


def resolve_properties(
    type_name: str,
    types: dict[str, TypeInfo],
    seen: set[str] | None = None,
    generics: dict[str, str] | None = None,
) -> list[Property]:
    """Returns base-class properties first, then the derived type's own."""
    seen = seen or set()
    resolved_type = _substitute_generics(type_name, generics)
    identity = re.sub(r"\s+", "", resolved_type)
    name = simple_name(resolved_type)
    if identity in seen or name not in types:
        return []
    seen.add(identity)
    info = types[name]
    local_generics = dict(generics or {})
    if info.type_parameters:
        arguments = [_substitute_generics(item, generics) for item in _generic_arguments(resolved_type)]
        local_generics.update(dict(zip(info.type_parameters, arguments)))
    resolved: list[Property] = []
    for base in info.bases:
        resolved.extend(resolve_properties(base, types, seen, local_generics))
    resolved.extend(
        Property(
            name=prop.name,
            type=_substitute_generics(prop.type, local_generics),
            attributes=dict(prop.attributes),
            required_modifier=prop.required_modifier,
        )
        for prop in info.properties
    )
    unique: dict[str, Property] = {}
    for prop in resolved:
        unique[prop.name] = prop
    return list(unique.values())


def build_filter_fields(
    entity: str,
    types: dict[str, TypeInfo],
    enums: dict[str, list[str]],
    prefix: str = "",
    depth: int = 0,
    seen: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Builds the filterable/sortable field list for a module's entity.

    Follows FilterSortExtensions.GetFilterableFields: scalar properties are
    filterable, collections are skipped, and complex types are flattened one
    level using dotted paths.
    """
    name = simple_name(entity)
    if depth > 1 or name in seen or name not in types:
        return []
    fields: list[dict[str, Any]] = []
    for prop in resolve_properties(name, types, set()):
        path = f"{prefix}.{prop.name}" if prefix else prop.name
        if "JsonIgnore" in prop.attributes or "NotMapped" in prop.attributes:
            continue
        if is_filterable_type(prop.type, enums):
            if element_type(prop.type) is not None:
                # Collections are explicitly skipped by GetFilterableFields.
                continue
            data_type = data_type_for(prop.type, enums)
            display = prop.attributes.get("Display", "")
            sort_attribute = prop.attributes.get("Sort")
            sortable = True
            if sort_attribute is not None:
                allowed = re.search(r"Allowed\s*=\s*(true|false)", sort_attribute)
                sortable = allowed.group(1) == "true" if allowed else True
            filterable_attribute = prop.attributes.get("Filterable")
            filterable = True
            if filterable_attribute is not None:
                allowed = re.search(r"IsFilterable\s*=\s*(true|false)", filterable_attribute)
                filterable = allowed.group(1) == "true" if allowed else True
            if not filterable:
                continue
            fields.append(
                {
                    "name": path,
                    "display_name": attribute_string_argument(display, "Name")
                    or attribute_string_argument(display)
                    or _humanize(prop.name),
                    "data_type": data_type,
                    "clr_type": prop.type,
                    "nullable": prop.nullable,
                    "sortable": sortable,
                    "lov": list(enums.get(simple_name(prop.type), [])),
                    "operators": SUPPORTED_OPERATIONS[data_type],
                }
            )
        elif element_type(prop.type) is None and simple_name(prop.type) in types:
            fields.extend(
                build_filter_fields(
                    prop.type, types, enums, path, depth + 1, seen + (name,)
                )
            )
    unique: dict[str, dict[str, Any]] = {}
    for item in fields:
        unique.setdefault(item["name"], item)
    return list(unique.values())


def build_payload_schema(
    type_name: str,
    types: dict[str, TypeInfo],
    enums: dict[str, list[str]],
    depth: int = 0,
    seen: tuple[str, ...] = (),
    generics: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Builds a renderable field tree for a request DTO."""
    resolved_type = _substitute_generics(type_name, generics)
    name = simple_name(resolved_type)
    if depth > 3 or resolved_type in seen:
        return None
    collection = element_type(resolved_type)
    if collection is not None:
        item = build_payload_schema(collection, types, enums, depth, seen)
        return {
            "kind": "array",
            "clr_type": resolved_type,
            "item": item
            or {"kind": _scalar_kind(collection, enums), "clr_type": collection,
                "lov": list(enums.get(simple_name(collection), []))},
        }
    if name not in types:
        return None
    fields: list[dict[str, Any]] = []
    for prop in resolve_properties(resolved_type, types, set(), generics):
        if "JsonIgnore" in prop.attributes:
            continue
        entry: dict[str, Any] = {
            "name": prop.name,
            "clr_type": prop.type,
            "nullable": prop.nullable,
            "required": prop.required or "required" in prop.attributes,
            "display_name": _humanize(prop.name),
        }
        enum_name = _enum_for_property(prop, enums)
        child_collection = element_type(prop.type)
        if enum_name:
            entry["kind"] = "enum"
            entry["lov"] = list(enums[enum_name])
        elif child_collection is not None:
            child = build_payload_schema(prop.type, types, enums, depth + 1, seen + (resolved_type,))
            entry["kind"] = "array"
            entry["item"] = (child or {}).get("item")
        elif simple_name(prop.type) in types:
            child = build_payload_schema(prop.type, types, enums, depth + 1, seen + (resolved_type,))
            if child is None:
                entry["kind"] = "text"
            else:
                entry["kind"] = "object"
                entry["fields"] = child["fields"]
        else:
            entry["kind"] = _scalar_kind(prop.type, enums)
        constraints = _constraints(prop)
        if constraints:
            entry["constraints"] = constraints
        fields.append(entry)
    if not fields:
        return None
    return {"kind": "object", "name": name, "clr_type": resolved_type, "fields": fields}


def build_response_schema(
    type_name: str,
    types: dict[str, TypeInfo],
    enums: dict[str, list[str]],
) -> dict[str, Any] | None:
    return build_payload_schema(type_name, types, enums)


def build_form_schema(
    type_name: str,
    types: dict[str, TypeInfo],
    enums: dict[str, list[str]],
) -> dict[str, Any] | None:
    """Builds a flat field list for a ``[FromForm]`` DTO.

    Multipart bodies are flat name/value pairs, so nested objects are flattened
    to the ``Parent.Child`` names ASP.NET model binding expects.
    """
    schema = build_payload_schema(type_name, types, enums)
    if schema is None or schema.get("kind") != "object":
        return None
    fields = _flatten_form_fields(schema.get("fields", []), prefix="")
    if not fields:
        return None
    return {"name": simple_name(type_name), "clr_type": type_name, "fields": fields}


def _flatten_form_fields(fields: list[dict[str, Any]], prefix: str) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for field in fields:
        name = f"{prefix}{field['name']}"
        if field.get("kind") == "object" and field.get("fields"):
            flattened.extend(_flatten_form_fields(field["fields"], f"{name}."))
            continue
        entry = dict(field)
        entry["name"] = name
        if entry.get("kind") == "array":
            # A multipart value is a string; arrays are entered comma separated.
            item = entry.pop("item", None) or {}
            entry["kind"] = "file_list" if item.get("kind") == "file" else "text"
            if item.get("lov"):
                entry["lov"] = item["lov"]
        flattened.append(entry)
    return flattened


def form_field_kind(type_name: str, enums: dict[str, list[str]]) -> str:
    """Renderable kind for a form field declared directly on an action."""
    collection = element_type(type_name)
    if collection is not None:
        return "file_list" if _scalar_kind(collection, enums) == "file" else "text"
    if simple_name(type_name) in enums:
        return "enum"
    return _scalar_kind(type_name, enums)


def _enum_for_property(prop: Property, enums: dict[str, list[str]]) -> str | None:
    name = simple_name(prop.type)
    if name in enums:
        return name
    validation = prop.attributes.get("EnumValidation")
    if validation:
        match = re.search(r"typeof\(\s*([\w.]+)\s*\)", validation)
        if match:
            candidate = match.group(1).split(".")[-1]
            if candidate in enums:
                return candidate
    return None


def _scalar_kind(type_name: str, enums: dict[str, list[str]]) -> str:
    if simple_name(type_name).lower() in {"iformfile", "formfile"}:
        return "file"
    data_type = data_type_for(type_name, enums)
    if data_type == ATTRIBUTE_TYPE_NUMBER:
        return "integer" if simple_name(type_name).lower() in {
            "int", "int16", "int32", "int64", "long", "short", "byte"
        } else "number"
    if data_type == ATTRIBUTE_TYPE_FLAG:
        return "boolean"
    if data_type == ATTRIBUTE_TYPE_DATE:
        return "date"
    if simple_name(type_name).lower() == "guid":
        return "guid"
    if simple_name(type_name).lower() in {"object", "jsonelement"}:
        return "json"
    return "text"


def _constraints(prop: Property) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    length = prop.attributes.get("StringLength") or prop.attributes.get("MaxLength")
    if length:
        number = re.match(r"\s*(\d+)", length)
        if number:
            constraints["max_length"] = int(number.group(1))
    value_range = prop.attributes.get("Range")
    if value_range:
        numbers = re.findall(r"-?\d+(?:\.\d+)?", value_range)
        if len(numbers) >= 2:
            constraints["minimum"] = float(numbers[0])
            constraints["maximum"] = float(numbers[1])
    if "RestrictPatch" in prop.attributes:
        constraints["restrict_patch"] = True
    if "Key" in prop.attributes:
        constraints["key"] = True
    return constraints


def _humanize(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", " ", name).replace("_", " ").strip()
