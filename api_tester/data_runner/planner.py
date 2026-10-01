"""Validates a Data Runner configuration and produces an executable plan.

This is the integration point between the CSV source
(:mod:`api_tester.data_runner.csv_source`), catalog-aware mapping
(:mod:`api_tester.data_runner.mapping`), and the shared execution
environment snapshot (:mod:`api_tester.execution.models`). See
``platform architecture guide: load-testing-and-data-runner.md``
section 9.9 for the row validation states this module classifies rows into.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from api_tester.catalog import Catalog, Endpoint
from api_tester.execution.cancellation import CancellationController
from api_tester.execution.models import ExecutionEnvironmentSnapshot

from .csv_source import CsvSource
from .mapping import ColumnMapping, ResolvedRow, RowMapper

#: Cap on the number of problem rows kept in memory. Validating every row of a
#: very large file is cheap, but materialising a result object per row is not —
#: and no user reads 200,000 issue rows. Counts stay exact past the cap.
DEFAULT_MAX_ISSUE_ROWS = 5_000

#: How often ``build_plan`` reports progress, in rows. Emitting per row would
#: cost more in signal delivery than the validation itself.
DEFAULT_PROGRESS_INTERVAL = 500


@dataclass(frozen=True)
class PlanIssue:
    """A plan-level problem, distinct from a per-row validation issue."""

    severity: str  # "error" | "warning"
    message: str


@dataclass(frozen=True)
class RowPreviewResult:
    """The outcome of resolving one row ahead of execution."""

    row_number: int
    correlation_key: str
    skip: bool
    is_valid: bool
    issue_count: int
    #: The resolved row's issue messages. The count alone cannot tell the user
    #: what to fix, so the messages travel with it.
    issue_messages: tuple[str, ...] = ()


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
    #: Only rows that are invalid or skipped. Valid rows are counted but not
    #: retained: nothing in the UI renders them, and keeping one object per
    #: row is what made full-file validation prohibitive.
    issue_rows: tuple[RowPreviewResult, ...] = field(default_factory=tuple)
    total_row_count: int = 0
    #: Rows actually resolved. Equals ``total_row_count`` for a completed
    #: validation, and is lower when validation was cancelled part-way.
    validated_row_count: int = 0
    valid_count: int = 0
    invalid_count: int = 0
    skip_count: int = 0
    #: True when more problem rows existed than ``issue_rows`` retained.
    issue_rows_truncated: bool = False
    #: True when the caller stopped validation before the last row.
    validation_cancelled: bool = False

    # Pre-existing call sites and tests read the older ``preview_*`` names.
    # They are retained as aliases; the stored names dropped the "preview"
    # prefix because these now describe the whole file, not a 50-row sample.
    @property
    def preview_rows(self) -> tuple[RowPreviewResult, ...]:
        return self.issue_rows

    @property
    def preview_valid_count(self) -> int:
        return self.valid_count

    @property
    def preview_invalid_count(self) -> int:
        return self.invalid_count

    @property
    def preview_skip_count(self) -> int:
        return self.skip_count

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
    progress: Callable[[int, int], None] | None = None,
    cancellation: CancellationController | None = None,
    max_issue_rows: int = DEFAULT_MAX_ISSUE_ROWS,
    progress_interval: int = DEFAULT_PROGRESS_INTERVAL,
) -> DataRunPlan:
    """Validates every static precondition and resolves every row.

    Resolution is streamed: rows are pulled one at a time from ``csv_source``
    and only problem rows are retained (see ``issue_rows``), so peak memory
    is bounded by the number of *issues* rather than the size of the file.

    ``progress`` is invoked as ``progress(validated, total)`` every
    ``progress_interval`` rows and once at the end, so a caller on a
    background thread can drive a progress bar. ``cancellation`` is polled
    per row; stopping yields a partial plan with ``validation_cancelled``
    set, which is never executable.
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
    validated_row_count = 0
    issue_rows: list[RowPreviewResult] = []
    issue_rows_truncated = False
    cancelled = False
    valid_count = invalid_count = skip_count = 0
    try:
        # Counted up front (and cached on the source) so ``progress`` has a
        # denominator; this pass only reads rows, it does not resolve them.
        total_row_count = csv_source.total_row_count()
        for row_number, row in csv_source.iter_rows():
            if cancellation is not None and cancellation.should_stop():
                cancelled = True
                break
            resolved: ResolvedRow = mapper.resolve(row_number, row)
            validated_row_count += 1
            if resolved.skip:
                skip_count += 1
            elif resolved.is_valid:
                valid_count += 1
            else:
                invalid_count += 1
            if resolved.skip or not resolved.is_valid:
                if len(issue_rows) < max_issue_rows:
                    issue_rows.append(
                        RowPreviewResult(
                            row_number=row_number,
                            correlation_key=resolved.correlation_key,
                            skip=resolved.skip,
                            is_valid=resolved.is_valid,
                            issue_count=len(resolved.issues),
                            issue_messages=tuple(issue.message for issue in resolved.issues),
                        )
                    )
                else:
                    issue_rows_truncated = True
            if progress is not None and validated_row_count % progress_interval == 0:
                progress(validated_row_count, total_row_count)
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

    if progress is not None:
        progress(validated_row_count, total_row_count)

    if cancelled:
        issues.append(
            PlanIssue(
                "error",
                f"Validation was cancelled after {validated_row_count} of "
                f"{total_row_count} rows. Re-open this step to validate the whole file.",
            )
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
        issue_rows=tuple(issue_rows),
        total_row_count=total_row_count,
        validated_row_count=validated_row_count,
        valid_count=valid_count,
        invalid_count=invalid_count,
        skip_count=skip_count,
        issue_rows_truncated=issue_rows_truncated,
        validation_cancelled=cancelled,
    )
