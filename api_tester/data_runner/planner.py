"""Validates a Data Runner configuration and produces an executable plan.

This is the integration point between the CSV source
(:mod:`api_tester.data_runner.csv_source`), catalog-aware mapping
(:mod:`api_tester.data_runner.mapping`), and the shared execution
environment snapshot (:mod:`api_tester.execution.models`). See
``platform architecture guide: load-testing-and-data-runner.md``
section 9.9 for the row validation states this module classifies rows into.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from api_tester.catalog import Catalog, Endpoint
from api_tester.execution.models import ExecutionEnvironmentSnapshot

from .csv_source import CsvSource
from .mapping import ColumnMapping, ResolvedRow, RowMapper


@dataclass(frozen=True)
class PlanIssue:
    """A plan-level problem, distinct from a per-row validation issue."""

    severity: str  # "error" | "warning"
    message: str


@dataclass(frozen=True)
class RowPreviewResult:
    """The outcome of resolving one sampled row ahead of execution."""

    row_number: int
    correlation_key: str
    skip: bool
    is_valid: bool
    issue_count: int


@dataclass(frozen=True)
class DataRunPlan:
    """An executable (or blocked) Data Runner configuration.

    ``mapper`` is ``None`` when the mapping itself could not be built (for
    example, a mapping references an unknown target); in that case
    ``issues`` explains why and the plan is never executable.
    """

    endpoint: Endpoint
    catalog: Catalog
    csv_source: CsvSource
    mappings: tuple[ColumnMapping, ...]
    environment: ExecutionEnvironmentSnapshot
    default_expected_status: str
    mapper: RowMapper | None
    issues: tuple[PlanIssue, ...] = field(default_factory=tuple)
    preview_rows: tuple[RowPreviewResult, ...] = field(default_factory=tuple)
    total_row_count: int = 0
    preview_valid_count: int = 0
    preview_invalid_count: int = 0
    preview_skip_count: int = 0

    @property
    def has_blocking_issues(self) -> bool:
        return any(issue.severity == "error" for issue in self.issues)

    @property
    def is_executable(self) -> bool:
        return self.mapper is not None and not self.has_blocking_issues and self.total_row_count > 0


def build_plan(
    endpoint: Endpoint,
    catalog: Catalog,
    csv_source: CsvSource,
    mappings: list[ColumnMapping],
    environment: ExecutionEnvironmentSnapshot,
    *,
    default_expected_status: str = "200-299",
    environment_variables: dict[str, str] | None = None,
    preview_sample_rows: int = 50,
) -> DataRunPlan:
    """Validates every static precondition and resolves a small row sample.

    Full-file row validation is deliberately deferred to execution time — for
    a very large CSV, resolving every row here would duplicate the cost of
    the run itself. The preview sample gives the user immediate, actionable
    feedback (missing required mappings, bad transforms, unknown enum
    values) without waiting for the whole file to stream.
    """
    issues: list[PlanIssue] = []

    if not environment.base_url(endpoint.service):
        issues.append(
            PlanIssue(
                "error",
                f"No base URL configured for service '{endpoint.service}' in "
                f"environment '{environment.environment_name}'.",
            )
        )

    mapper: RowMapper | None = None
    try:
        mapper = RowMapper(
            endpoint,
            catalog,
            mappings,
            default_expected_status=default_expected_status,
            environment_variables=environment_variables,
        )
    except ValueError as exc:
        issues.append(PlanIssue("error", str(exc)))
        return DataRunPlan(
            endpoint=endpoint,
            catalog=catalog,
            csv_source=csv_source,
            mappings=tuple(mappings),
            environment=environment,
            default_expected_status=default_expected_status,
            mapper=None,
            issues=tuple(issues),
        )

    total_row_count = 0
    preview_rows: list[RowPreviewResult] = []
    preview_valid = preview_invalid = preview_skip = 0
    try:
        total_row_count = csv_source.total_row_count()
        for row_number, row in itertools.islice(csv_source.iter_rows(), preview_sample_rows):
            resolved: ResolvedRow = mapper.resolve(row_number, row)
            if resolved.skip:
                preview_skip += 1
            elif resolved.is_valid:
                preview_valid += 1
            else:
                preview_invalid += 1
            preview_rows.append(
                RowPreviewResult(
                    row_number=row_number,
                    correlation_key=resolved.correlation_key,
                    skip=resolved.skip,
                    is_valid=resolved.is_valid,
                    issue_count=len(resolved.issues),
                )
            )
    except (FileNotFoundError, OSError) as exc:
        issues.append(PlanIssue("error", f"Could not read CSV file: {exc}"))
        return DataRunPlan(
            endpoint=endpoint,
            catalog=catalog,
            csv_source=csv_source,
            mappings=tuple(mappings),
            environment=environment,
            default_expected_status=default_expected_status,
            mapper=mapper,
            issues=tuple(issues),
        )

    if total_row_count == 0:
        issues.append(PlanIssue("warning", "CSV has no rows to execute."))

    return DataRunPlan(
        endpoint=endpoint,
        catalog=catalog,
        csv_source=csv_source,
        mappings=tuple(mappings),
        environment=environment,
        default_expected_status=default_expected_status,
        mapper=mapper,
        issues=tuple(issues),
        preview_rows=tuple(preview_rows),
        total_row_count=total_row_count,
        preview_valid_count=preview_valid,
        preview_invalid_count=preview_invalid,
        preview_skip_count=preview_skip,
    )
