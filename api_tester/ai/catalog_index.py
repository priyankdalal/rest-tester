"""Keyword search over the catalog and the compact endpoint cards sent to the model.

Search is local and deterministic: no embeddings, no network. Endpoints are
scored by weighted token overlap (controller and path words count most) with a
fuzzy bonus for near matches, and the verb in the prompt nudges the HTTP method.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from ..catalog import Catalog, Endpoint
from ..schema import FilterSchema

FILTER_PARAMETER = "Filter"
SORT_PARAMETER = "Sort"

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NON_WORD = re.compile(r"[^A-Za-z0-9]+")

STOPWORDS = frozenset(
    "a an the all any of to for in on at by with and or from me my please show give "
    "test check call hit run api apis endpoint endpoints request service using use "
    "where which that whose is are be it its this those these then than value values "
    "first top i want need can you should".split()
)

METHOD_HINTS: dict[str, str] = {
    **dict.fromkeys(("create", "add", "new", "insert", "post", "register"), "POST"),
    **dict.fromkeys(("update", "edit", "change", "modify", "rename", "put"), "PUT"),
    **dict.fromkeys(("patch",), "PATCH"),
    **dict.fromkeys(("delete", "remove", "destroy", "drop"), "DELETE"),
    **dict.fromkeys(("list", "get", "fetch", "find", "search", "read", "retrieve", "view", "filter", "sort"), "GET"),
    **dict.fromkeys(("count", "head", "many", "total"), "HEAD"),
}


def split_words(text: str) -> list[str]:
    words: list[str] = []
    for chunk in _NON_WORD.split(text or ""):
        if chunk:
            words.extend(part for part in _CAMEL.split(chunk) if part)
    return [word.lower() for word in words]


def singular(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ses", "xes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(text: str) -> list[str]:
    return [singular(word) for word in split_words(text)]


def query_tokens(text: str) -> list[str]:
    return [word for word in tokens(text) if word not in STOPWORDS and len(word) > 1]


@dataclass(frozen=True)
class SearchHit:
    endpoint: Endpoint
    score: float

    def summary(self) -> str:
        return endpoint_summary(self.endpoint)


def endpoint_summary(endpoint: Endpoint) -> str:
    return (
        f"{endpoint.id} | {endpoint.method} {endpoint.path} | "
        f"{endpoint.service} {endpoint.controller}.{endpoint.action}"
    )


class CatalogIndex:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.endpoints: dict[str, Endpoint] = {
            endpoint.id: endpoint for service in catalog.services for endpoint in service.endpoints
        }
        self._documents: dict[str, dict[str, float]] = {
            endpoint.id: self._document(endpoint) for endpoint in self.endpoints.values()
        }
        frequency: dict[str, int] = {}
        for document in self._documents.values():
            for word in document:
                frequency[word] = frequency.get(word, 0) + 1
        total = max(len(self._documents), 1)
        # Inverse document frequency: "brand" identifies an endpoint, "name" or
        # "id" (present almost everywhere) barely does.
        self._idf = {
            word: 0.25 + math.log((total + 1) / (count + 1)) / math.log(total + 1)
            for word, count in frequency.items()
        }

    def get(self, endpoint_id: str) -> Endpoint | None:
        return self.endpoints.get(endpoint_id)

    def _near_words(self, word: str) -> tuple[str, ...]:
        return tuple(
            other
            for other in self._idf
            if other != word
            and abs(len(other) - len(word)) <= 2
            and SequenceMatcher(None, word, other).ratio() >= 0.85
        )

    def _document(self, endpoint: Endpoint) -> dict[str, float]:
        weights: dict[str, float] = {}

        def add(text: str, weight: float) -> None:
            for word in tokens(text):
                if len(word) > 1 and weights.get(word, 0) < weight:
                    weights[word] = weight

        for parameter in endpoint.parameters:
            add(parameter.name, 0.5)
        schema = self.catalog.payload_schema(endpoint.payload_schema)
        for name in _payload_field_names(schema):
            add(name, 0.5)
        add(endpoint.service, 1.0)
        add(endpoint.action, 2.0)
        add(re.sub(r"\{[^}]*\}", " ", endpoint.path), 2.0)
        add(endpoint.controller, 3.0)
        return weights

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        service: str | None = None,
        method: str | None = None,
    ) -> list[SearchHit]:
        words = query_tokens(query)
        raw_words = set(split_words(query))
        hinted = method or next((METHOD_HINTS[word] for word in split_words(query) if word in METHOD_HINTS), None)
        list_cues = {"all", "list", "every", "newest", "latest", "oldest", "top", "first", "last", "recent", "whose", "where", "sorted", "sort", "filter", "filtered", "count", "many"}
        wants_single = not (raw_words & list_cues) and (
            bool(raw_words & {"id", "single", "one", "detail", "details"}) or bool(re.search(r"\b\d+\b", query or ""))
        )
        hits: list[SearchHit] = []
        near = {word: self._near_words(word) for word in words}
        for endpoint_id, document in self._documents.items():
            endpoint = self.endpoints[endpoint_id]
            if service and endpoint.service.lower() != service.lower():
                continue
            score = 0.0
            for word in words:
                if word in document:
                    score += document[word] * self._idf[word]
                    continue
                score += max(
                    (document[other] * self._idf[other] * 0.6 for other in near[word] if other in document),
                    default=0.0,
                )
            if score <= 0:
                continue
            if hinted:
                matches = endpoint.method == hinted or (hinted == "PUT" and endpoint.method == "PATCH")
                score += 1.0 if matches else -0.5
            elif endpoint.method == "GET":
                score += 0.5
            has_path_parameter = any(item.source == "path" for item in endpoint.parameters)
            if endpoint.method == "GET":
                score += 0.4 if has_path_parameter == wants_single else -0.4
            hits.append(SearchHit(endpoint, round(score, 3)))
        hits.sort(key=lambda hit: (-hit.score, len(hit.endpoint.path), hit.endpoint.id))
        return hits[:limit]

    # -- cards ---------------------------------------------------------------
    def filter_schema(self, endpoint: Endpoint) -> FilterSchema | None:
        schema = self.catalog.endpoint_filter_schema(endpoint)
        if schema is None:
            return None
        names = {item.name for item in endpoint.parameters if item.source == "query"}
        return schema if names & {FILTER_PARAMETER, SORT_PARAMETER} else None

    def card(
        self,
        endpoint: Endpoint,
        *,
        max_filter_fields: int = 40,
        max_lov: int = 12,
        include_response: bool = False,
    ) -> str:
        lines = [
            f"id: {endpoint.id}   {endpoint.method} {endpoint.path}   service: {endpoint.service}"
            f"   action: {endpoint.controller}.{endpoint.action}"
        ]
        schema = self.filter_schema(endpoint)
        structured = {FILTER_PARAMETER, SORT_PARAMETER} if schema is not None else set()
        path = [item for item in endpoint.parameters if item.source == "path"]
        query = [
            item
            for item in endpoint.parameters
            if item.source in {"query", "header", "form"} and item.name not in structured
        ]
        lines.append("path params: " + (", ".join(_parameter_text(item, max_lov) for item in path) or "none"))
        lines.append("other params: " + (", ".join(_parameter_text(item, max_lov) for item in query) or "none"))
        if schema is not None:
            fields = schema.fields[:max_filter_fields]
            lines.append(
                "filterable (use \"filters\"): "
                + "  ".join(_filter_field_text(field, max_lov) for field in fields)
            )
            if len(schema.fields) > max_filter_fields:
                lines.append(f"  (+{len(schema.fields) - max_filter_fields} more filterable fields)")
            lines.append(
                "sortable (use \"sort\"): " + (", ".join(item.name for item in schema.sortable_fields) or "none")
            )
        else:
            lines.append("filterable: not supported (leave \"filters\" and \"sort\" empty)")
        payload = self.catalog.payload_schema(endpoint.payload_schema)
        if payload is not None:
            lines.append("payload: " + _payload_text(payload, max_lov))
        elif endpoint.payload is not None:
            lines.append("payload example: " + _truncate(str(endpoint.payload), 400))
        else:
            lines.append("payload: none")
        lines.append(f"expected status: {endpoint.expected_status}")
        if include_response:
            lines.append("response: " + self.response_text(endpoint, max_lov))
        return "\n".join(lines)

    def response_text(self, endpoint: Endpoint, max_lov: int = 12) -> str:
        schema = self.catalog.response_schema(endpoint.response_schema)
        if not schema:
            fields = self.likely_record_fields(endpoint)
            if fields:
                return "not declared in the catalog; the record likely has: " + ", ".join(fields)
            return "not declared in the catalog"
        if is_generic_schema(schema):
            # Shared generic wrappers (e.g. IEntitiesDto<T>) are stored once, so
            # only their top level is reliable for every endpoint.
            return "{" + ", ".join(f"{item.get('name')}: {item.get('kind', 'value')}" for item in schema.get("fields", ())) + "}"
        return _payload_text(schema, max_lov)

    def likely_record_fields(self, endpoint: Endpoint, limit: int = 30) -> list[str]:
        """Best guess at a resource's record fields when no response schema is declared.

        The filterable entity of a sibling list endpoint is the stored record,
        so its properties are what a create/read endpoint usually returns.
        """
        for sibling in self.siblings(endpoint):
            schema = self.catalog.endpoint_filter_schema(sibling)
            if schema is not None and schema.fields:
                return [item.name for item in schema.fields][:limit]
        return []

    def siblings(self, endpoint: Endpoint) -> list[Endpoint]:
        """Every endpoint on the same service controller - one resource's full lifecycle."""
        return [
            item
            for item in self.endpoints.values()
            if item.service == endpoint.service and item.controller == endpoint.controller
        ]


def is_generic_schema(schema: dict[str, Any]) -> bool:
    return "<" in str(schema.get("clr_type") or "")


def _parameter_text(parameter, max_lov: int) -> str:
    text = f"{parameter.name}({parameter.type}{', required' if parameter.required else ''}"
    if parameter.values:
        text += ", one of " + "|".join(parameter.values[:max_lov])
    return text + ")"


def _filter_field_text(field, max_lov: int) -> str:
    operators = ",".join(item.value for item in field.operators)
    kind = field.data_type
    if field.lov:
        kind = "LOV " + "|".join(field.lov[:max_lov])
    return f"{field.name}[{kind}: {operators}]"


def _payload_field_names(schema: dict[str, Any] | None) -> list[str]:
    if not schema:
        return []
    if schema.get("kind") == "array":
        return _payload_field_names(schema.get("item"))
    return [str(item.get("name", "")) for item in schema.get("fields", ())]


def _payload_text(schema: dict[str, Any], max_lov: int, depth: int = 0) -> str:
    if schema.get("kind") == "array":
        item = schema.get("item") or {}
        inner = _payload_text(item, max_lov, depth) if item.get("fields") else item.get("kind", "value")
        return f"array of {inner}"
    parts = []
    for field in schema.get("fields", ()):
        kind = field.get("kind", "text")
        text = f"{field.get('name')}: {kind}"
        if field.get("required"):
            text += " required"
        if field.get("lov"):
            text += " one of " + "|".join(str(item) for item in field["lov"][:max_lov])
        if kind == "object" and depth < 1 and field.get("fields"):
            text += " " + _payload_text(field, max_lov, depth + 1)
        if kind == "array" and depth < 1 and (field.get("item") or {}).get("fields"):
            text += " of " + _payload_text(field["item"], max_lov, depth + 1)
        parts.append(text)
    return "{" + ", ".join(parts) + "}"


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"
