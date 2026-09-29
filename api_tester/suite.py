"""Test-suite model: cases, assertions, captured variables, and persistence.

A suite is a named, saveable collection of test cases. Each case pins one
catalog endpoint together with the concrete path/query/header/form values, the
request payload, the expected status, and the assertions that must hold for the
response. Cases may capture values out of a response into suite variables so a
later case can reference them with ``{{name}}`` placeholders.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from collections.abc import Iterable
from uuid import uuid4

from .baseline import BaselineConfig

SUITE_SCHEMA_VERSION = 2

#: Assertion kinds and whether they need a path and/or an expected value.
ASSERTION_KINDS: dict[str, dict[str, Any]] = {
    "body_contains": {"label": "Body contains text", "path": False, "value": True},
    "body_not_contains": {"label": "Body does not contain text", "path": False, "value": True},
    "body_matches": {"label": "Body matches regex", "path": False, "value": True},
    "json_equals": {"label": "JSON field equals", "path": True, "value": True},
    "json_not_equals": {"label": "JSON field does not equal", "path": True, "value": True},
    "json_contains": {"label": "JSON field contains", "path": True, "value": True},
    "json_exists": {"label": "JSON field exists", "path": True, "value": False},
    "json_absent": {"label": "JSON field is absent", "path": True, "value": False},
    "json_not_empty": {"label": "JSON field is not empty", "path": True, "value": False},
    "json_length_eq": {"label": "JSON length equals", "path": True, "value": True},
    "json_length_gte": {"label": "JSON length at least", "path": True, "value": True},
    "json_length_lte": {"label": "JSON length at most", "path": True, "value": True},
    "json_type": {"label": "JSON field type is", "path": True, "value": True},
    "header_equals": {"label": "Response header equals", "path": True, "value": True},
    "header_exists": {"label": "Response header exists", "path": True, "value": False},
    "max_duration_ms": {"label": "Responds within (ms)", "path": False, "value": True},
    "response_schema": {
        "label": "Matches response schema",
        "path": False,
        "value": False,
    },
    "baseline": {
        "label": "Matches structural baseline",
        "path": False,
        "value": False,
    },
}

JSON_TYPE_NAMES = {
    type(None): "null",
    bool: "boolean",
    int: "number",
    float: "number",
    str: "string",
    list: "array",
    dict: "object",
}

_MISSING = object()
_VARIABLE_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_.\-]+)\s*\}\}")


@dataclass
class Assertion:
    kind: str = "json_exists"
    path: str = ""
    value: str = ""
    enabled: bool = True

    @property
    def label(self) -> str:
        meta = ASSERTION_KINDS.get(self.kind, {})
        text = str(meta.get("label", self.kind))
        if meta.get("path") and self.path:
            text = f"{text} [{self.path}]"
        if meta.get("value"):
            text = f"{text}: {self.value}"
        return text

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Assertion":
        return cls(
            kind=value.get("kind", "json_exists"),
            path=value.get("path", ""),
            value=value.get("value", ""),
            enabled=value.get("enabled", True),
        )


@dataclass
class Capture:
    """Stores a value from a response into a suite variable for later cases."""

    name: str = ""
    path: str = ""
    source: str = "body"  # body | header | status

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Capture":
        return cls(
            name=value.get("name", ""),
            path=value.get("path", ""),
            source=value.get("source", "body"),
        )


@dataclass
class TestCase:
    endpoint_id: str
    name: str = ""
    id: str = field(default_factory=lambda: uuid4().hex[:12])
    description: str = ""
    values: dict[str, str] = field(default_factory=dict)
    payload: Any = None
    expected_status: str = "200-299"
    assertions: list[Assertion] = field(default_factory=list)
    captures: list[Capture] = field(default_factory=list)
    enabled: bool = True
    authentication: str = "inherit"
    depends_on: list[str] = field(default_factory=list)
    phase: str = "normal"
    always_run: bool = False
    baseline: BaselineConfig = field(default_factory=BaselineConfig)

    #: Stops pytest from collecting this domain class as a test class.
    __test__ = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TestCase":
        return cls(
            id=value.get("id") or uuid4().hex[:12],
            endpoint_id=value["endpoint_id"],
            name=value.get("name", ""),
            description=value.get("description", ""),
            values=dict(value.get("values", {})),
            payload=value.get("payload"),
            expected_status=value.get("expected_status", "200-299"),
            assertions=[Assertion.from_dict(item) for item in value.get("assertions", [])],
            captures=[Capture.from_dict(item) for item in value.get("captures", [])],
            enabled=value.get("enabled", True),
            authentication=str(value.get("authentication", "inherit")),
            depends_on=[str(item) for item in value.get("depends_on", [])],
            phase=str(value.get("phase", "normal")),
            always_run=bool(value.get("always_run", False)),
            baseline=BaselineConfig.from_dict(value.get("baseline")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TestSuite:
    name: str = "New suite"
    description: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    cases: list[TestCase] = field(default_factory=list)
    stop_on_failure: bool = False
    path: Path | None = None

    #: Stops pytest from collecting this domain class as a test class.
    __test__ = False

    @classmethod
    def from_dict(cls, value: dict[str, Any], path: Path | None = None) -> "TestSuite":
        suite = cls(
            name=value.get("name", "New suite"),
            description=value.get("description", ""),
            variables=dict(value.get("variables", {})),
            cases=[TestCase.from_dict(item) for item in value.get("cases", [])],
            stop_on_failure=value.get("stop_on_failure", False),
            path=path,
        )
        suite.validate_dependencies()
        return suite

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SUITE_SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "variables": self.variables,
            "stop_on_failure": self.stop_on_failure,
            "cases": [case.to_dict() for case in self.cases],
        }

    def case(self, case_id: str) -> TestCase | None:
        return next((item for item in self.cases if item.id == case_id), None)

    def validate_dependencies(self) -> None:
        """Reject invalid dependency graphs before any requests are sent."""
        cases_by_id: dict[str, TestCase] = {}
        for case in self.cases:
            if case.id in cases_by_id:
                raise ValueError(f"Duplicate test case ID: {case.id}")
            if case.phase not in {"normal", "cleanup"}:
                raise ValueError(
                    f"Invalid phase {case.phase!r} for case {case.id}; "
                    "expected 'normal' or 'cleanup'."
                )
            cases_by_id[case.id] = case

        for case in self.cases:
            for dependency_id in case.depends_on:
                if dependency_id == case.id:
                    raise ValueError(f"Case {case.id} cannot depend on itself.")
                dependency = cases_by_id.get(dependency_id)
                if dependency is None:
                    raise ValueError(
                        f"Case {case.id} depends on unknown case {dependency_id}."
                    )
                if case.phase == "normal" and dependency.phase == "cleanup":
                    raise ValueError(
                        f"Normal case {case.id} cannot depend on cleanup case "
                        f"{dependency_id}."
                    )

        state: dict[str, int] = {}
        trail: list[str] = []

        def visit(case_id: str) -> None:
            if state.get(case_id) == 2:
                return
            if state.get(case_id) == 1:
                start = trail.index(case_id)
                cycle = trail[start:] + [case_id]
                raise ValueError(f"Dependency cycle detected: {' -> '.join(cycle)}")
            state[case_id] = 1
            trail.append(case_id)
            for dependency_id in cases_by_id[case_id].depends_on:
                visit(dependency_id)
            trail.pop()
            state[case_id] = 2

        for case in self.cases:
            visit(case.id)


def save_suite(suite: TestSuite, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(suite.to_dict(), stream, indent=2)
        stream.write("\n")
    suite.path = path


def load_suite(path: Path) -> TestSuite:
    with path.open(encoding="utf-8") as stream:
        return TestSuite.from_dict(json.load(stream), path=path)


def list_suites(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(directory.glob("*.json"))


def resolve_path(document: Any, path: str) -> Any:
    """Resolves ``a.b[0].c`` against a decoded JSON document.

    Returns the sentinel ``_MISSING`` when any segment cannot be resolved, so
    callers can distinguish "absent" from "present but null".
    """
    current = document
    text = (path or "").strip()
    if text.startswith("$"):
        text = text[1:]
    text = text.lstrip(".")
    if not text:
        return current
    for segment in re.findall(r"[^.\[\]]+|\[\d+\]", text):
        if segment.startswith("["):
            index = int(segment[1:-1])
            if not isinstance(current, list) or index >= len(current):
                return _MISSING
            current = current[index]
        else:
            if isinstance(current, dict):
                if segment not in current:
                    # Fall back to a case-insensitive match; C# DTOs serialize
                    # with varying casing depending on the endpoint.
                    match = next(
                        (key for key in current if key.lower() == segment.lower()), None
                    )
                    if match is None:
                        return _MISSING
                    segment = match
                current = current[segment]
            elif isinstance(current, list):
                # Project the field across a list, which is what an assertion
                # like `items.name` naturally means.
                projected = [
                    item[segment]
                    for item in current
                    if isinstance(item, dict) and segment in item
                ]
                if not projected:
                    return _MISSING
                current = projected
            else:
                return _MISSING
    return current


def _coerce(actual: Any, expected: str) -> bool:
    if isinstance(actual, bool):
        return str(actual).lower() == expected.strip().lower()
    if actual is None:
        return expected.strip().lower() in {"null", "none", ""}
    if isinstance(actual, (int, float)):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    if isinstance(actual, str):
        return actual == expected
    return json.dumps(actual, sort_keys=True) == expected.strip()


def _length_of(value: Any) -> int | None:
    if isinstance(value, (list, dict, str)):
        return len(value)
    return None


@dataclass(frozen=True)
class AssertionOutcome:
    assertion: Assertion
    passed: bool
    detail: str


#: Loose JSON-shape check per payload/response field ``kind``. Mirrors
#: ``api_tester.catalog_builder.PAYLOAD_KINDS`` — response-schema fields
#: produced by the catalog builder/generator use the same vocabulary as
#: payload/form schemas, since a response DTO is described the same way a
#: request DTO is.
_SCALAR_JSON_CHECK: dict[str, tuple[type, ...]] = {
    "text": (str,),
    "guid": (str,),
    "date": (str,),
    "json": (str, dict, list, int, float, bool, type(None)),
    "integer": (int, float),
    "number": (int, float),
    "boolean": (bool,),
}


def _schema_type_matches(kind: str, value: Any) -> bool:
    """True when ``value`` is a plausible JSON encoding of ``kind``.

    Loose on purpose: JSON has no separate integer/date/guid types, so this
    only rejects gross shape mismatches (a number where an object was
    declared, a list where a boolean was declared, ...), not precision or
    format details a regex would be needed for.
    """
    if kind in {"integer", "number"} and isinstance(value, bool):
        # bool is a subclass of int in Python; a JSON boolean is not a number.
        return False
    checkers = _SCALAR_JSON_CHECK.get(kind)
    if checkers is not None:
        return isinstance(value, checkers)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return True  # Unknown/other kinds (file, file_list, enum handled by caller) pass through.


def validate_response_schema(
    schema: dict[str, Any] | None, document: Any, path: str = ""
) -> list[str]:
    """Structurally validates ``document`` against a catalog response schema.

    ``schema`` is a payload-schema-shaped dict (``{"kind": "object"|"array",
    "fields": [...] }`` or ``{"item": {...}}`` for a bare array) as produced by
    the catalog builder/generator's payload/form/response schema stores.
    Returns a list of human-readable problems; an empty list means the
    document matches. Extra properties the schema does not describe are never
    flagged — this checks that the declared shape is honoured, not that
    nothing else was returned.
    """
    if schema is None:
        return ["no response schema is configured for this endpoint"]
    label = path or "$"
    kind = schema.get("kind")
    if kind == "array" or (kind is None and "item" in schema and "fields" not in schema):
        if not isinstance(document, list):
            return [f"{label}: expected an array, got {type(document).__name__}"]
        item_schema = schema.get("item") or {}
        problems: list[str] = []
        for index, element in enumerate(document[:20]):
            problems.extend(
                validate_response_schema(item_schema, element, f"{label}[{index}]")
            )
        return problems
    fields = schema.get("fields")
    if not fields:
        return []
    if not isinstance(document, dict):
        return [f"{label}: expected an object, got {type(document).__name__}"]
    problems = []
    for entry in fields:
        name = entry.get("name")
        if not name:
            continue
        field_path = f"{label}.{name}" if label != "$" else name
        if name not in document:
            if entry.get("required") and not entry.get("nullable"):
                problems.append(f"{field_path}: required field is missing")
            continue
        value = document[name]
        if value is None:
            if entry.get("required") and not entry.get("nullable"):
                problems.append(f"{field_path}: required field is null")
            continue
        field_kind = entry.get("kind", "text")
        if field_kind == "enum":
            values = entry.get("lov") or []
            if values and str(value) not in {str(item) for item in values}:
                problems.append(
                    f"{field_path}: value {value!r} is not one of the declared enum values"
                )
        elif field_kind == "array":
            if not isinstance(value, list):
                problems.append(f"{field_path}: expected an array, got {type(value).__name__}")
            else:
                item_schema = entry.get("item") or {}
                for index, element in enumerate(value[:20]):
                    problems.extend(
                        validate_response_schema(item_schema, element, f"{field_path}[{index}]")
                    )
        elif field_kind == "object":
            if not isinstance(value, dict):
                problems.append(f"{field_path}: expected an object, got {type(value).__name__}")
            else:
                problems.extend(
                    validate_response_schema(
                        {"kind": "object", "fields": entry.get("fields") or []},
                        value,
                        field_path,
                    )
                )
        elif not _schema_type_matches(field_kind, value):
            problems.append(
                f"{field_path}: expected {field_kind}, got {type(value).__name__} ({value!r})"
            )
    return problems


def evaluate_assertion(
    assertion: Assertion,
    status_code: int,
    headers: dict[str, str],
    body: str,
    document: Any,
    elapsed_ms: int,
    response_schema: dict[str, Any] | None = None,
) -> AssertionOutcome:
    kind = assertion.kind
    expected = assertion.value

    def outcome(passed: bool, detail: str) -> AssertionOutcome:
        return AssertionOutcome(assertion=assertion, passed=passed, detail=detail)

    if kind == "body_contains":
        return outcome(expected in body, f"body {'contains' if expected in body else 'is missing'} {expected!r}")
    if kind == "body_not_contains":
        return outcome(expected not in body, f"body contains {expected!r}" if expected in body else "not present")
    if kind == "body_matches":
        try:
            matched = re.search(expected, body) is not None
        except re.error as exc:
            return outcome(False, f"invalid regex: {exc}")
        return outcome(matched, "matched" if matched else "no match")
    if kind in {"header_equals", "header_exists"}:
        lookup = {key.lower(): value for key, value in headers.items()}
        actual = lookup.get(assertion.path.strip().lower())
        if kind == "header_exists":
            return outcome(actual is not None, "present" if actual is not None else "header absent")
        return outcome(actual == expected, f"header={actual!r}")
    if kind == "max_duration_ms":
        try:
            limit = float(expected)
        except ValueError:
            return outcome(False, f"invalid duration: {expected!r}")
        return outcome(elapsed_ms <= limit, f"{elapsed_ms} ms vs limit {limit:g} ms")

    if document is _MISSING or (document is None and body.strip()):
        return outcome(False, "response body is not valid JSON")

    if kind == "response_schema":
        problems = validate_response_schema(response_schema, document)
        if not problems:
            return outcome(True, "matches the declared response schema")
        shown = "; ".join(problems[:5])
        if len(problems) > 5:
            shown += f"; and {len(problems) - 5} more"
        return outcome(False, shown)

    if kind == "baseline":
        return outcome(False, "structural baseline comparison is evaluated by the runner")

    actual = resolve_path(document, assertion.path)
    if kind == "json_exists":
        return outcome(actual is not _MISSING, "found" if actual is not _MISSING else "path not found")
    if kind == "json_absent":
        return outcome(actual is _MISSING, "absent" if actual is _MISSING else f"found {actual!r}")
    if actual is _MISSING:
        return outcome(False, f"path {assertion.path!r} not found")
    if kind == "json_equals":
        return outcome(_coerce(actual, expected), f"actual={actual!r}")
    if kind == "json_not_equals":
        return outcome(not _coerce(actual, expected), f"actual={actual!r}")
    if kind == "json_contains":
        if isinstance(actual, list):
            return outcome(
                any(_coerce(item, expected) for item in actual), f"actual={actual!r}"
            )
        return outcome(expected in str(actual), f"actual={actual!r}")
    if kind == "json_not_empty":
        empty = actual in (None, "", [], {})
        return outcome(not empty, f"actual={actual!r}")
    if kind in {"json_length_eq", "json_length_gte", "json_length_lte"}:
        length = _length_of(actual)
        if length is None:
            return outcome(False, f"{type(actual).__name__} has no length")
        try:
            limit = int(float(expected))
        except ValueError:
            return outcome(False, f"invalid length: {expected!r}")
        comparison = {
            "json_length_eq": length == limit,
            "json_length_gte": length >= limit,
            "json_length_lte": length <= limit,
        }[kind]
        return outcome(comparison, f"length={length}")
    if kind == "json_type":
        name = JSON_TYPE_NAMES.get(type(actual), "unknown")
        return outcome(name == expected.strip().lower(), f"type={name}")
    return outcome(False, f"unknown assertion kind {kind!r}")


def apply_variables(value: Any, variables: dict[str, str]) -> Any:
    """Substitutes ``{{name}}`` placeholders throughout strings, dicts and lists."""
    if isinstance(value, str):
        def substitute(match: re.Match[str]) -> str:
            return str(variables.get(match.group(1), match.group(0)))

        return _VARIABLE_PATTERN.sub(substitute, value)
    if isinstance(value, dict):
        return {key: apply_variables(item, variables) for key, item in value.items()}
    if isinstance(value, list):
        return [apply_variables(item, variables) for item in value]
    return value


def extract_capture(
    capture: Capture, status_code: int, headers: dict[str, str], document: Any
) -> str | None:
    if not capture.name:
        return None
    if capture.source == "status":
        return str(status_code)
    if capture.source == "header":
        lookup = {key.lower(): value for key, value in headers.items()}
        return lookup.get(capture.path.strip().lower())
    if document is _MISSING or document is None:
        return None
    value = resolve_path(document, capture.path)
    if value is _MISSING:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def default_case_name(method: str, path: str, index: int) -> str:
    return f"{method} {path} #{index}"


def suggest_case_name(method: str, path: str, existing: Iterable[str]) -> str:
    """A readable, unique-within-the-suite default name for a new case.

    Returns ``GET /api/brands``, then ``GET /api/brands (2)``, ``(3)`` and so on,
    so adding the same endpoint repeatedly never produces indistinguishable cases.
    """
    base = f"{method.upper()} {path}".strip()
    taken = {name.strip() for name in existing if name and name.strip()}
    if base not in taken:
        return base
    index = 2
    while f"{base} ({index})" in taken:
        index += 1
    return f"{base} ({index})"


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def clone_case(case: TestCase) -> TestCase:
    return replace(
        case,
        id=uuid4().hex[:12],
        name=f"{case.name} (copy)" if case.name else "",
        assertions=[replace(item) for item in case.assertions],
        captures=[replace(item) for item in case.captures],
        baseline=BaselineConfig.from_dict(case.baseline.to_dict()),
        values=dict(case.values),
        depends_on=list(case.depends_on),
        payload=json.loads(json.dumps(case.payload)) if case.payload is not None else None,
    )
