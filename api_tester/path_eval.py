"""Path evaluation for the response inspector.

JSON paths use the grammar shared by suite assertions/captures
(``suite.resolve_path``) and structural-baseline paths (``baseline``):

* optional ``$`` root, ``.name`` / leading ``name`` property steps;
* ``[n]`` array index;
* ``[*]`` wildcard, fanning out over every array element (baseline);
* ``[key=value]`` identity filter, keeping array elements whose ``key``
  equals ``value`` — the form baseline differences are reported in.

A property step on an array projects the property across its elements, and
property names fall back to a case-insensitive match, exactly like
assertions. Without ``[*]`` or a filter the result therefore equals
``suite.resolve_path`` for the same path.

XML paths use the XPath subset supported by :mod:`xml.etree.ElementTree`,
plus absolute ``/root/...`` paths and trailing ``text()`` / ``@attr`` steps.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PathResult:
    """Outcome of evaluating a path. ``error`` is empty on success."""

    ok: bool
    values: list[Any] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    error: str = ""
    #: 1-based column of the offending path character, 0 when not positional.
    column: int = 0
    #: True when the path fanned out (``[*]``/filter/XPath) — show a list.
    multiple: bool = False

    @property
    def count(self) -> int:
        return len(self.values)


# ---------------------------------------------------------------- JSON paths

@dataclass(frozen=True)
class _Step:
    kind: str  # "key", "index", "wildcard", "filter"
    column: int
    key: str = ""
    index: int = 0
    value: Any = None
    text: str = ""


class PathSyntaxError(ValueError):
    def __init__(self, message: str, column: int) -> None:
        super().__init__(message)
        self.column = column


_NAME = re.compile(r"[^.\[\]=\s]+")


def parse_json_path(path: str) -> list[_Step]:
    """Tokenises a JSON path, raising :class:`PathSyntaxError` on bad input."""
    text = path or ""
    length = len(text)
    position = 0
    while position < length and text[position].isspace():
        position += 1
    if position < length and text[position] == "$":
        position += 1
    steps: list[_Step] = []
    first = True
    while position < length:
        char = text[position]
        column = position + 1
        if char.isspace():
            if text[position:].strip():
                raise PathSyntaxError("Spaces are not allowed inside a path", column)
            break
        if char == ".":
            position += 1
            if position < length and text[position] == ".":
                raise PathSyntaxError(
                    "Recursive descent '..' is not supported; use [*] instead", column
                )
            match = _NAME.match(text, position)
            if match is None:
                raise PathSyntaxError("Expected a property name after '.'", column)
            steps.append(_Step("key", position + 1, key=match.group(), text=match.group()))
            position = match.end()
        elif char == "[":
            close = text.find("]", position)
            if close < 0:
                raise PathSyntaxError("Unclosed '['", column)
            inner = text[position + 1:close].strip()
            steps.append(_parse_bracket(inner, column))
            position = close + 1
        elif char == "]":
            raise PathSyntaxError("Unexpected ']' without a matching '['", column)
        elif first:
            match = _NAME.match(text, position)
            if match is None:
                raise PathSyntaxError(f"Unexpected character {char!r}", column)
            steps.append(_Step("key", column, key=match.group(), text=match.group()))
            position = match.end()
        else:
            raise PathSyntaxError(
                f"Unexpected character {char!r}; separate properties with '.'", column
            )
        first = False
    return steps


def _parse_bracket(inner: str, column: int) -> _Step:
    if not inner:
        raise PathSyntaxError("Empty brackets; use [n], [*] or [key=value]", column)
    if inner == "*":
        return _Step("wildcard", column, text="[*]")
    if inner.lstrip("-").isdigit():
        index = int(inner)
        if index < 0:
            raise PathSyntaxError("Negative indexes are not supported", column)
        return _Step("index", column, index=index, text=f"[{index}]")
    if "=" in inner:
        key, _, raw = inner.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not key or not _NAME.fullmatch(key):
            raise PathSyntaxError("Expected [key=value] with a property name", column)
        if not raw:
            raise PathSyntaxError(f"Missing value after '{key}='", column)
        return _Step("filter", column, key=key, value=_literal(raw), text=f"[{inner}]")
    if (inner[0] in "'\"") and inner[-1:] == inner[0] and len(inner) > 1:
        name = inner[1:-1]
        return _Step("key", column, key=name, text=f"[{inner}]")
    raise PathSyntaxError(f"Invalid index {inner!r}; use [n], [*] or [key=value]", column)


def _literal(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if len(raw) > 1 and raw[0] == raw[-1] == "'":
            return raw[1:-1]
        return raw


def _lookup(document: dict[str, Any], key: str) -> tuple[bool, str]:
    if key in document:
        return True, key
    match = next((name for name in document if name.lower() == key.lower()), None)
    return (match is not None), (match or key)


def _filter_matches(item: Any, key: str, expected: Any) -> bool:
    if not isinstance(item, dict):
        return False
    found, name = _lookup(item, key)
    if not found:
        return False
    actual = item[name]
    if actual == expected:
        return True
    # A bare value such as [code=ABC] or [id=5] may be compared against a
    # differently typed JSON value; fall back to the text form.
    text = expected if isinstance(expected, str) else json.dumps(expected)
    if isinstance(actual, bool):
        return str(actual).lower() == text.lower()
    return str(actual) == text if not isinstance(actual, (dict, list)) else False


def _describe(value: Any) -> str:
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return f"an array of {len(value)}"
    if value is None:
        return "null"
    name = {"int": "integer", "float": "number", "str": "string", "bool": "boolean"}.get(
        type(value).__name__, type(value).__name__
    )
    return f"a {name}"


def _key_hint(values: list[Any]) -> str:
    keys: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, dict):
                keys.extend(name for name in item if name not in keys)
    if not keys:
        return ""
    shown = ", ".join(keys[:8]) + (" …" if len(keys) > 8 else "")
    return f". Available: {shown}"


def evaluate_json_path(document: Any, path: str) -> PathResult:
    """Evaluates ``path`` against a decoded JSON document."""
    try:
        steps = parse_json_path(path)
    except PathSyntaxError as exc:
        return PathResult(False, error=str(exc), column=exc.column)
    matches: list[tuple[str, Any]] = [("$", document)]
    multiple = False
    for step in steps:
        next_matches: list[tuple[str, Any]] = []
        for location, value in matches:
            if step.kind == "key":
                if isinstance(value, dict):
                    found, name = _lookup(value, step.key)
                    if found:
                        next_matches.append((f"{location}.{name}", value[name]))
                elif isinstance(value, list):
                    # Projection is case-sensitive, exactly like assertions.
                    projected = [
                        item[step.key]
                        for item in value
                        if isinstance(item, dict) and step.key in item
                    ]
                    if projected:
                        next_matches.append((f"{location}.{step.key}", projected))
            elif step.kind == "index":
                if isinstance(value, list) and step.index < len(value):
                    next_matches.append((f"{location}[{step.index}]", value[step.index]))
            elif step.kind == "wildcard":
                if isinstance(value, list):
                    next_matches.extend(
                        (f"{location}[{index}]", item) for index, item in enumerate(value)
                    )
                elif isinstance(value, dict):
                    next_matches.extend(
                        (f"{location}.{name}", item) for name, item in value.items()
                    )
            elif step.kind == "filter":
                if isinstance(value, list):
                    next_matches.extend(
                        (f"{location}[{index}]", item)
                        for index, item in enumerate(value)
                        if _filter_matches(item, step.key, step.value)
                    )
        if step.kind in ("wildcard", "filter"):
            multiple = True
        if not next_matches:
            return PathResult(
                False,
                error=_no_match_message(step, [value for _, value in matches], multiple),
                column=step.column,
                multiple=multiple,
            )
        matches = next_matches
    return PathResult(
        True,
        values=[value for _, value in matches],
        paths=[location for location, _ in matches],
        multiple=multiple,
    )


def _no_match_message(step: _Step, parents: list[Any], multiple: bool) -> str:
    if step.kind == "key":
        if not any(isinstance(value, (dict, list)) for value in parents):
            return f"'{step.key}' cannot be read from {_describe(parents[0])}"
        return f"No property '{step.key}'{_key_hint(parents)}"
    if step.kind == "index":
        arrays = [value for value in parents if isinstance(value, list)]
        if not arrays:
            return f"[{step.index}] needs an array, found {_describe(parents[0])}"
        return f"Index [{step.index}] is out of range (array of {len(arrays[0])})"
    if step.kind == "wildcard":
        return f"[*] needs an array or object, found {_describe(parents[0])}"
    if not any(isinstance(value, list) for value in parents):
        return f"{step.text} needs an array, found {_describe(parents[0])}"
    return f"No element matches {step.text}"


def json_result_text(result: PathResult) -> str:
    if not result.ok:
        return ""
    value: Any = result.values if result.multiple else result.values[0]
    return json.dumps(value, indent=2, ensure_ascii=False)


# ----------------------------------------------------------------- XML paths

_XML_TRAILER = re.compile(r"/(text\(\)|@[\w:.-]+)$")


def evaluate_xml_path(root: ElementTree.Element, path: str) -> PathResult:
    """Evaluates an ElementTree XPath (plus absolute paths, text() and @attr)."""
    expression = (path or "").strip()
    if not expression or expression in {"/", "."}:
        return PathResult(True, values=[root], paths=[f"/{root.tag}"])
    trailer = ""
    match = _XML_TRAILER.search(expression)
    if match:
        trailer = match.group(1)
        expression = expression[: match.start()] or "."
    anchor = root
    if expression.startswith("//"):
        wrapper = ElementTree.Element("__document__")
        wrapper.append(root)
        anchor, expression = wrapper, "." + expression
    elif expression.startswith("/"):
        wrapper = ElementTree.Element("__document__")
        wrapper.append(root)
        anchor, expression = wrapper, "." + expression
    try:
        elements = anchor.findall(expression)
    except SyntaxError as exc:
        return PathResult(False, error=f"Invalid XPath: {exc}", multiple=True)
    except (KeyError, TypeError, ValueError):
        return PathResult(
            False,
            error="Invalid XPath expression (check brackets and predicates)",
            multiple=True,
        )
    if not elements:
        return PathResult(False, error="No element matches this XPath", multiple=True)
    if trailer == "text()":
        values: list[Any] = [(element.text or "").strip() for element in elements]
    elif trailer.startswith("@"):
        name = trailer[1:]
        values = [element.get(name) for element in elements if name in element.attrib]
        if not values:
            return PathResult(False, error=f"No matched element has attribute '{name}'", multiple=True)
    else:
        values = list(elements)
    return PathResult(True, values=values, paths=[], multiple=len(values) != 1)


def xml_result_text(result: PathResult) -> str:
    if not result.ok:
        return ""
    parts: list[str] = []
    for value in result.values:
        if isinstance(value, ElementTree.Element):
            element = _copy_without_tail(value)
            ElementTree.indent(element, space="  ")
            parts.append(ElementTree.tostring(element, encoding="unicode"))
        else:
            parts.append("" if value is None else str(value))
    return "\n".join(parts)


def _copy_without_tail(element: ElementTree.Element) -> ElementTree.Element:
    clone = ElementTree.fromstring(ElementTree.tostring(element, encoding="unicode"))
    clone.tail = None
    return clone


def parse_xml(text: str) -> ElementTree.Element:
    return ElementTree.fromstring(text.strip())
