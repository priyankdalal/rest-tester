"""The "suite" plan: a multi-case test suite described as structured JSON.

Each case is a request plan plus suite wiring (assertions, captures,
dependencies, phase). Cases refer to each other by a short ``key`` the model
chooses; the converter maps keys to real :class:`TestCase` ids. Every case is
checked with the single-request validator, then the suite rules (keys,
dependency graph, variables, assertions, captures) are checked on top.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from ..suite import ASSERTION_KINDS, JSON_TYPE_NAMES, Assertion, Capture, TestCase, TestSuite
from .catalog_index import CatalogIndex, is_generic_schema
from .request_plan import (
    READ_METHODS,
    STATUSES,
    FilterSpec,
    RequestPlan,
    SortSpec,
    _list,
    _text,
    request_plan_schema,
    to_explorer_request,
)
from .validation import VARIABLE, PlanIssue, _options, validate_request_plan
PHASES = ("normal", "cleanup")
CAPTURE_SOURCES = ("body", "header", "status")
_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")
_KEY_CLEAN = re.compile(r"[^A-Za-z0-9_\-]+")
_JSON_KINDS = {kind for kind, meta in ASSERTION_KINDS.items() if kind.startswith("json_") and meta["path"]}
_TYPE_NAMES = sorted(set(JSON_TYPE_NAMES.values()))
#: Kinds models reach for that the suite expresses differently.
_KIND_HINTS = {
    "status": "the status code is checked by expected_status, not an assertion",
    "status_code": "the status code is checked by expected_status, not an assertion",
    "status_equals": "the status code is checked by expected_status, not an assertion",
}


@dataclass(frozen=True)
class AssertionSpec:
    kind: str
    path: str = ""
    value: str = ""


@dataclass(frozen=True)
class CaptureSpec:
    name: str
    path: str = ""
    source: str = "body"


@dataclass(frozen=True)
class SuiteCasePlan:
    key: str
    name: str = ""
    endpoint_id: str = ""
    parameters: tuple[tuple[str, str], ...] = ()
    filters: tuple[FilterSpec, ...] = ()
    filter_join: str = "and"
    sort: tuple[SortSpec, ...] = ()
    payload: Any = None
    expected_status: str = ""
    assertions: tuple[AssertionSpec, ...] = ()
    captures: tuple[CaptureSpec, ...] = ()
    depends_on: tuple[str, ...] = ()
    phase: str = "normal"
    always_run: bool = False

    def request_plan(self) -> RequestPlan:
        return RequestPlan(
            title=self.name,
            endpoint_id=self.endpoint_id,
            parameters=self.parameters,
            filters=self.filters,
            filter_join=self.filter_join,
            sort=self.sort,
            payload=self.payload,
            expected_status=self.expected_status,
        )

    @classmethod
    def from_json(cls, value: dict[str, Any], position: int) -> "SuiteCasePlan":
        request = RequestPlan.from_json(value)
        phase = _text(value.get("phase")).lower()
        return cls(
            key=_KEY_CLEAN.sub("_", _text(value.get("key"))).strip("_") or f"case{position + 1}",
            name=_text(value.get("name")),
            endpoint_id=request.endpoint_id,
            parameters=request.parameters,
            filters=request.filters,
            filter_join=request.filter_join,
            sort=request.sort,
            payload=request.payload,
            expected_status=request.expected_status,
            assertions=tuple(
                AssertionSpec(_text(item.get("kind")).lower(), _text(item.get("path")), _text(item.get("value")))
                for item in _list(value.get("assertions"))
                if isinstance(item, dict) and _text(item.get("kind"))
            ),
            captures=tuple(
                CaptureSpec(
                    _text(item.get("name")),
                    _text(item.get("path")),
                    _text(item.get("source")).lower() or "body",
                )
                for item in _list(value.get("captures"))
                if isinstance(item, dict) and _text(item.get("name"))
            ),
            depends_on=tuple(
                dict.fromkeys(_KEY_CLEAN.sub("_", _text(item)).strip("_") for item in _list(value.get("depends_on")) if _text(item))
            ),
            phase=phase if phase in PHASES else "normal",
            always_run=bool(value.get("always_run")),
        )

    def to_json(self) -> dict[str, Any]:
        value = self.request_plan().to_json()
        for key in ("status", "title", "summary", "assumptions", "warnings", "question", "choices"):
            value.pop(key, None)
        value.update(
            key=self.key,
            name=self.name,
            assertions=[{"kind": item.kind, "path": item.path, "value": item.value} for item in self.assertions],
            captures=[{"name": item.name, "path": item.path, "source": item.source} for item in self.captures],
            depends_on=list(self.depends_on),
            phase=self.phase,
            always_run=self.always_run,
        )
        return value


@dataclass(frozen=True)
class SuitePlan:
    status: str = "plan"
    name: str = ""
    description: str = ""
    variables: tuple[tuple[str, str], ...] = ()
    stop_on_failure: bool = False
    cases: tuple[SuiteCasePlan, ...] = ()
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    question: str = ""
    choices: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: Any) -> "SuitePlan":
        if not isinstance(value, dict):
            raise ValueError("The suite plan must be a JSON object.")
        status = _text(value.get("status") or "plan").lower()
        raw_variables = value.get("variables")
        if isinstance(raw_variables, dict):
            variables = tuple((_text(name), _text(item)) for name, item in raw_variables.items() if _text(name))
        else:
            variables = tuple(
                (_text(item.get("name")), _text(item.get("value")))
                for item in _list(raw_variables)
                if isinstance(item, dict) and _text(item.get("name"))
            )
        return cls(
            status=status if status in STATUSES else "plan",
            name=_text(value.get("name")),
            description=_text(value.get("description")),
            variables=variables,
            stop_on_failure=bool(value.get("stop_on_failure")),
            cases=tuple(
                SuiteCasePlan.from_json(item, position)
                for position, item in enumerate(_list(value.get("cases")))
                if isinstance(item, dict)
            ),
            assumptions=tuple(_text(item) for item in _list(value.get("assumptions")) if _text(item)),
            warnings=tuple(_text(item) for item in _list(value.get("warnings")) if _text(item)),
            question=_text(value.get("question")),
            choices=tuple(_text(item) for item in _list(value.get("choices")) if _text(item)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "name": self.name,
            "description": self.description,
            "variables": [{"name": name, "value": value} for name, value in self.variables],
            "stop_on_failure": self.stop_on_failure,
            "cases": [case.to_json() for case in self.cases],
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
            "question": self.question,
            "choices": list(self.choices),
        }

    def with_changes(self, **changes: Any) -> "SuitePlan":
        return replace(self, **changes)


def suite_plan_schema(candidate_ids: list[str]) -> dict[str, Any]:
    request = request_plan_schema(candidate_ids)["properties"]
    string_list = {"type": "array", "items": {"type": "string"}}
    case_properties: dict[str, Any] = {
        "key": {"type": "string"},
        "name": {"type": "string"},
        "endpoint_id": {"type": "string", "enum": list(candidate_ids)},
        "parameters": request["parameters"],
        "filters": request["filters"],
        "filter_join": request["filter_join"],
        "sort": request["sort"],
        "payload": request["payload"],
        "expected_status": {"type": "string"},
        "assertions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(ASSERTION_KINDS)},
                    "path": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["kind", "path", "value"],
            },
        },
        "captures": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "path": {"type": "string"},
                    "source": {"type": "string", "enum": list(CAPTURE_SOURCES)},
                },
                "required": ["name", "path", "source"],
            },
        },
        "depends_on": string_list,
        "phase": {"type": "string", "enum": list(PHASES)},
        "always_run": {"type": "boolean"},
    }
    properties: dict[str, Any] = {
        "status": {"type": "string", "enum": list(STATUSES)},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "variables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "value": {"type": "string"}},
                "required": ["name", "value"],
            },
        },
        "stop_on_failure": {"type": "boolean"},
        "cases": {
            "type": "array",
            "items": {"type": "object", "properties": case_properties, "required": list(case_properties)},
        },
        "assumptions": string_list,
        "warnings": string_list,
        "question": {"type": "string"},
        "choices": string_list,
    }
    return {"type": "object", "properties": properties, "required": list(properties)}


# -- validation ----------------------------------------------------------------


@dataclass(frozen=True)
class SuiteValidation:
    plan: SuitePlan
    issues: tuple[PlanIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


def _variables_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(VARIABLE.findall(value))
    if isinstance(value, dict):
        return set().union(*(_variables_in(item) for item in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_variables_in(item) for item in value)) if value else set()
    return set()


def _case_variables(case: SuiteCasePlan) -> set[str]:
    used = _variables_in([value for _name, value in case.parameters])
    used |= _variables_in([list(item.values) for item in case.filters])
    used |= _variables_in(case.payload)
    return used


def _is_success(expected: str) -> bool:
    return not expected or expected.strip().startswith("2")


def response_path_problem(schema: dict[str, Any] | None, path: str) -> str | None:
    """Why ``path`` cannot exist in ``schema``; ``None`` when it can (or the shape is unknown)."""

    if not schema:
        return None
    text = (path or "").strip()
    text = text[1:] if text.startswith("$") else text
    segments = re.findall(r"[^.\[\]]+|\[\d+\]", text.lstrip("."))
    # Shared generic wrappers are only reliable at their top level.
    depth_limit = 1 if is_generic_schema(schema) else len(segments)
    node: dict[str, Any] | None = schema
    checked = 0
    for segment in segments:
        if node is None or checked >= depth_limit:
            return None
        kind = node.get("kind")
        if segment.startswith("["):
            if kind != "array":
                return f"'{segment}' indexes a {kind or 'value'}, not an array"
            node = node.get("item")
            continue
        if kind == "array":
            node = node.get("item")
            if node is None:
                return None
        fields = node.get("fields")
        if not fields:
            return None
        match = next((item for item in fields if str(item.get("name", "")).lower() == segment.lower()), None)
        if match is None:
            return f"the response has no field '{segment}' here. Valid: {_options(item.get('name') for item in fields)}"
        node = match
        checked += 1
    return None


def validate_suite_plan(plan: SuitePlan, index: CatalogIndex) -> SuiteValidation:
    if plan.status != "plan":
        if plan.status == "clarify" and not plan.question:
            return SuiteValidation(plan, (PlanIssue("question", "missing_question", "status is clarify but question is empty"),))
        return SuiteValidation(plan, ())
    issues: list[PlanIssue] = []
    assumptions = list(plan.assumptions)
    warnings = list(plan.warnings)
    if not plan.cases:
        return SuiteValidation(plan, (PlanIssue("cases", "no_cases", "a suite plan needs at least one case"),))

    variable_names = {name for name, _value in plan.variables}
    for name, value in plan.variables:
        if not _NAME.match(name):
            issues.append(PlanIssue(f"variables.{name}", "invalid_variable", "variable names use letters, digits, _ . - only"))
        elif not value:
            warnings.append(f"Set suite variable '{name}' before running.")

    seen: set[str] = set()
    for position, case in enumerate(plan.cases):
        if case.key in seen:
            issues.append(PlanIssue(f"cases[{position}].key", "duplicate_key", f"key '{case.key}' is used twice; keys must be unique"))
        seen.add(case.key)
    by_key = {case.key: case for case in plan.cases}

    cases: list[SuiteCasePlan] = []
    writes: list[str] = []
    for position, case in enumerate(plan.cases):
        prefix = f"cases[{position}]"
        endpoint = index.get(case.endpoint_id)
        if endpoint is None:
            issues.append(PlanIssue(f"{prefix}.endpoint_id", "unknown_endpoint", f"'{case.endpoint_id}' is not a catalog endpoint; choose one of the candidate ids"))
            cases.append(case)
            continue
        result = validate_request_plan(case.request_plan(), index)
        issues.extend(PlanIssue(f"{prefix}.{item.path}", item.code, item.message) for item in result.issues)
        fixed = result.plan
        label = case.name or case.key
        assumptions.extend(f"{label}: {text}" for text in fixed.assumptions)
        for text in fixed.warnings:
            if text.startswith("Fill in path parameter"):
                continue
            if text.endswith("changes data on the server."):
                continue
            warnings.append(f"{label}: {text}")
        if endpoint.method.upper() not in READ_METHODS:
            writes.append(endpoint.method.upper())
        given = {name for name, value in fixed.parameters if value}
        for parameter in endpoint.parameters:
            if parameter.source == "path" and parameter.name not in given:
                issues.append(
                    PlanIssue(
                        f"{prefix}.parameters",
                        "missing_path_parameter",
                        f"path parameter '{parameter.name}' is empty; use a value the user gave or a variable such as"
                        " {{" + _guess_variable(endpoint.controller) + "}} captured by an earlier case",
                    )
                )

        expected = fixed.expected_status or endpoint.expected_status
        response_schema = index.catalog.response_schema(endpoint.response_schema)
        assertions: list[AssertionSpec] = []
        for number, item in enumerate(case.assertions):
            path = f"{prefix}.assertions[{number}]"
            meta = ASSERTION_KINDS.get(item.kind)
            if meta is None:
                hint = _KIND_HINTS.get(item.kind, f"Valid: {_options(ASSERTION_KINDS)}")
                issues.append(PlanIssue(f"{path}.kind", "unknown_assertion", f"'{item.kind}' is not an assertion kind; {hint}"))
                continue
            if item.kind == "baseline":
                assumptions.append(f"{label}: dropped the baseline assertion; record a baseline in Test Suites first.")
                continue
            if meta["path"] and not item.path:
                issues.append(PlanIssue(f"{path}.path", "missing_path", f"'{item.kind}' needs a path such as $.Id"))
                continue
            if meta["value"] and item.value == "" and item.kind not in {"json_equals", "json_not_equals"}:
                issues.append(PlanIssue(f"{path}.value", "missing_value", f"'{item.kind}' needs a value"))
                continue
            if VARIABLE.search(item.value):
                issues.append(
                    PlanIssue(
                        f"{path}.value",
                        "variable_in_assertion",
                        "assertion values are compared literally ({{variables}} are not substituted); use a literal"
                        " value, e.g. the same text you sent in the payload",
                    )
                )
                continue
            if item.kind in {"json_length_eq", "json_length_gte", "json_length_lte"} and not re.fullmatch(r"\d+", item.value.strip()):
                issues.append(PlanIssue(f"{path}.value", "invalid_value", f"'{item.kind}' needs a whole number, not '{item.value}'"))
                continue
            if item.kind == "max_duration_ms" and not re.fullmatch(r"\d+(\.\d+)?", item.value.strip()):
                issues.append(PlanIssue(f"{path}.value", "invalid_value", f"max_duration_ms needs milliseconds, not '{item.value}'"))
                continue
            if item.kind == "json_type" and item.value.strip().lower() not in _TYPE_NAMES:
                issues.append(PlanIssue(f"{path}.value", "invalid_value", f"json_type is one of {_options(_TYPE_NAMES)}"))
                continue
            if item.kind == "response_schema" and not _is_success(expected):
                issues.append(PlanIssue(f"{path}.kind", "schema_on_error", "response_schema only applies to successful (2xx) responses"))
                continue
            if item.kind in _JSON_KINDS and item.kind != "json_absent" and _is_success(expected):
                problem = response_path_problem(response_schema, item.path)
                if problem:
                    issues.append(PlanIssue(f"{path}.path", "unknown_response_path", problem))
                    continue
            assertions.append(
                AssertionSpec(item.kind, item.path if meta["path"] else "", item.value if meta["value"] else "")
            )

        captures: list[CaptureSpec] = []
        for number, item in enumerate(case.captures):
            path = f"{prefix}.captures[{number}]"
            source = item.source if item.source in CAPTURE_SOURCES else "body"
            if not _NAME.match(item.name):
                issues.append(PlanIssue(f"{path}.name", "invalid_variable", f"'{item.name}' is not a valid variable name (letters, digits, _ . -)"))
                continue
            if source != "status" and not item.path:
                issues.append(PlanIssue(f"{path}.path", "missing_path", "a body capture needs a JSON path such as $.Id; a header capture needs the header name"))
                continue
            if source == "body":
                problem = response_path_problem(response_schema, item.path)
                if problem:
                    issues.append(PlanIssue(f"{path}.path", "unknown_response_path", problem))
                    continue
            captures.append(CaptureSpec(item.name, item.path if source != "status" else "", source))

        cases.append(
            replace(
                case,
                parameters=fixed.parameters,
                filters=fixed.filters,
                sort=fixed.sort,
                payload=fixed.payload,
                expected_status=fixed.expected_status,
                assertions=tuple(assertions),
                captures=tuple(captures),
                always_run=case.always_run or case.phase == "cleanup",
            )
        )

    # -- dependencies ----------------------------------------------------------
    for position, case in enumerate(cases):
        for key in case.depends_on:
            if key == case.key:
                issues.append(PlanIssue(f"cases[{position}].depends_on", "self_dependency", "a case cannot depend on itself"))
            elif key not in by_key:
                issues.append(PlanIssue(f"cases[{position}].depends_on", "unknown_dependency", f"no case has key '{key}'. Keys: {_options(by_key)}"))
            elif case.phase == "normal" and by_key[key].phase == "cleanup":
                issues.append(PlanIssue(f"cases[{position}].depends_on", "depends_on_cleanup", f"a normal case cannot depend on cleanup case '{key}'"))

    # -- variables: captured by a dependency or defined on the suite ----------
    captured_by: dict[str, str] = {}
    for case in cases:
        for capture in case.captures:
            captured_by.setdefault(capture.name, case.key)
    graph = {case.key: list(dict.fromkeys(item for item in case.depends_on if item in by_key and item != case.key)) for case in cases}
    for position, case in enumerate(cases):
        for name in sorted(_case_variables(case)):
            if name in variable_names:
                continue
            owner = captured_by.get(name)
            if owner is None:
                issues.append(
                    PlanIssue(
                        f"cases[{position}]",
                        "undefined_variable",
                        f"'{{{{{name}}}}}' is never captured or defined; capture it in an earlier case or add it to"
                        " variables",
                    )
                )
                continue
            if owner == case.key:
                issues.append(PlanIssue(f"cases[{position}]", "own_capture", f"'{{{{{name}}}}}' is captured by this same case, after its request is sent"))
                continue
            if owner in _ancestors(case.key, graph):
                continue
            owner_case = by_key[owner]
            if case.phase == "normal" and owner_case.phase == "cleanup":
                issues.append(PlanIssue(f"cases[{position}]", "depends_on_cleanup", f"'{{{{{name}}}}}' comes from cleanup case '{owner}'"))
                continue
            if case.key in _ancestors(owner, graph):
                issues.append(PlanIssue(f"cases[{position}]", "dependency_cycle", f"'{{{{{name}}}}}' comes from '{owner}', which runs after this case"))
                continue
            graph[case.key].append(owner)
            assumptions.append(f"{case.name or case.key}: runs after '{owner}', which captures {{{{{name}}}}}.")
    for position, case in enumerate(cases):
        if case.key in _ancestors(case.key, graph):
            issues.append(PlanIssue(f"cases[{position}].depends_on", "dependency_cycle", f"'{case.key}' depends on itself through other cases"))
            break
    cases = [replace(case, depends_on=tuple(graph.get(case.key, case.depends_on))) for case in cases]

    if writes:
        counts = ", ".join(f"{writes.count(method)} {method}" for method in dict.fromkeys(writes))
        warnings.append(f"This suite changes data on the server ({counts}).")
    if any(isinstance(case.payload, dict) and any(VARIABLE.fullmatch(str(value).strip()) for value in case.payload.values()) for case in cases):
        warnings.append("Payload fields holding a {{variable}} are sent as text; check the API accepts that.")
    fixed_plan = plan.with_changes(
        cases=tuple(cases),
        assumptions=tuple(dict.fromkeys(assumptions)),
        warnings=tuple(dict.fromkeys(warnings)),
    )
    return SuiteValidation(fixed_plan, tuple(issues))


def _ancestors(key: str, graph: dict[str, list[str]]) -> set[str]:
    found: set[str] = set()
    stack = list(graph.get(key, ()))
    while stack:
        item = stack.pop()
        if item in found:
            continue
        found.add(item)
        stack.extend(graph.get(item, ()))
    return found


def _guess_variable(controller: str) -> str:
    name = controller[:1].lower() + controller[1:] if controller else "item"
    return f"{name}Id"


# -- conversion ------------------------------------------------------------------


def to_test_suite(plan: SuitePlan, index: CatalogIndex) -> TestSuite:
    """Converts a validated plan into a runnable :class:`TestSuite`."""

    ids = {case.key: TestCase(endpoint_id=case.endpoint_id).id for case in plan.cases}
    cases: list[TestCase] = []
    for case in plan.cases:
        endpoint = index.get(case.endpoint_id)
        if endpoint is None:
            raise ValueError(f"Case '{case.key}' uses an endpoint that is not in the catalog.")
        draft = to_explorer_request(case.request_plan(), endpoint, index.filter_schema(endpoint) is not None)
        cases.append(
            TestCase(
                endpoint_id=endpoint.id,
                id=ids[case.key],
                name=case.name or f"{endpoint.method} {endpoint.path}",
                values=draft.values,
                payload=draft.payload,
                expected_status=draft.expected_status,
                assertions=[Assertion(item.kind, item.path, item.value) for item in case.assertions],
                captures=[Capture(item.name, item.path, item.source) for item in case.captures],
                depends_on=[ids[key] for key in case.depends_on if key in ids],
                phase=case.phase,
                always_run=case.always_run,
            )
        )
    suite = TestSuite(
        name=plan.name or "AI suite",
        description=plan.description,
        variables={name: value for name, value in plan.variables},
        cases=cases,
        stop_on_failure=plan.stop_on_failure,
    )
    suite.validate_dependencies()
    return suite
