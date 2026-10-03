"""AI planning for Data Runner and Load Testing workflows.

The planner produces editor drafts only. It never reads CSV rows, opens files,
sets load-test consent, or executes requests.
"""

from __future__ import annotations

import html
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..catalog import Endpoint
from ..data_runner.mapping import (
    TRANSFORM_KINDS,
    ColumnMapping,
    RowMapper,
    Transform,
    mapping_targets,
)
from ..load_testing.scenario import (
    MVP_STAGE_KINDS,
    THRESHOLD_METRICS,
    THRESHOLD_OPERATORS,
    LoadStage,
    ThresholdDefinition,
)
from .catalog_index import FILTER_PARAMETER, SORT_PARAMETER, CatalogIndex
from .planner import PlanOutcome, RequestPlanner
from .prompts import REQUEST_SYSTEM
from .request_plan import ExplorerRequest, RequestPlan, request_plan_schema, to_explorer_request
from .validation import PlanIssue, ValidationResult, has_variable, validate_request_plan

WORKFLOW_PROMPT_VERSION = "workflow_plan v1"
_CSV_PATH_PLACEHOLDER = "{{csv.column}}"
_MAX_LOAD_USERS = 100_000
_MAX_STAGE_SECONDS = 86_400
_MAX_THINK_TIME_MS = 600_000
_MAX_THRESHOLD_TARGET = 1_000_000
_MAX_TOTAL_SECONDS = 86_400

_SENSITIVE_NAME = re.compile(
    r"(?:^|[._-])(?:authorization|auth|token|access[_-]?token|refresh[_-]?token|"
    r"password|passwd|secret|api[_-]?key|client[_-]?secret|credential|cookie)(?:$|[._-])",
    re.IGNORECASE,
)
_FILE_PATH_NAME = re.compile(
    r"(?:^|[._-])(?:file|filename|filepath|file_path|directory|folder)(?:$|[._-])",
    re.IGNORECASE,
)
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_OPTION_KEYS = {
    "default": frozenset({"default"}),
    "env_fallback": frozenset({"variable"}),
    "date_format": frozenset({"format"}),
    "split_list": frozenset({"delimiter"}),
    "prefix_suffix": frozenset({"prefix", "suffix"}),
}
_OPTION_KEYS_DEFAULT_EMPTY = frozenset()

_DATA_INSTRUCTIONS = """\
Plan a Data Runner workflow for exactly one catalog endpoint.
Available CSV column names (names only; no row values or file paths are available):
{columns}
Return catalog-valid mappings using these destination forms: path:<parameter>, query:<parameter>,
form:<parameter>, payload:<dotted.field> (or payload: for a whole JSON body), filter:<field>:<supported-op>,
sort:<field>, expected_status, correlation_key, or skip_row. Choose only columns above and destinations
shown on the selected endpoint card. Do not map headers, credentials, secrets, tokens, or filesystem paths.
Transforms must use only the closed transform kinds in the schema and their existing supported options.
The API request's parameters and payload are constants, not CSV variables. Do not put {{...}} placeholders in
them; CSV columns belong only in mappings. Missing required payload fields may be supplied by payload mappings.
If a path parameter is mapped, leave its request constant empty. Do not invent values or ask the user for that
mapped path value.
If the user asks for multiple endpoints or behavior that cannot be represented, use clarify or unsupported;
do not imply that a CSV workflow will execute during planning.
"""

_LOAD_INSTRUCTIONS = """\
Plan one Load Testing Studio scenario for exactly ONE catalog endpoint and the closed
closed_virtual_users workload model. Use one or more stages from warm_up, ramp_up, steady, ramp_down only.
Do not encode open-rate/arrival-rate workloads, spike/soak/step stages, or multiple targets; use clarify
(or unsupported when the requested behavior cannot be offered) instead.
Stage users are integers from 0 to 100000; duration_seconds must be finite, from 0.1 to 86400, and have
at most one decimal place; think_time_ms is an integer from 0 to 600000.
Threshold target values are finite, from -1000000 to 1000000, and have at most three decimal places.
error_rate targets are fractions in [0, 1] (for example 0.01 means 1 percent), never percentage numbers.
Use only supported threshold metrics/operators. No string-to-number conversions.
Do not include CSV mappings or unresolved {{...}} variables in request constants.
Planning never grants environment permission, confirms write safety, or constitutes user consent. The native
editor must obtain those separately before execution.
"""


@dataclass(frozen=True)
class WorkflowPlan(RequestPlan):
    """One request draft plus the optional Data Runner or load-test definition."""

    mode: str = "data"
    mappings: tuple[ColumnMapping, ...] = ()
    stages: tuple[LoadStage, ...] = ()
    thresholds: tuple[ThresholdDefinition, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"data", "load"}:
            raise ValueError("Workflow mode must be 'data' or 'load'.")
        object.__setattr__(self, "mappings", tuple(self.mappings))
        object.__setattr__(self, "stages", tuple(self.stages))
        object.__setattr__(self, "thresholds", tuple(self.thresholds))

    def to_json(self) -> dict[str, Any]:
        value = super().to_json()
        value.update(
            {
                "mode": self.mode,
                "mappings": [
                    {
                        "column": mapping.column,
                        "target_key": mapping.target_key,
                        "transforms": [
                            {"kind": transform.kind, "options": dict(transform.options)}
                            for transform in mapping.transforms
                        ],
                    }
                    for mapping in self.mappings
                ],
                "stages": [stage.to_dict() for stage in self.stages],
                "thresholds": [threshold.to_dict() for threshold in self.thresholds],
            }
        )
        return value


class WorkflowPlanner(RequestPlanner):
    """Catalog-grounded planner for Data Runner or Load Testing."""

    def __init__(
        self,
        *base_args: Any,
        mode: str,
        columns: tuple[str, ...] = (),
        **base_kwargs: Any,
    ) -> None:
        if mode not in {"data", "load"}:
            raise ValueError("Workflow mode must be 'data' or 'load'.")
        if not isinstance(columns, tuple) or any(not isinstance(item, str) or not item.strip() for item in columns):
            raise ValueError("CSV columns must be a tuple of non-empty column names.")
        if len(set(columns)) != len(columns):
            raise ValueError("CSV column names must be unique to avoid ambiguous mappings.")
        super().__init__(*base_args, **base_kwargs)
        self.mode = mode
        self.columns = columns

    def system_prompt(self) -> str:
        if self.mode == "data":
            return REQUEST_SYSTEM + "\n\n" + _DATA_INSTRUCTIONS.replace(
                "{columns}", json.dumps(self.columns, ensure_ascii=False)
            )
        return REQUEST_SYSTEM + "\n\n" + _LOAD_INSTRUCTIONS

    def plan(self, prompt: str) -> PlanOutcome:
        outcome = super().plan(prompt)
        outcome.prompt_version = WORKFLOW_PROMPT_VERSION
        return outcome

    def output_token_limit(self) -> int:
        return 4000

    def response_schema(self, candidate_ids: list[str]) -> dict[str, Any]:
        schema = request_plan_schema(candidate_ids)
        properties = schema["properties"]
        properties.update(
            {
                "mode": {"type": "string", "enum": [self.mode]},
                "mappings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "target_key": {"type": "string"},
                            "transforms": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"type": "string", "enum": list(TRANSFORM_KINDS)},
                                        "options": {
                                            "type": "object",
                                            "properties": {
                                                "default": {},
                                                "variable": {"type": "string"},
                                                "format": {"type": "string"},
                                                "delimiter": {"type": "string"},
                                                "prefix": {"type": "string"},
                                                "suffix": {"type": "string"},
                                            },
                                            "additionalProperties": False,
                                        },
                                    },
                                    "required": ["kind", "options"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["column", "target_key", "transforms"],
                        "additionalProperties": False,
                    },
                },
                "stages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"type": "string", "enum": list(MVP_STAGE_KINDS)},
                            "duration_seconds": {"type": "number"},
                            "start_users": {"type": "integer"},
                            "end_users": {"type": "integer"},
                            "think_time_ms": {"type": "integer"},
                            "label": {"type": "string"},
                        },
                        "required": [
                            "kind",
                            "duration_seconds",
                            "start_users",
                            "end_users",
                            "think_time_ms",
                            "label",
                        ],
                        "additionalProperties": False,
                    },
                },
                "thresholds": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "metric": {"type": "string", "enum": list(THRESHOLD_METRICS)},
                            "operator": {"type": "string", "enum": list(THRESHOLD_OPERATORS)},
                            "target": {"type": "number"},
                            "label": {"type": "string"},
                        },
                        "required": ["metric", "operator", "target", "label"],
                        "additionalProperties": False,
                    },
                },
            }
        )
        schema["required"] = [*schema["required"], "mode", "mappings", "stages", "thresholds"]
        schema["additionalProperties"] = False
        return schema

    def parse_plan(self, value: Any) -> WorkflowPlan:
        if not isinstance(value, dict):
            raise ValueError("The workflow plan must be a JSON object.")
        workflow_keys = {"mode", "mappings", "stages", "thresholds"}
        allowed_keys = set(request_plan_schema([])["properties"]) | workflow_keys
        _require_allowed_keys(value, workflow_keys, allowed_keys, "workflow")
        mode = value["mode"]
        if not isinstance(mode, str) or mode != self.mode:
            raise ValueError(f"The workflow mode must be {self.mode!r}.")
        mappings_value = value["mappings"]
        stages_value = value["stages"]
        thresholds_value = value["thresholds"]
        if not isinstance(mappings_value, list):
            raise ValueError("'mappings' must be an array.")
        if not isinstance(stages_value, list):
            raise ValueError("'stages' must be an array.")
        if not isinstance(thresholds_value, list):
            raise ValueError("'thresholds' must be an array.")

        mappings = tuple(_parse_mapping(item, position) for position, item in enumerate(mappings_value))
        stages = tuple(_parse_stage(item, position) for position, item in enumerate(stages_value))
        thresholds = tuple(
            _parse_threshold(item, position) for position, item in enumerate(thresholds_value)
        )
        request = RequestPlan.from_json(value)
        _ensure_finite_json(request.payload, "payload")
        return WorkflowPlan(
            **request.__dict__,
            mode=mode,
            mappings=mappings,
            stages=stages,
            thresholds=thresholds,
        )

    def validate_plan(self, plan: RequestPlan) -> ValidationResult:
        if not isinstance(plan, WorkflowPlan) or plan.mode != self.mode:
            return ValidationResult(
                plan,
                (PlanIssue("mode", "invalid_mode", f"Expected a {self.mode!r} workflow plan."),),
            )
        return validate_workflow_plan(plan, self.index, self.columns)


def validate_workflow_plan(
    plan: WorkflowPlan,
    index: CatalogIndex,
    columns: tuple[str, ...] = (),
) -> ValidationResult:
    """Revalidate a persisted workflow draft against the current catalog.

    For Data Runner plans, pass the selected CSV's current column-name
    snapshot. No CSV rows or filenames are needed or inspected.
    """

    if not isinstance(plan, WorkflowPlan):
        return ValidationResult(
            plan,
            (PlanIssue("workflow", "invalid_plan", "Expected a WorkflowPlan instance."),),
        )
    if plan.status != "plan":
        issues = list(validate_request_plan(plan, index).issues)
        if plan.mappings or plan.stages or plan.thresholds:
            issues.append(
                PlanIssue(
                    "workflow",
                    "unexpected_sections",
                    "Clarification and unsupported plans must not include mappings, stages, or thresholds.",
                )
            )
        return ValidationResult(plan, tuple(issues))

    endpoint = index.get(plan.endpoint_id)
    if endpoint is None:
        return validate_request_plan(plan, index)

    issues: list[PlanIssue] = []
    if plan.mode == "data":
        if plan.stages or plan.thresholds:
            issues.append(
                PlanIssue(
                    "stages/thresholds",
                    "unexpected_sections",
                    "Data workflows cannot contain load stages or thresholds.",
                )
            )
        issues.extend(_validate_workflow_mappings(plan, index, columns, endpoint))
        prepared = _with_mapped_path_placeholders(plan)
    else:
        if plan.mappings:
            issues.append(
                PlanIssue("mappings", "unexpected_section", "Load workflows cannot contain CSV mappings.")
            )
        issues.extend(_validate_load_sections(plan))
        prepared = plan

    issues.extend(_unresolved_value_issues(prepared, allow_csv_path=plan.mode == "data"))
    issues.extend(_local_path_value_issues(prepared, endpoint))
    for name, parameter_value in prepared.parameters:
        parameter = next(
            (item for item in endpoint.parameters if item.name.casefold() == name.casefold()),
            None,
        )
        if parameter is not None and parameter.source == "header" and parameter_value:
            issues.append(
                PlanIssue(
                    f"parameters.{name}",
                    "header_value_forbidden",
                    "AI workflow plans cannot author header values because headers may contain credentials; configure them in the native editor.",
                )
            )
        if parameter is not None and parameter.source == "path" and _is_sensitive_name(parameter.name):
            issues.append(
                PlanIssue(
                    f"parameters.{name}",
                    "credential_target_forbidden",
                    "Credential, token, password, and secret path parameters cannot be AI-planned.",
                )
            )
    request_result = validate_request_plan(prepared, index)
    fixed = request_result.plan
    issues.extend(request_result.issues)
    if plan.mode == "data" and plan.mappings:
        fixed = fixed.with_changes(
            warnings=_remove_mapped_payload_warnings(fixed.warnings, plan.mappings)
        )
    if plan.mode == "load":
        safety_notice = (
            "AI planning does not grant environment permission, confirm write safety, or approve execution."
        )
        fixed = fixed.with_changes(warnings=tuple(dict.fromkeys((*fixed.warnings, safety_notice))))
    return ValidationResult(fixed, tuple(issues))


def _validate_workflow_mappings(
    plan: WorkflowPlan,
    index: CatalogIndex,
    columns: tuple[str, ...],
    endpoint: Endpoint,
) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    available = set(columns)
    targets = mapping_targets(endpoint, index.catalog)
    targets_by_key = {target.key: target for target in targets}
    structured_filters = index.filter_schema(endpoint) is not None
    destinations: set[str] = set()

    for position, mapping in enumerate(plan.mappings):
        path = f"mappings[{position}]"
        if mapping.column not in available:
            issues.append(
                PlanIssue(path + ".column", "unknown_column", f"{mapping.column!r} is not an available CSV column.")
            )
        if mapping.target_key.startswith("header:"):
            issues.append(
                PlanIssue(
                    path + ".target_key",
                    "header_mapping_forbidden",
                    "AI workflow plans cannot map header values because headers may contain credentials; configure them in the native editor.",
                )
            )
        query_name = mapping.target_key.partition(":")[2]
        if (
            structured_filters
            and mapping.target_key.startswith("query:")
            and query_name.casefold() in {FILTER_PARAMETER.casefold(), SORT_PARAMETER.casefold()}
        ):
            issues.append(
                PlanIssue(
                    path + ".target_key",
                    "structured_query_mapping_forbidden",
                    f"Use catalog-aware filter/sort mapping targets instead of raw query:{query_name} grammar.",
                )
            )

        target = targets_by_key.get(mapping.target_key)
        destination = mapping.target_key
        if mapping.target_key.startswith("filter:"):
            # RowMapper accepts the operator suffix as part of a filter key.
            # Different valid operators are distinct destinations.
            target_name = mapping.target_key[len("filter:") :].rpartition(":")[0]
        elif target is not None:
            target_name = target.key.split(":", 1)[-1]
        else:
            target_name = mapping.target_key

        if destination in destinations:
            issues.append(
                PlanIssue(
                    path + ".target_key",
                    "duplicate_target",
                    f"Destination {destination!r} is mapped more than once.",
                )
            )
        destinations.add(destination)

        if _is_sensitive_name(target_name):
            issues.append(
                PlanIssue(
                    path + ".target_key",
                    "credential_target_forbidden",
                    "Credential, token, password, and secret destinations cannot be AI-mapped.",
                )
            )
        if _is_file_path_name(target_name):
            issues.append(
                PlanIssue(
                    path + ".target_key",
                    "file_path_target_forbidden",
                    "AI workflow mappings cannot target filesystem path or directory fields.",
                )
            )
        for transform_index, transform in enumerate(mapping.transforms):
            for option_name, option_value in transform.options.items():
                if _contains_local_path(option_value):
                    issues.append(
                        PlanIssue(
                            f"{path}.transforms[{transform_index}].options.{option_name}",
                            "file_path_forbidden",
                            "Transform options cannot contain arbitrary filesystem paths.",
                        )
                    )

    try:
        RowMapper(endpoint, index.catalog, list(plan.mappings))
    except ValueError as exc:
        issues.append(PlanIssue("mappings", "invalid_mapping", str(exc)))
    return issues


def _require_allowed_keys(
    value: dict[str, Any],
    required: set[str],
    allowed: set[str],
    label: str,
) -> None:
    missing = required - value.keys()
    extra = value.keys() - allowed
    if missing:
        raise ValueError(f"{label} section is missing keys: {', '.join(sorted(missing))}.")
    if extra:
        raise ValueError(f"{label} section has unsupported keys: {', '.join(sorted(extra))}.")


def _parse_mapping(value: Any, position: int) -> ColumnMapping:
    path = f"mappings[{position}]"
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object.")
    _require_exact_keys(value, {"column", "target_key", "transforms"}, path)
    column = _strict_string(value["column"], f"{path}.column", strip=False)
    target_key = _strict_string(value["target_key"], f"{path}.target_key")
    transform_values = value["transforms"]
    if not isinstance(transform_values, list):
        raise ValueError(f"{path}.transforms must be an array.")
    transforms: list[Transform] = []
    for index, transform_value in enumerate(transform_values):
        transform_path = f"{path}.transforms[{index}]"
        if not isinstance(transform_value, dict):
            raise ValueError(f"{transform_path} must be an object.")
        _require_exact_keys(transform_value, {"kind", "options"}, transform_path)
        kind = _strict_string(transform_value["kind"], f"{transform_path}.kind")
        if kind not in TRANSFORM_KINDS:
            raise ValueError(f"{transform_path}.kind must be one of: {', '.join(TRANSFORM_KINDS)}.")
        options = transform_value["options"]
        if not isinstance(options, dict):
            raise ValueError(f"{transform_path}.options must be an object.")
        allowed_options = _OPTION_KEYS.get(kind, _OPTION_KEYS_DEFAULT_EMPTY)
        extra_options = options.keys() - allowed_options
        if extra_options:
            raise ValueError(
                f"{transform_path}.options has unsupported keys: {', '.join(sorted(extra_options))}."
            )
        _validate_transform_options(kind, options, transform_path)
        _ensure_finite_json(options, f"{transform_path}.options")
        transforms.append(Transform(kind, options))
    return ColumnMapping(column, target_key, tuple(transforms))


def _validate_transform_options(kind: str, options: dict[str, Any], path: str) -> None:
    string_options = {
        "env_fallback": ("variable",),
        "date_format": ("format",),
        "split_list": ("delimiter",),
        "prefix_suffix": ("prefix", "suffix"),
    }
    for name in string_options.get(kind, ()):
        if name in options and not isinstance(options[name], str):
            raise ValueError(f"{path}.options.{name} must be a string.")
    if kind == "split_list" and options.get("delimiter") == "":
        raise ValueError(f"{path}.options.delimiter must not be empty.")
    if kind == "date_format" and not options.get("format", "").strip():
        raise ValueError(f"{path}.options.format must be a non-empty date format.")
    if kind == "env_fallback" and not options.get("variable", "").strip():
        raise ValueError(f"{path}.options.variable must be a non-empty environment-variable name.")


def _parse_stage(value: Any, position: int) -> LoadStage:
    path = f"stages[{position}]"
    keys = {
        "kind",
        "duration_seconds",
        "start_users",
        "end_users",
        "think_time_ms",
        "label",
    }
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object.")
    extra = value.keys() - keys
    if extra != {"counts_toward_sla"} and extra:
        raise ValueError(f"{path} has unsupported keys: {', '.join(sorted(extra))}.")
    missing = keys - value.keys()
    if missing:
        raise ValueError(f"{path} is missing keys: {', '.join(sorted(missing))}.")
    if "counts_toward_sla" in value and value["counts_toward_sla"] is not None:
        raise ValueError(f"{path}.counts_toward_sla overrides are not supported by the native editor.")
    kind = _strict_string(value["kind"], f"{path}.kind")
    duration = _strict_number(value["duration_seconds"], f"{path}.duration_seconds")
    start_users = _strict_integer(value["start_users"], f"{path}.start_users")
    end_users = _strict_integer(value["end_users"], f"{path}.end_users")
    think_time = _strict_integer(value["think_time_ms"], f"{path}.think_time_ms")
    label = _strict_string(value["label"], f"{path}.label")
    try:
        return LoadStage(
            kind,
            duration,
            start_users,
            end_users,
            think_time,
            label=label,
        )
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _parse_threshold(value: Any, position: int) -> ThresholdDefinition:
    path = f"thresholds[{position}]"
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object.")
    _require_exact_keys(value, {"metric", "operator", "target", "label"}, path)
    metric = _strict_string(value["metric"], f"{path}.metric")
    operator = _strict_string(value["operator"], f"{path}.operator")
    target = _strict_number(value["target"], f"{path}.target")
    label = _strict_string(value["label"], f"{path}.label")
    try:
        return ThresholdDefinition(metric, operator, target, label)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _require_exact_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing:
        raise ValueError(f"{path} is missing keys: {', '.join(sorted(missing))}.")
    if extra:
        raise ValueError(f"{path} has unsupported keys: {', '.join(sorted(extra))}.")


def _strict_string(value: Any, path: str, *, strip: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a string.")
    return value.strip() if strip else value


def _strict_integer(value: Any, path: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{path} must be a JSON integer (not a boolean or string).")
    return value


def _strict_number(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a JSON number (not a boolean or string).")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{path} must be finite.") from exc
    if not math.isfinite(number):
        raise ValueError(f"{path} must be finite.")
    return number


def _ensure_finite_json(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity.")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_finite_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} object keys must be strings.")
            _ensure_finite_json(item, f"{path}.{key}")
        return
    raise ValueError(f"{path} contains a value that is not JSON-compatible.")


def _is_sensitive_name(name: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return _SENSITIVE_NAME.search(normalized) is not None


def _is_file_path_name(name: str) -> bool:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return _FILE_PATH_NAME.search(normalized) is not None


def _looks_like_local_path(value: str) -> bool:
    return bool(
        _WINDOWS_PATH.match(value)
        or value.startswith("/")
    )


def _contains_local_path(value: Any) -> bool:
    if isinstance(value, str):
        return _looks_like_local_path(value)
    if isinstance(value, dict):
        return any(_contains_local_path(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_local_path(item) for item in value)
    return False


def _has_decimal_places(value: float, places: int) -> bool:
    try:
        decimal = Decimal(str(value))
        return decimal.is_finite() and decimal == decimal.quantize(Decimal(1).scaleb(-places))
    except InvalidOperation:
        return False


def _validate_load_sections(plan: WorkflowPlan) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    if not plan.stages:
        issues.append(PlanIssue("stages", "missing_stages", "A load workflow needs at least one stage."))
    total_seconds = 0.0
    peak_users = max((stage.peak_users() for stage in plan.stages), default=0)
    for index, stage in enumerate(plan.stages):
        path = f"stages[{index}]"
        if stage.counts_toward_sla is not None:
            issues.append(
                PlanIssue(
                    f"{path}.counts_toward_sla",
                    "unsupported_sla_override",
                    "SLA stage overrides are not supported by the native editor; use the stage defaults.",
                )
            )
        if stage.kind not in MVP_STAGE_KINDS:
            issues.append(
                PlanIssue(
                    f"{path}.kind",
                    "unsupported_stage",
                    f"Supported stages are: {', '.join(MVP_STAGE_KINDS)}.",
                )
            )
        if not math.isfinite(stage.duration_seconds) or not (
            0.1 <= stage.duration_seconds <= _MAX_STAGE_SECONDS
        ):
            issues.append(
                PlanIssue(
                    f"{path}.duration_seconds",
                    "duration_out_of_range",
                    f"Duration must be finite and between 0.1 and {_MAX_STAGE_SECONDS} seconds.",
                )
            )
        elif not _has_decimal_places(stage.duration_seconds, 1):
            issues.append(
                PlanIssue(
                    f"{path}.duration_seconds",
                    "duration_precision",
                    "Duration must be representable with at most one decimal place.",
                )
            )
        for field_name, users in (("start_users", stage.start_users), ("end_users", stage.end_users)):
            if not 0 <= users <= _MAX_LOAD_USERS:
                issues.append(
                    PlanIssue(
                        f"{path}.{field_name}",
                        "users_out_of_range",
                        f"Virtual users must be from 0 to {_MAX_LOAD_USERS}.",
                    )
                )
        if not 0 <= stage.think_time_ms <= _MAX_THINK_TIME_MS:
            issues.append(
                PlanIssue(
                    f"{path}.think_time_ms",
                    "think_time_out_of_range",
                    f"Think time must be from 0 to {_MAX_THINK_TIME_MS} milliseconds.",
                )
            )
        if math.isfinite(stage.duration_seconds):
            total_seconds += stage.duration_seconds
    if peak_users <= 0:
        issues.append(PlanIssue("stages", "zero_users", "Every stage has zero virtual users; nothing to run."))
    if total_seconds > _MAX_TOTAL_SECONDS:
        issues.append(
            PlanIssue(
                "stages",
                "total_duration_out_of_range",
                f"Total duration cannot exceed {_MAX_TOTAL_SECONDS} seconds.",
            )
        )

    for index, threshold in enumerate(plan.thresholds):
        path = f"thresholds[{index}]"
        if not math.isfinite(threshold.target) or not (
            -_MAX_THRESHOLD_TARGET <= threshold.target <= _MAX_THRESHOLD_TARGET
        ):
            issues.append(
                PlanIssue(
                    f"{path}.target",
                    "threshold_out_of_range",
                    f"Threshold targets must be finite and between -{_MAX_THRESHOLD_TARGET} and {_MAX_THRESHOLD_TARGET}.",
                )
            )
        elif not _has_decimal_places(threshold.target, 3):
            issues.append(
                PlanIssue(
                    f"{path}.target",
                    "threshold_precision",
                    "Threshold targets must be representable with at most three decimal places.",
                )
            )
        if threshold.metric == "error_rate" and not 0 <= threshold.target <= 1:
            issues.append(
                PlanIssue(
                    f"{path}.target",
                    "error_rate_units",
                    "error_rate targets are fractions from 0 to 1; 0.01 means 1 percent.",
                )
            )
    return issues


def _with_mapped_path_placeholders(plan: WorkflowPlan) -> WorkflowPlan:
    mapped_names = [
        mapping.target_key.split(":", 1)[1]
        for mapping in plan.mappings
        if mapping.target_key.startswith("path:") and ":" in mapping.target_key
    ]
    if not mapped_names:
        return plan
    parameters = dict(plan.parameters)
    for mapped_name in mapped_names:
        parameter_name = next(
            (item_name for item_name in parameters if item_name.casefold() == mapped_name.casefold()),
            mapped_name,
        )
        parameters[parameter_name] = _CSV_PATH_PLACEHOLDER
    return plan.with_changes(parameters=tuple(parameters.items()))


def _unresolved_value_issues(
    plan: WorkflowPlan,
    *,
    allow_csv_path: bool,
) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    mapped_paths = {
        mapping.target_key.split(":", 1)[1].casefold()
        for mapping in plan.mappings
        if allow_csv_path and mapping.target_key.startswith("path:") and ":" in mapping.target_key
    }
    for position, (name, value) in enumerate(plan.parameters):
        if has_variable(value) and not (
            value == _CSV_PATH_PLACEHOLDER and name.casefold() in mapped_paths
        ):
            issues.append(
                PlanIssue(
                    f"parameters[{position}].value",
                    "unresolved_variable",
                    f"Unresolved variable in request parameter {name!r}; request constants must be literal.",
                )
            )
    for position, item in enumerate(plan.filters):
        if any(has_variable(value) for value in item.values):
            issues.append(
                PlanIssue(
                    f"filters[{position}].values",
                    "unresolved_variable",
                    "Filter values must be literal request constants.",
                )
            )

    def visit(value: Any, path: str) -> None:
        if isinstance(value, str) and has_variable(value):
            issues.append(
                PlanIssue(path, "unresolved_variable", "Request payload values must not contain unresolved variables.")
            )
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(plan.payload, "payload")
    return issues


def _local_path_value_issues(plan: WorkflowPlan, endpoint: Endpoint) -> list[PlanIssue]:
    issues: list[PlanIssue] = []
    for position, (name, value) in enumerate(plan.parameters):
        parameter = next(
            (item for item in endpoint.parameters if item.name.casefold() == name.casefold()),
            None,
        )
        if parameter is not None and _looks_like_local_path(value) and not has_variable(value):
            issues.append(
                PlanIssue(
                    f"parameters[{position}].value",
                    "file_path_forbidden",
                    "AI workflow request constants cannot contain arbitrary local filesystem paths.",
                )
            )
    for position, condition in enumerate(plan.filters):
        if any(_looks_like_local_path(value) for value in condition.values):
            issues.append(
                PlanIssue(
                    f"filters[{position}].values",
                    "file_path_forbidden",
                    "AI workflow request constants cannot contain arbitrary local filesystem paths.",
                )
            )

    is_json_patch = (endpoint.payload_type or "").startswith("JsonPatchDocument")

    def visit(value: Any, path: str, key: str = "") -> None:
        if isinstance(value, str):
            if _looks_like_local_path(value) and not (is_json_patch and key.casefold() in {"path", "from"}):
                issues.append(
                    PlanIssue(
                        path,
                        "file_path_forbidden",
                        "AI workflow request constants cannot contain arbitrary local filesystem paths.",
                    )
                )
        elif isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, f"{path}.{child_key}", str(child_key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(plan.payload, "payload")
    return issues


def _remove_mapped_payload_warnings(
    warnings: tuple[str, ...], mappings: tuple[ColumnMapping, ...]
) -> tuple[str, ...]:
    mapped_roots = {
        mapping.target_key[len("payload:") :].split(".", 1)[0].casefold()
        for mapping in mappings
        if mapping.target_key.startswith("payload:")
    }
    if not mapped_roots:
        return warnings
    prefix = "Required payload fields left for you to fill: "
    result: list[str] = []
    for warning in warnings:
        if not warning.startswith(prefix):
            result.append(warning)
            continue
        remaining = [
            name.strip()
            for name in warning[len(prefix) :].split(",")
            if name.strip() and name.strip().casefold() not in mapped_roots
        ]
        if remaining:
            result.append(prefix + ", ".join(remaining))
    return tuple(result)


def to_workflow_request(plan: WorkflowPlan, index: CatalogIndex) -> ExplorerRequest:
    """Convert a validated workflow draft to an Explorer request without CSV markers."""

    endpoint = index.get(plan.endpoint_id)
    if endpoint is None:
        raise ValueError(f"Cannot open unknown catalog endpoint {plan.endpoint_id!r}.")
    request = to_explorer_request(plan, endpoint, index.filter_schema(endpoint) is not None)
    values = {
        key: "" if value == _CSV_PATH_PLACEHOLDER else value
        for key, value in request.values.items()
    }
    return ExplorerRequest(
        endpoint_id=request.endpoint_id,
        values=values,
        payload=request.payload,
        expected_status=request.expected_status,
    )


def render_workflow_outcome(outcome: PlanOutcome, index: CatalogIndex) -> str:
    """Render a workflow result as escaped HTML; this function never executes it."""

    plan = outcome.plan
    escape = lambda value: html.escape(str(value), quote=True)
    mode = getattr(plan, "mode", "")
    mode_name = "Data Runner" if mode == "data" else "Load Testing" if mode == "load" else "Workflow"
    parts = [
        f"<h3 style='margin:0'>{escape(plan.title or mode_name + ' plan')}</h3>",
        f"<p><b>{escape(mode_name)}</b> | status: <b>{escape(plan.status)}</b></p>",
    ]
    if plan.summary:
        parts.append(f"<p>{escape(plan.summary)}</p>")
    endpoint = index.get(plan.endpoint_id)
    if endpoint is not None:
        parts.append(
            "<p><b>Request</b><br>"
            f"{escape(endpoint.method)} <code>{escape(endpoint.path)}</code><br>"
            f"{escape(endpoint.service)} | {escape(endpoint.controller)}.{escape(endpoint.action)}"
            "</p>"
        )
        if plan.expected_status:
            parts.append(f"<p><b>Expected status</b> {escape(plan.expected_status)}</p>")
    elif plan.endpoint_id:
        parts.append(f"<p><b>Endpoint</b> {escape(plan.endpoint_id)}</p>")
    if plan.parameters:
        items = "".join(
            f"<li><code>{escape(name)}</code>: {escape(value)}</li>"
            for name, value in plan.parameters
            if value != _CSV_PATH_PLACEHOLDER
        )
        if items:
            parts.append(f"<p><b>Request parameters</b></p><ul>{items}</ul>")
    if plan.payload is not None:
        parts.append(
            "<p><b>Request payload</b></p><pre>"
            + escape(json.dumps(plan.payload, ensure_ascii=False, indent=2, default=str))
            + "</pre>"
        )

    if isinstance(plan, WorkflowPlan) and plan.mode == "data" and plan.mappings:
        rows = "".join(
            "<tr>"
            f"<td>{escape(mapping.column)}</td>"
            f"<td>{escape(mapping.target_key)}</td>"
            f"<td>{escape(', '.join(item.kind for item in mapping.transforms) or 'None')}</td>"
            "</tr>"
            for mapping in plan.mappings
        )
        parts.append(
            "<p><b>CSV mappings</b></p><table><thead><tr><th>Column</th><th>Destination</th>"
            f"<th>Transforms</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    if isinstance(plan, WorkflowPlan) and plan.mode == "data" and plan.status == "plan":
        parts.append(
            "<p><b>CSV validation required:</b> This plan has column names only, not row samples. "
            "After selecting the CSV in the native editor, validate its rows and mappings before running.</p>"
        )
    elif isinstance(plan, WorkflowPlan) and plan.mode == "load":
        stage_rows = "".join(
            "<tr>"
            f"<td>{escape(stage.kind)}</td><td>{stage.duration_seconds:.1f}</td>"
            f"<td>{stage.start_users}</td><td>{stage.end_users}</td>"
            f"<td>{stage.think_time_ms}</td><td>{escape(stage.label)}</td></tr>"
            for stage in plan.stages
        )
        parts.append(
            "<p><b>Closed virtual-user stages</b></p><table><thead><tr><th>Stage</th><th>Duration (s)</th>"
            "<th>Start users</th><th>End users</th><th>Think time (ms)</th><th>Label</th></tr></thead>"
            f"<tbody>{stage_rows}</tbody></table>"
        )
        if plan.thresholds:
            threshold_rows = "".join(
                "<tr>"
                f"<td>{escape(threshold.metric)}"
                f"{' (fraction 0-1)' if threshold.metric == 'error_rate' else ''}</td>"
                f"<td>{escape(threshold.operator)}</td><td>{threshold.target:.3f}</td>"
                f"<td>{escape(threshold.label)}</td></tr>"
                for threshold in plan.thresholds
            )
            parts.append(
                "<p><b>Thresholds</b></p><table><thead><tr><th>Metric</th><th>Operator</th>"
                f"<th>Target</th><th>Label</th></tr></thead><tbody>{threshold_rows}</tbody></table>"
            )

    if plan.status == "clarify":
        if plan.question:
            parts.append(f"<p><b>{escape(plan.question)}</b></p>")
        if plan.choices:
            parts.append("<ul>" + "".join(f"<li>{escape(choice)}</li>" for choice in plan.choices) + "</ul>")

    if outcome.issues:
        parts.append(
            "<p><b>Validation issues</b></p><ul>"
            + "".join(f"<li>{escape(issue.as_text())}</li>" for issue in outcome.issues)
            + "</ul>"
        )
    if plan.warnings:
        parts.append(
            "<p><b>Warnings</b></p><ul>"
            + "".join(f"<li>{escape(item)}</li>" for item in plan.warnings)
            + "</ul>"
        )
    if plan.assumptions:
        parts.append(
            "<p><b>Assumptions</b></p><ul>"
            + "".join(f"<li>{escape(item)}</li>" for item in plan.assumptions)
            + "</ul>"
        )
    parts.append(
        "<p><b>Planning only:</b> No API request or load test was run. Review and explicitly approve "
        "the draft in the native editor before execution. Load tests additionally require native "
        "environment permission and any required write-safety confirmation.</p>"
    )
    return "\n".join(parts)
