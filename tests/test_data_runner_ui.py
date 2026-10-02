"""GUI coverage for the Data Runner tab (source -> mapping -> validate -> run)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication, QMessageBox

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.data_runner.mapping import Transform
from api_tester.data_runner.ui import DataRunnerTab, TransformEditorDialog, _transforms_summary
from api_tester.execution.models import ExecutionEnvironmentSnapshot


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
        )


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


def _catalog(endpoint: Endpoint | None = None) -> Catalog:
    endpoint = endpoint or _endpoint()
    return Catalog(
        services=(Service("TrialAuth", "TrialAuth", "https://example.test", (endpoint,)),),
        filter_schemas={},
        payload_schemas={},
        enums={},
    )


def _environment(*, base_url: str = "https://qa.example.test") -> ExecutionEnvironmentSnapshot:
    return ExecutionEnvironmentSnapshot(
        environment_id="env-1",
        environment_name="QA",
        base_urls={"TrialAuth": base_url} if base_url else {},
    )


def _write_csv(tmp_path: Path, rows: list[dict[str, str]], header: list[str]) -> str:
    path = tmp_path / "data.csv"
    lines = [",".join(header)]
    for row in rows:
        lines.append(",".join(row.get(column, "") for column in header))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def _make_tab(app: QApplication, catalog: Catalog | None = None) -> DataRunnerTab:
    return DataRunnerTab(catalog or _catalog(), lambda: _environment())


# ------------------------------------------------------------------ endpoint selection


def test_loads_endpoints_for_the_default_service(app: QApplication) -> None:
    tab = _make_tab(app)
    assert tab.service_combo.count() == 1
    assert tab.endpoint_combo.count() == 1
    assert tab.current_endpoint is not None
    assert tab.current_endpoint.id == "brands.update"


def test_endpoint_selection_rebuilds_mapping_targets(app: QApplication) -> None:
    tab = _make_tab(app)
    keys = {target.key for target in tab.current_targets}
    assert "path:Id" in keys
    assert "query:Status" in keys


def test_refresh_catalog_rebuilds_combos(app: QApplication) -> None:
    tab = _make_tab(app)
    other_endpoint = Endpoint(
        id="teams.get",
        service="TrialAuth",
        controller="Teams",
        action="Get",
        method="GET",
        path="/Teams/{Id}",
        parameters=(Parameter(name="Id", source="path", type="int", required=True),),
    )
    new_catalog = _catalog(other_endpoint)
    tab.refresh_catalog(new_catalog)
    assert tab.current_endpoint is not None
    assert tab.current_endpoint.id == "teams.get"


# ------------------------------------------------------------------ source


def test_load_preview_populates_tables(app: QApplication, tmp_path: Path) -> None:
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Id": "1", "Status": "Active"}], ["Id", "Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()

    assert tab.preview is not None
    assert "2 columns" in tab.preview_summary_label.text()
    assert tab.sample_rows_table.columnCount() == 2
    assert tab.sample_rows_table.rowCount() == 1


def test_load_preview_warns_on_missing_file(app: QApplication, quiet_dialogs: None, tmp_path: Path) -> None:
    tab = _make_tab(app)
    tab.csv_path.setText(str(tmp_path / "missing.csv"))
    tab._load_preview()
    assert tab.preview is None


# ------------------------------------------------------------------ mapping


def test_add_and_remove_mapping_rows(app: QApplication) -> None:
    tab = _make_tab(app)
    tab._add_mapping_row("Id", "path:Id")
    tab._add_mapping_row("Status", "query:Status")
    assert tab.mapping_table.rowCount() == 2

    mappings = tab._current_mappings()
    assert {m.column for m in mappings} == {"Id", "Status"}
    assert {m.target_key for m in mappings} == {"path:Id", "query:Status"}

    tab._remove_mapping_row(0)
    assert tab.mapping_table.rowCount() == 1


def test_auto_map_matches_columns_by_trailing_name(app: QApplication, tmp_path: Path) -> None:
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Id": "1", "Status": "Active"}], ["Id", "Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()

    tab._auto_map()

    mappings = {m.column: m.target_key for m in tab._current_mappings()}
    assert mappings.get("Id") == "path:Id"
    assert mappings.get("Status") == "query:Status"


def test_transform_editor_round_trips_selections(app: QApplication) -> None:
    catalog = _catalog()
    target = next(t for t in tab_targets(catalog) if t.key == "path:Id")
    dialog = TransformEditorDialog(target, (Transform("trim"), Transform("required")))
    assert dialog.trim.isChecked() is True
    assert dialog.required.isChecked() is True
    dialog.default_value.setText("42")
    transforms = dialog.transforms()
    kinds = {t.kind for t in transforms}
    assert {"trim", "required", "default"} <= kinds
    assert _transforms_summary(transforms) != "(none)"


def tab_targets(catalog: Catalog):
    from api_tester.data_runner.mapping import mapping_targets

    endpoint = catalog.services[0].endpoints[0]
    return mapping_targets(endpoint, catalog)


# ------------------------------------------------------------------ validate


def test_validate_requires_an_endpoint_and_csv(app: QApplication, quiet_dialogs: None) -> None:
    tab = _make_tab(app)
    tab._validate()
    assert tab.plan is None
    assert tab.run_button.isEnabled() is False


def test_validate_builds_an_executable_plan(app: QApplication, tmp_path: Path) -> None:
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Id": "1", "Status": "Active"}], ["Id", "Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    tab._add_mapping_row("Id", "path:Id")
    tab._add_mapping_row("Status", "query:Status")

    tab._validate()

    assert tab.plan is not None
    assert tab.plan.is_executable is True
    assert tab.run_button.isEnabled() is True
    assert tab.preview_rows_table.rowCount() == 0
    assert tab.validate_summary_label.text().startswith("Ready to run")


def test_validate_reports_blocking_issues(app: QApplication, tmp_path: Path) -> None:
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Id": "1"}], ["Id"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    tab._add_mapping_row("Id", "path:Id")
    tab.environment_provider = lambda: _environment(base_url="")

    tab._validate()

    assert tab.plan is not None
    assert tab.plan.is_executable is False
    assert tab.preview_rows_table.rowCount() >= 1
    assert tab.validate_summary_label.text().startswith("1 blocking issue")
    assert tab.run_button.isEnabled() is False


# ------------------------------------------------------------------ run & results


def test_run_button_disabled_without_a_plan(app: QApplication) -> None:
    tab = _make_tab(app)
    assert tab.run_button.isEnabled() is False
    assert tab.pause_button.isEnabled() is False
    assert tab.stop_button.isEnabled() is False


class _FakeSession:
    def request(self, *args, **kwargs):
        from api_tester import client

        return client.requests.request(*args, **kwargs)

    def close(self) -> None:
        pass


def _install_fake_transport(monkeypatch: pytest.MonkeyPatch, *, status_code: int = 200) -> None:
    from types import SimpleNamespace

    def fake_request(method, url, **kwargs):
        return SimpleNamespace(
            status_code=status_code,
            headers={"content-type": "application/json"},
            content=b"{}",
            url=url,
            request=SimpleNamespace(headers=kwargs.get("headers", {}), body=None),
            reason="OK",
        )

    monkeypatch.setattr("api_tester.client.requests.request", fake_request)
    monkeypatch.setattr("api_tester.execution.transport.requests.Session", _FakeSession)


def _run_to_completion(tab: DataRunnerTab, app: QApplication, *, timeout_ms: int = 5000) -> None:
    tab._start_run()
    assert tab._thread is not None
    deadline = timeout_ms
    while tab._thread is not None and deadline > 0:
        app.processEvents()
        QThread.msleep(5)
        deadline -= 5
    app.processEvents()


def _prepare_validated_tab(app: QApplication, tmp_path: Path) -> DataRunnerTab:
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Id": "1", "Status": "Active"}], ["Id", "Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    tab._add_mapping_row("Id", "path:Id")
    tab._add_mapping_row("Status", "query:Status")
    tab._validate()
    assert tab.plan is not None and tab.plan.is_executable
    return tab


def test_run_end_to_end_updates_results_table(app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    assert tab.results_table.rowCount() == 1
    assert tab.export_button.isEnabled() is True


def test_run_updates_outcome_chart_and_latency_summary(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    assert tab._outcome_counts.get("passed") == 1
    assert tab.latency_chart._values, "latency chart should have received one sample"
    assert "p50" in tab.latency_summary_label.text()


def test_run_populates_outcome_latency_and_throughput_charts(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression coverage for the current Charts tab metrics."""
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    assert tab.outcome_chart._counts["passed"] == 1
    assert tab.latency_chart._values, "latency chart should have received a sample"
    assert "p50" in tab.latency_summary_label.text()
    assert [label for label, _value, _color in tab.latency_percentile_chart._entries] == [
        "p50",
        "p90",
        "p99",
        "max",
    ]
    assert tab._cumulative_completed == 1
    assert tab.cumulative_completed_chart._values["Completed"] == [1.0]
    assert tab.latency_timeline_chart._points, "latency timeline should have received a sample"


def test_run_error_populates_error_category_chart(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(method, url, **kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr("api_tester.client.requests.request", fake_request)
    monkeypatch.setattr("api_tester.execution.transport.requests.Session", _FakeSession)

    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    assert tab._error_category_counts, "error-category counts should have been recorded"
    assert tab.error_category_chart._entries, "error-category chart should have entries"


def test_default_persist_path_is_derived_from_csv(app: QApplication, tmp_path: Path) -> None:
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Id": "1"}], ["Id"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    assert tab._default_persist_path() == f"{csv_path}.runs.db"


def test_run_with_persistence_enabled_writes_history_db(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)
    db_path = tmp_path / "history.db"
    tab.persist_checkbox.setChecked(True)
    tab.persist_path.setText(str(db_path))

    _run_to_completion(tab, app)

    assert db_path.exists()

    tab.history_path.setText(str(db_path))
    tab._refresh_history()
    assert tab.history_runs_table.rowCount() == 1
    assert tab.history_runs_table.item(0, 1).text() == "brands.update"


def test_run_error_outcome_is_reflected_in_outcome_chart(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the 'error' vs 'errored' outcome-key mismatch between
    ui.py's request outcomes and charts.py's stacked-bar keys."""

    def fake_request(method, url, **kwargs):
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr("api_tester.client.requests.request", fake_request)
    monkeypatch.setattr("api_tester.execution.transport.requests.Session", _FakeSession)

    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    assert tab._outcome_counts.get("error") == 1
    # The chart must recognise "error" as a known key, not silently drop it.
    assert tab.outcome_chart._counts.get("error") == 1


def test_stepper_marks_steps_completed_as_the_wizard_progresses(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _make_tab(app)
    assert tab.stepper._completed == set()

    csv_path = _write_csv(tmp_path, [{"Id": "1", "Status": "Active"}], ["Id", "Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    assert 0 in tab.stepper._completed

    tab._add_mapping_row("Id", "path:Id")
    tab._add_mapping_row("Status", "query:Status")
    tab._refresh_stepper_progress()
    assert 1 in tab.stepper._completed

    tab._validate()
    assert tab.plan is not None and tab.plan.is_executable
    assert 2 in tab.stepper._completed

    _run_to_completion(tab, app)
    assert 3 in tab.stepper._completed


def test_stat_cards_and_progress_bar_reflect_a_completed_run(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    assert tab.stat_cards["completed"].value_label.text() == "1"
    assert tab.stat_cards["passed"].value_label.text() == "1"
    assert tab.stat_cards["remaining"].value_label.text() == "0"
    assert tab.run_progress_bar.value() == tab.run_progress_bar.maximum()


def test_run_context_panel_shows_a_write_warning_for_mutating_endpoints(
    app: QApplication, tmp_path: Path
) -> None:
    tab = _prepare_validated_tab(app, tmp_path)
    tab._refresh_run_context_panel()
    # The fixture endpoint is a PATCH, which is a write verb. isVisible() requires
    # a shown top-level window, so check the widget's own visibility flag instead.
    assert tab.write_warning_label.isHidden() is False


def test_history_load_populates_results_table(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)
    db_path = tmp_path / "history.db"
    tab.persist_checkbox.setChecked(True)
    tab.persist_path.setText(str(db_path))
    _run_to_completion(tab, app)

    tab.history_path.setText(str(db_path))
    tab._refresh_history()
    tab.history_runs_table.selectRow(0)

    assert tab.history_load_button.isEnabled() is True
    tab._load_history_run_into_results()

    assert tab.results_table.rowCount() == 1
    assert tab.steps.currentIndex() == 3


def test_history_load_lazily_fetches_row_detail_from_the_store(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row_details table is not preloaded when a run is loaded from
    History; selecting a row should trigger a single lazy fetch that
    populates the row inspector, mirroring a live run's behaviour."""
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)
    db_path = tmp_path / "history.db"
    tab.persist_checkbox.setChecked(True)
    tab.persist_path.setText(str(db_path))
    _run_to_completion(tab, app)

    tab.history_path.setText(str(db_path))
    tab._refresh_history()
    tab.history_runs_table.selectRow(0)
    tab._load_history_run_into_results()

    # Loading from history should not preload details up front.
    assert tab._row_details == {}

    tab.results_table.selectRow(0)
    tab._on_result_row_selected()

    assert 1 in tab._row_details
    assert "no detail captured" not in tab.row_inspector_hint.text()
    assert tab.row_inspector_input_row.toPlainText() != ""


def test_history_load_shows_fallback_hint_for_runs_predating_detail_capture(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)
    db_path = tmp_path / "history.db"
    tab.persist_checkbox.setChecked(True)
    tab.persist_path.setText(str(db_path))
    _run_to_completion(tab, app)

    tab.history_path.setText(str(db_path))
    tab._refresh_history()
    # Simulate an older run whose detail was never persisted.
    tab._history_store._connection.execute("DELETE FROM row_details")
    tab._history_store._connection.commit()
    tab.history_runs_table.selectRow(0)
    tab._load_history_run_into_results()
    tab.results_table.selectRow(0)
    tab._on_result_row_selected()

    assert "no detail captured" in tab.row_inspector_hint.text()


# ------------------------------------------------------------------ Data Runner polish: results pagination


def _prepare_validated_tab_with_rows(app: QApplication, tmp_path: Path, row_count: int) -> DataRunnerTab:
    tab = _make_tab(app)
    rows = [{"Id": str(i), "Status": "Active"} for i in range(1, row_count + 1)]
    csv_path = _write_csv(tmp_path, rows, ["Id", "Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    tab._add_mapping_row("Id", "path:Id")
    tab._add_mapping_row("Status", "query:Status")
    tab._validate()
    assert tab.plan is not None and tab.plan.is_executable
    return tab


def test_results_table_only_materialises_the_current_page(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 12)
    tab._on_page_size_changed("5")

    _run_to_completion(tab, app)

    assert len(tab._result_rows) == 12
    # Live-tailing should land on the last (partial) page: rows 11-12.
    assert tab.results_table.rowCount() == 2
    assert tab.results_table.item(0, 0).text() == "11"
    assert tab.results_pager.page == 2
    assert tab.results_pager.page_count == 3
    assert tab.results_pager.total_label.text() == "of 3"
    assert tab.results_pager.prev_button.isEnabled() is True
    assert tab.results_pager.next_button.isEnabled() is False


def test_previous_page_button_stops_following_the_latest_page(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 12)
    tab._on_page_size_changed("5")

    _run_to_completion(tab, app)
    tab.results_pager.prev_button.click()

    assert tab._follow_latest_page is False
    assert tab.results_pager.page == 1
    assert tab.results_table.rowCount() == 5
    assert tab.results_table.item(0, 0).text() == "6"
    assert tab.results_pager.next_button.isEnabled() is True


def test_jump_to_latest_restores_live_tailing(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 12)
    tab._on_page_size_changed("5")
    _run_to_completion(tab, app)
    tab.results_pager.prev_button.click()

    tab.results_pager.next_button.click()

    assert tab._follow_latest_page is True
    assert tab.results_pager.page == 2
    assert tab.results_table.rowCount() == 2
    assert tab.results_table.item(0, 0).text() == "11"


def test_changing_page_size_re_renders_and_keeps_roughly_the_same_place(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 12)
    tab._on_page_size_changed("5")
    _run_to_completion(tab, app)
    tab.results_pager.prev_button.click()  # now viewing rows 6-10

    tab._on_page_size_changed("100")

    assert tab.results_table.rowCount() == 12
    assert tab.results_pager.page == 0
    assert tab.results_pager.page_count == 1
    assert tab.results_pager.total_label.text() == "of 1"


def test_load_history_run_paginates_large_runs(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 12)
    db_path = tmp_path / "history.db"
    tab.persist_checkbox.setChecked(True)
    tab.persist_path.setText(str(db_path))
    _run_to_completion(tab, app)
    tab._on_page_size_changed("5")

    tab.history_path.setText(str(db_path))
    tab._refresh_history()
    tab.history_runs_table.selectRow(0)
    tab._load_history_run_into_results()

    assert len(tab._result_rows) == 12
    assert tab.results_table.rowCount() <= 5


# ------------------------------------------------------------------ Phase 2: results tabs + row inspector


def test_results_tabs_include_the_expected_sub_tabs(app: QApplication) -> None:
    tab = _make_tab(app)
    titles = [tab.results_tabs.tabText(i) for i in range(tab.results_tabs.count())]
    assert titles == ["Live results", "Input validation", "Errors", "Charts", "Mapping"]
    assert tab.results_tabs.cornerWidget() is not None


def test_selecting_a_passed_row_shows_a_placeholder_instead_of_the_response_body(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    tab.results_table.selectRow(0)
    assert "not retained" in tab.row_inspector_response.toPlainText()
    assert tab.row_inspector_input_row.toPlainText().strip() != ""
    assert '"Id"' in tab.row_inspector_request.toPlainText()


def test_selecting_a_failed_row_retains_the_full_response_body(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch, status_code=500)
    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    tab.results_table.selectRow(0)
    assert "not retained" not in tab.row_inspector_response.toPlainText()
    assert tab.row_inspector_response.toPlainText().strip() != ""


def test_invalid_rows_are_listed_in_the_input_validation_tab(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Id": "", "Status": "Bad"}], ["Id", "Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    tab._add_mapping_row("Id", "path:Id")
    tab._add_mapping_row("Status", "query:Status")
    tab._validate()

    _run_to_completion(tab, app)

    assert tab.invalid_rows_table.rowCount() == 1
    assert tab.invalid_rows_table.item(0, 1).text() == "invalid"


def test_failed_rows_are_listed_in_the_errors_tab(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch, status_code=500)
    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    assert tab.run_errors_list.count() == 1


def _install_mixed_status_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Id=1 -> HTTP 500, Id=2 -> HTTP 404, Id=3 -> connection failure,
    Id=4 -> passes — enough variety to exercise the Errors tab's category
    filter and search box."""
    from types import SimpleNamespace

    def fake_request(method, url, **kwargs):
        if "/Brands/3" in url:
            raise ConnectionError("simulated network failure")
        status_code = 500 if "/Brands/1" in url else 404 if "/Brands/2" in url else 200
        return SimpleNamespace(
            status_code=status_code,
            headers={"content-type": "application/json"},
            content=b"{}",
            url=url,
            request=SimpleNamespace(headers=kwargs.get("headers", {}), body=None),
            reason="OK",
        )

    monkeypatch.setattr("api_tester.client.requests.request", fake_request)
    monkeypatch.setattr("api_tester.execution.transport.requests.Session", _FakeSession)


def test_errors_tab_lists_every_failed_or_errored_row_by_default(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mixed_status_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 4)

    _run_to_completion(tab, app)

    assert tab.run_errors_list.count() == 3
    assert {tab.errors_category_filter.itemText(i) for i in range(tab.errors_category_filter.count())} == {
        "All categories",
        "http_5xx",
        "http_4xx",
        "internal",
    }
    assert tab.errors_filter_summary_label.text() == "3 errors"


def test_errors_tab_category_filter_narrows_the_list(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mixed_status_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 4)
    _run_to_completion(tab, app)

    tab.errors_category_filter.setCurrentText("http_4xx")

    assert tab.run_errors_list.count() == 1
    assert "Row 2" in tab.run_errors_list.item(0).text()
    assert tab.errors_filter_summary_label.text() == "Showing 1 of 3 errors"


def test_errors_tab_search_box_filters_by_row_number(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mixed_status_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 4)
    _run_to_completion(tab, app)

    tab.errors_search_box.setText("Row 3")

    assert tab.run_errors_list.count() == 1
    assert "Row 3" in tab.run_errors_list.item(0).text()


def test_errors_tab_filters_reset_between_runs(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mixed_status_transport(monkeypatch)
    tab = _prepare_validated_tab_with_rows(app, tmp_path, 4)
    _run_to_completion(tab, app)
    tab.errors_search_box.setText("Row 3")
    tab.errors_category_filter.setCurrentText("http_5xx")

    _run_to_completion(tab, app)

    assert tab.errors_search_box.text() == ""
    assert tab.errors_category_filter.currentText() == "All categories"


def test_mapping_tab_reflects_the_mappings_used_for_the_run(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)

    _run_to_completion(tab, app)

    assert tab.mapping_summary_table.rowCount() == 2
    columns = {tab.mapping_summary_table.item(row, 0).text() for row in range(2)}
    assert columns == {"Id", "Status"}



def _brightest_icon_pixel(button, mode) -> str:
    from PyQt6.QtGui import QIcon  # noqa: F401 - mode enum lives here

    image = button.icon().pixmap(18, 18, mode).toImage()
    opaque = [
        image.pixelColor(x, y) for x in range(18) for y in range(18)
        if image.pixelColor(x, y).alpha() > 200
    ]
    return max(opaque, key=lambda c: c.lightness()).name()


@pytest.mark.parametrize(
    ("attribute", "label"),
    [
        ("history_load_button", "Load"),
        ("history_export_button", "Export"),
    ],
)
def test_history_actions_show_a_label_beside_their_icon(app, attribute, label) -> None:
    from PyQt6.QtWidgets import QSizePolicy

    button = getattr(_make_tab(app), attribute)
    assert button.text() == label
    assert not button.icon().isNull()
    # Sized to the label rather than stretched across the toolbar.
    assert button.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Fixed


def test_entering_validate_step_builds_plan_without_a_validate_button(
    app: QApplication, tmp_path: Path
) -> None:
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Id": "", "Status": "Active"}], ["Id", "Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    tab._add_mapping_row("Id", "path:Id")
    tab._add_mapping_row("Status", "query:Status")

    assert not hasattr(tab, "validate_button")
    tab.steps.setCurrentIndex(2)

    assert tab.plan is not None
    assert tab.plan.invalid_count == 1
    assert tab.preview_rows_table.rowCount() == 1
    assert tab.preview_rows_table.item(0, 2).text() == "Invalid"
    assert tab.validate_summary_label.text().startswith("1 invalid rows")


def test_export_corner_action_is_enabled_only_after_a_run_produces_rows(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _prepare_validated_tab(app, tmp_path)

    assert tab.results_tabs.cornerWidget().isAncestorOf(tab.export_button)
    assert tab.export_button.isEnabled() is False

    _run_to_completion(tab, app)

    assert tab.results_table.rowCount() == 1
    assert tab.export_button.isEnabled() is True


def test_mapping_rows_use_the_roomy_theme_height(app) -> None:
    from api_tester import theme

    tab = _make_tab(app)
    assert tab.mapping_table.objectName() == "roomyEditorTable"
    assert tab.mapping_table.verticalHeader().defaultSectionSize() == theme.ROOMY_ROW_HEIGHT


@pytest.mark.parametrize("mode", ["Light", "Dark"])
def test_run_icon_stays_white_on_the_accent_button(app, mode) -> None:
    from PyQt6.QtGui import QIcon
    from api_tester import theme

    theme.apply_theme(app, mode)
    try:
        tab = _make_tab(app)
        tab.refresh_theme()
        assert not tab.run_button.isEnabled()
        for icon_mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
            assert _brightest_icon_pixel(tab.run_button, icon_mode) == theme.TEXT_INVERSE
    finally:
        theme.apply_theme(app, "Light")


# ------------------------------------------------------------------ request template (API Explorer)


def _two_endpoint_catalog() -> Catalog:
    other = Endpoint(
        id="teams.get",
        service="TrialAuth",
        controller="Teams",
        action="Get",
        method="GET",
        path="/Teams/{Id}",
        parameters=(Parameter(name="Id", source="path", type="int", required=True),),
    )
    return Catalog(
        services=(Service("TrialAuth", "TrialAuth", "https://example.test", (other, _endpoint())),),
        filter_schemas={},
        payload_schemas={},
        enums={},
    )


def test_load_request_selects_endpoint_and_shows_template_banner(app: QApplication) -> None:
    tab = _make_tab(app, _two_endpoint_catalog())
    tab.steps.setCurrentIndex(1)
    assert tab.current_endpoint.id == "teams.get"

    assert tab.load_request(
        _endpoint(),
        {"path:Id": "9", "query:Status": "Off", "enabled:query:Status": "false"},
        {"Name": "X"},
    )

    assert tab.current_endpoint.id == "brands.update"
    assert tab.steps.currentIndex() == 0
    assert tab.has_request_template()
    assert tab._template_values == {"path:Id": "9"}  # unticked optional dropped
    assert tab._template_payload == {"Name": "X"}
    assert not tab.template_banner.isHidden()
    assert "1 value · payload" in tab.template_banner_label.text()


def test_load_request_rejects_unknown_endpoint(app: QApplication) -> None:
    tab = _make_tab(app)
    unknown = Endpoint(
        id="x.get", service="Other", controller="X", action="Get", method="GET", path="/X"
    )
    assert not tab.load_request(unknown, {}, None)
    assert not tab.has_request_template()
    assert tab.template_banner.isHidden()


def test_template_is_cleared_by_button_or_endpoint_change(app: QApplication) -> None:
    tab = _make_tab(app, _two_endpoint_catalog())
    assert tab.load_request(_endpoint(), {"path:Id": "9"}, None)
    tab.template_clear_button.click()
    assert not tab.has_request_template()
    assert tab.template_banner.isHidden()

    assert tab.load_request(_endpoint(), {"path:Id": "9"}, None)
    tab.endpoint_combo.setCurrentIndex(0)  # user picks another endpoint
    assert not tab.has_request_template()


def test_validate_uses_template_for_unmapped_required_values(app: QApplication, tmp_path: Path) -> None:
    tab = _make_tab(app)
    csv_path = _write_csv(tmp_path, [{"Status": "Active"}], ["Status"])
    tab.csv_path.setText(csv_path)
    tab._load_preview()
    tab._add_mapping_row("Status", "query:Status")

    tab._validate()
    assert tab.plan is not None and tab.plan.invalid_count == 1  # Id unmapped

    assert tab.load_request(_endpoint(), {"path:Id": "42"}, None)
    tab._validate()
    assert tab.plan is not None and tab.plan.is_executable
    assert tab.plan.invalid_count == 0
    assert tab.plan.mapper.template_values == {"path:Id": "42"}


# ------------------------------------------------------------------ searchable selectors


def test_service_and_endpoint_selectors_are_searchable(app: QApplication) -> None:
    from api_tester.widgets import SEARCH_TEXT_ROLE, SearchableComboBox

    tab = _make_tab(app, _two_endpoint_catalog())
    for combo in (tab.service_combo, tab.endpoint_combo):
        assert isinstance(combo, SearchableComboBox)
        assert combo.isEditable()
    assert "Brands" in tab.endpoint_combo.itemData(1, SEARCH_TEXT_ROLE)


def test_endpoint_search_selects_the_real_endpoint(app: QApplication) -> None:
    tab = _make_tab(app, _two_endpoint_catalog())
    combo = tab.endpoint_combo
    combo.lineEdit().setText("patch brands")
    combo.lineEdit().textEdited.emit("patch brands")
    assert tab.current_endpoint.id == "teams.get"  # typing alone changes nothing
    combo.flush_search()
    assert combo.suggestion_texts() == ["PATCH /Brands/{Id}"]
    combo.choose_suggestion(0)
    assert tab.current_endpoint.id == "brands.update"
    assert combo.currentData().id == "brands.update"


def test_service_search_does_not_repopulate_endpoints_while_typing(app: QApplication) -> None:
    other = Endpoint(
        id="crops.get", service="TrialLibrary", controller="Crops", action="Get",
        method="GET", path="/Crops/{Id}",
    )
    catalog = Catalog(
        services=(
            Service("TrialAuth", "TrialAuth", "https://a.test", (_endpoint(),)),
            Service("TrialLibrary", "TrialLibrary", "https://l.test", (other,)),
        ),
        filter_schemas={}, payload_schemas={}, enums={},
    )
    tab = _make_tab(app, catalog)
    combo = tab.service_combo
    combo.lineEdit().setText("lib")
    combo.lineEdit().textEdited.emit("lib")
    assert tab.current_endpoint.id == "brands.update"
    combo.flush_search()
    combo.choose_suggestion(0)
    assert combo.itemText(combo.currentIndex()) == "TrialLibrary"
    assert tab.current_endpoint.id == "crops.get"
    assert tab.load_request(_endpoint(), {"path:Id": "1"}, None)  # still switches back
    assert tab.current_endpoint.id == "brands.update"
