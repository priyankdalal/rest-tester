"""The "request" plan: one API call described as structured JSON.

Filters and sorts are structured (field / op / values). The converter encodes
them with the app's own ``encode_filter`` / ``encode_sort``, so the model never
writes the wire grammar and cannot get it wrong.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from ..catalog import Endpoint
from ..client import parameter_enabled_key
from ..schema import VALUE_DELIMITER, encode_filter, encode_sort
from .catalog_index import FILTER_PARAMETER, SORT_PARAMETER

STATUSES = ("plan", "clarify", "unsupported")
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@dataclass(frozen=True)
class FilterSpec:
    field: str
    op: str
    values: tuple[str, ...] = ()


@dataclass(frozen=True)
class SortSpec:
    field: str
    descending: bool = False


@dataclass(frozen=True)
class RequestPlan:
    status: str = "plan"
    title: str = ""
    summary: str = ""
    endpoint_id: str = ""
    parameters: tuple[tuple[str, str], ...] = ()
    filters: tuple[FilterSpec, ...] = ()
    filter_join: str = "and"
    sort: tuple[SortSpec, ...] = ()
    payload: Any = None
    expected_status: str = ""
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    question: str = ""
    choices: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: Any) -> "RequestPlan":
        if not isinstance(value, dict):
            raise ValueError("The plan must be a JSON object.")
        status = _text(value.get("status") or "plan").lower()
        return cls(
            status=status if status in STATUSES else "plan",
            title=_text(value.get("title")),
            summary=_text(value.get("summary")),
            endpoint_id=_text(value.get("endpoint_id")),
            parameters=tuple(
                (_text(item.get("name")), _text(item.get("value")))
                for item in _list(value.get("parameters"))
                if isinstance(item, dict) and _text(item.get("name"))
            ),
            filters=tuple(
                FilterSpec(
                    field=_text(item.get("field")),
                    op=_text(item.get("op")).lower(),
                    values=tuple(_text(entry) for entry in _list(item.get("values"))),
                )
                for item in _list(value.get("filters"))
                if isinstance(item, dict)
            ),
            filter_join="or" if _text(value.get("filter_join")).lower() == "or" else "and",
            sort=tuple(
                SortSpec(field=_text(item.get("field")), descending=bool(item.get("descending")))
                for item in _list(value.get("sort"))
                if isinstance(item, dict) and _text(item.get("field"))
            ),
            payload=_payload(value.get("payload")),
            expected_status=_text(value.get("expected_status")),
            assumptions=tuple(_text(item) for item in _list(value.get("assumptions")) if _text(item)),
            warnings=tuple(_text(item) for item in _list(value.get("warnings")) if _text(item)),
            question=_text(value.get("question")),
            choices=tuple(_text(item) for item in _list(value.get("choices")) if _text(item)),
        )

    def to_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["parameters"] = [{"name": name, "value": item} for name, item in self.parameters]
        value["filters"] = [
            {"field": item.field, "op": item.op, "values": list(item.values)} for item in self.filters
        ]
        value["sort"] = [{"field": item.field, "descending": item.descending} for item in self.sort]
        for key in ("assumptions", "warnings", "choices"):
            value[key] = list(value[key])
        return value

    def with_changes(self, **changes: Any) -> "RequestPlan":
        return replace(self, **changes)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _payload(value: Any) -> Any:
    # Small models sometimes return the payload as a JSON string.
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == "null":
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    if value == {} or value == []:
        return None
    return value


def request_plan_schema(candidate_ids: list[str]) -> dict[str, Any]:
    """JSON Schema enforced by the provider.

    ``endpoint_id`` is restricted to the retrieved candidates, so the model
    cannot invent an endpoint. Every key is required to keep the grammar
    simple for local models.
    """
    string_list = {"type": "array", "items": {"type": "string"}}
    properties: dict[str, Any] = {
        "status": {"type": "string", "enum": list(STATUSES)},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "endpoint_id": {"type": "string", "enum": [*candidate_ids, ""]},
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                "required": ["name", "value"],
            },
        },
        "filters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "op": {"type": "string"},
                    "values": string_list,
                },
                "required": ["field", "op", "values"],
            },
        },
        "filter_join": {"type": "string", "enum": ["and", "or"]},
        "sort": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"field": {"type": "string"}, "descending": {"type": "boolean"}},
                "required": ["field", "descending"],
            },
        },
        "payload": {"description": "JSON body for POST/PUT/PATCH, otherwise null"},
        "expected_status": {"type": "string"},
        "assumptions": string_list,
        "warnings": string_list,
        "question": {"type": "string"},
        "choices": string_list,
    }
    return {"type": "object", "properties": properties, "required": list(properties)}


@dataclass(frozen=True)
class ExplorerRequest:
    """What API Explorer needs to open a request: the editor's value map, body and status."""

    endpoint_id: str
    values: dict[str, str] = field(default_factory=dict)
    payload: Any = None
    expected_status: str = "200-299"


def to_explorer_request(plan: RequestPlan, endpoint: Endpoint, structured: bool) -> ExplorerRequest:
    """Converts a validated plan into the API Explorer draft for ``endpoint``.

    Query parameters the plan does not set are switched off so the request
    sends exactly what the plan describes. ``structured`` says whether the
    endpoint's Filter/Sort take the structured grammar.
    """
    given = dict(plan.parameters)
    values: dict[str, str] = {}
    for parameter in endpoint.parameters:
        key = f"{parameter.source}:{parameter.name}"
        if structured and parameter.source == "query" and parameter.name in (FILTER_PARAMETER, SORT_PARAMETER):
            continue
        if parameter.name in given:
            values[key] = given[parameter.name]
            if parameter.source == "query":
                values[parameter_enabled_key("query", parameter.name)] = "true"
        else:
            values[key] = ""
            if parameter.source == "query":
                values[parameter_enabled_key("query", parameter.name)] = "false"
    if structured:
        names = {item.name for item in endpoint.parameters if item.source == "query"}
        if FILTER_PARAMETER in names:
            text = encode_filter(
                [
                    {
                        "name": item.field,
                        "operator": item.op,
                        "value": VALUE_DELIMITER.join(item.values),
                        "and": plan.filter_join == "and",
                    }
                    for item in plan.filters
                ]
            )
            values[f"query:{FILTER_PARAMETER}"] = text
            values[parameter_enabled_key("query", FILTER_PARAMETER)] = "true" if text else "false"
        if SORT_PARAMETER in names:
            text = encode_sort([{"name": item.field, "descending": item.descending} for item in plan.sort])
            values[f"query:{SORT_PARAMETER}"] = text
            values[parameter_enabled_key("query", SORT_PARAMETER)] = "true" if text else "false"
    payload = None if endpoint.method.upper() in READ_METHODS else plan.payload
    return ExplorerRequest(
        endpoint_id=endpoint.id,
        values=values,
        payload=payload,
        expected_status=plan.expected_status or endpoint.expected_status,
    )
