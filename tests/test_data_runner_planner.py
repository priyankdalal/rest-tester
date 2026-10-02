from __future__ import annotations

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.data_runner.csv_source import CsvImportSettings, CsvSource
from api_tester.data_runner.mapping import ColumnMapping, Transform
from api_tester.data_runner.planner import build_plan
from api_tester.execution.models import ExecutionEnvironmentSnapshot


def _endpoint() -> Endpoint:
    return Endpoint(
        id="brands.update",
        service="TrialAuth",
        controller="Brands",
        action="Update",
        method="PATCH",
        path="/Brands/{Id}",
        parameters=(
            Parameter(name="Id", source="path", type="int", required=True),
            Parameter(name="Status", source="query", type="string", required=False),
        ),
    )


def _catalog(endpoint: Endpoint) -> Catalog:
    return Catalog(
        services=(Service("TrialAuth", "TrialAuth", "https://example.test", (endpoint,)),),
        filter_schemas={},
        payload_schemas={},
        enums={},
    )


def _environment(*, base_url: str = "https://qa.example.test") -> ExecutionEnvironmentSnapshot:
    base_urls = {"TrialAuth": base_url} if base_url else {}
    return ExecutionEnvironmentSnapshot(
        environment_id="env-1",
        environment_name="QA",
        base_urls=base_urls,
    )


def _write_csv(tmp_path, rows: list[dict[str, str]], header: list[str]) -> str:
    path = tmp_path / "data.csv"
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(row.get(column, "") for column in header))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_build_plan_is_executable_for_valid_configuration(tmp_path) -> None:
    endpoint = _endpoint()
    catalog = _catalog(endpoint)
    csv_path = _write_csv(
        tmp_path,
        [{"Id": "1", "Status": "Active"}, {"Id": "2", "Status": "Inactive"}],
        ["Id", "Status"],
    )
    csv_source = CsvSource(CsvImportSettings(path=csv_path))
    mappings = [
        ColumnMapping("Id", "path:Id"),
        ColumnMapping("Status", "query:Status"),
    ]

    plan = build_plan(endpoint, catalog, csv_source, mappings, _environment())

    assert plan.is_executable is True
    assert plan.total_row_count == 2
    assert plan.preview_valid_count == 2
    assert plan.preview_invalid_count == 0
    assert not plan.has_blocking_issues


def test_build_plan_flags_missing_base_url(tmp_path) -> None:
    endpoint = _endpoint()
    catalog = _catalog(endpoint)
    csv_path = _write_csv(tmp_path, [{"Id": "1"}], ["Id"])
    csv_source = CsvSource(CsvImportSettings(path=csv_path))
    mappings = [ColumnMapping("Id", "path:Id")]

    plan = build_plan(endpoint, catalog, csv_source, mappings, _environment(base_url=""))

    assert plan.is_executable is False
    assert plan.has_blocking_issues is True
    assert any("base URL" in issue.message for issue in plan.issues)


def test_build_plan_flags_unknown_mapping_target(tmp_path) -> None:
    endpoint = _endpoint()
    catalog = _catalog(endpoint)
    csv_path = _write_csv(tmp_path, [{"Id": "1"}], ["Id"])
    csv_source = CsvSource(CsvImportSettings(path=csv_path))
    mappings = [ColumnMapping("Id", "query:DoesNotExist")]

    plan = build_plan(endpoint, catalog, csv_source, mappings, _environment())

    assert plan.mapper is None
    assert plan.is_executable is False
    assert any("Unknown mapping target" in issue.message for issue in plan.issues)


def test_build_plan_flags_missing_csv_file() -> None:
    endpoint = _endpoint()
    catalog = _catalog(endpoint)
    csv_source = CsvSource(CsvImportSettings(path="C:\\does\\not\\exist.csv"))
    mappings = [ColumnMapping("Id", "path:Id")]

    plan = build_plan(endpoint, catalog, csv_source, mappings, _environment())

    assert plan.is_executable is False
    assert any("Could not read CSV file" in issue.message for issue in plan.issues)


def test_build_plan_marks_invalid_rows_in_preview(tmp_path) -> None:
    endpoint = _endpoint()
    catalog = _catalog(endpoint)
    csv_path = _write_csv(
        tmp_path,
        [{"Id": "1", "Status": "Active"}, {"Id": "", "Status": "Inactive"}],
        ["Id", "Status"],
    )
    csv_source = CsvSource(CsvImportSettings(path=csv_path))
    mappings = [
        ColumnMapping("Id", "path:Id", transforms=(Transform("required"),)),
        ColumnMapping("Status", "query:Status"),
    ]

    plan = build_plan(endpoint, catalog, csv_source, mappings, _environment())

    assert plan.total_row_count == 2
    assert plan.preview_valid_count == 1
    assert plan.preview_invalid_count == 1
    # An invalid preview row does not block the whole run, it is skipped/reported at execution time.
    assert plan.is_executable is True


def test_build_plan_warns_on_empty_csv(tmp_path) -> None:
    endpoint = _endpoint()
    catalog = _catalog(endpoint)
    path = tmp_path / "empty.csv"
    path.write_text("Id\n", encoding="utf-8")
    csv_source = CsvSource(CsvImportSettings(path=str(path)))
    mappings = [ColumnMapping("Id", "path:Id")]

    plan = build_plan(endpoint, catalog, csv_source, mappings, _environment())

    assert plan.total_row_count == 0
    assert plan.is_executable is False
    assert any(issue.severity == "warning" for issue in plan.issues)
