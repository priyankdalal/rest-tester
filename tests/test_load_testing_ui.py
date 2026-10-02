"""GUI coverage for the Load Testing Studio tab (scenario -> safety/thresholds
-> pre-run summary -> live dashboard/errors/final report).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication, QMessageBox, QSizePolicy

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.execution.models import ExecutionEnvironmentSnapshot
from api_tester.load_testing.ui import LoadTestingTab


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def quiet_dialogs(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(
            QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)
        )


def _endpoint(method: str = "GET") -> Endpoint:
    return Endpoint(
        id="brands.get",
        service="TrialAuth",
        controller="Brands",
        action="Get",
        method=method,
        path="/Brands/{Id}",
        parameters=(Parameter(name="Id", source="path", type="int", required=True),),
        expected_status="200-299",
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


def _make_tab(app: QApplication, catalog: Catalog | None = None, environment=None) -> LoadTestingTab:
    return LoadTestingTab(catalog or _catalog(), environment or (lambda: _environment()))


def _make_fast_scenario(tab: LoadTestingTab) -> None:
    """Replaces the default 4-stage/2-minute ramp with a single ~0.3s stage
    so run-lifecycle tests complete quickly."""
    tab.stages_table.setRowCount(0)
    tab._add_stage_row(kind="steady", duration_seconds=0.3, start_users=2, end_users=2, think_time_ms=50)
    tab.environment_permits_checkbox.setChecked(True)
    tab.confirm_checkbox.setChecked(True)


class _FakeSession:
    def request(self, *args, **kwargs):
        from api_tester import client

        return client.requests.request(*args, **kwargs)

    def close(self) -> None:
        pass


def _install_fake_transport(monkeypatch: pytest.MonkeyPatch, *, status_code: int = 200) -> None:
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


def _run_to_completion(tab: LoadTestingTab, app: QApplication, *, timeout_ms: int = 8000) -> None:
    tab._start_run()
    assert tab._thread is not None
    deadline = timeout_ms
    while tab._thread is not None and deadline > 0:
        app.processEvents()
        QThread.msleep(5)
        deadline -= 5
    app.processEvents()


# ------------------------------------------------------------------ endpoint selection


def test_loads_endpoints_for_the_default_service(app: QApplication) -> None:
    tab = _make_tab(app)
    assert tab.service_combo.count() == 1
    assert tab.endpoint_combo.count() == 1
    assert tab.current_endpoint is not None
    assert tab.current_endpoint.id == "brands.get"


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
    tab.refresh_catalog(_catalog(other_endpoint))
    assert tab.current_endpoint is not None
    assert tab.current_endpoint.id == "teams.get"


def test_parameters_table_seeds_required_parameters_on_endpoint_load(app: QApplication) -> None:
    tab = _make_tab(app)
    assert tab.parameters_table.rowCount() == 1
    assert tab.parameters_table.item(0, 0).text() == "path"
    assert tab.parameters_table.item(0, 1).text() == "Id"
    assert tab.parameters_table.item(0, 4).text() != ""


def test_write_method_hint_flags_write_endpoints(app: QApplication) -> None:
    tab = _make_tab(app, _catalog(_endpoint("PATCH")))
    assert "write method" in tab.write_method_hint.text()
    assert "Confirm write endpoints" in tab.write_method_hint.text()


# ------------------------------------------------------------------ stage editor


def test_default_ramp_seeds_four_stages(app: QApplication) -> None:
    tab = _make_tab(app)
    assert tab.stages_table.rowCount() == 4
    kinds = [tab._stage_from_row(row).kind for row in range(4)]
    assert kinds == ["warm_up", "ramp_up", "steady", "ramp_down"]


def test_scenario_sections_use_accordion_layout_with_schedule_expanded(app: QApplication) -> None:
    tab = _make_tab(app)

    assert tab.scenario_details_section.is_expanded() is False
    assert tab.request_values_section.is_expanded() is False
    assert tab.payload_section.is_expanded() is False
    assert tab.load_schedule_section.is_expanded() is True
    assert tab.stages_table.minimumHeight() >= 330
    assert "4 stage(s)" in tab.load_schedule_section.summary()
    assert "1 parameter(s)" in tab.request_values_section.summary()


def test_collapsed_load_schedule_releases_its_vertical_space(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.resize(1200, 800)
    tab.show()
    app.processEvents()
    expanded_height = tab.load_schedule_section.height()

    tab.load_schedule_section.set_expanded(False)
    app.processEvents()
    collapsed_height = tab.load_schedule_section.height()

    assert collapsed_height < expanded_height
    assert collapsed_height <= tab.load_schedule_section.header.sizeHint().height() + 4
    assert tab.load_schedule_section.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
    tab.hide()


def test_collapsed_scenario_accordions_stay_top_aligned(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.resize(1200, 800)
    tab.load_schedule_section.set_expanded(False)
    tab.show()
    app.processEvents()

    sections = (
        tab.scenario_details_section,
        tab.request_values_section,
        tab.payload_section,
        tab.load_schedule_section,
    )
    gaps = [
        sections[index + 1].geometry().top() - sections[index].geometry().bottom() - 1
        for index in range(len(sections) - 1)
    ]

    assert all(gap <= 16 for gap in gaps)
    assert sections[-1].geometry().bottom() < tab.steps.height() // 2
    tab.hide()


def test_scenario_page_scrolls_instead_of_overlapping_when_cramped(app: QApplication) -> None:
    """Regression: all sections expanded in a short viewport must scroll, not overlap.

    Stacking the four accordions directly in a plain QVBoxLayout (no scroll
    area) let Qt compress widgets below their minimum size once the summed
    content height exceeded the visible tab area, producing overlapping text
    and clipped/missing borders. The scenario page must be a QScrollArea so
    overflow scrolls instead.
    """
    from PyQt6.QtWidgets import QScrollArea

    tab = _make_tab(app)
    scenario_page = tab.steps.widget(0)
    assert isinstance(scenario_page, QScrollArea)

    tab.resize(1200, 420)
    tab.scenario_details_section.set_expanded(True)
    tab.request_values_section.set_expanded(True)
    tab.payload_section.set_expanded(True)
    tab.load_schedule_section.set_expanded(True)
    tab.show()
    app.processEvents()

    sections = (
        tab.scenario_details_section,
        tab.request_values_section,
        tab.payload_section,
        tab.load_schedule_section,
    )
    for previous, current in zip(sections, sections[1:]):
        # Each section's top must sit at/after the previous section's bottom;
        # a negative gap means the layout squeezed them into overlap.
        assert current.geometry().top() >= previous.geometry().bottom()
    tab.hide()


def test_add_and_remove_stage_rows(app: QApplication) -> None:
    tab = _make_tab(app)
    tab._add_stage_row(kind="spike", duration_seconds=5.0, start_users=1, end_users=20)
    assert tab.stages_table.rowCount() == 5
    tab.stages_table.selectRow(4)
    tab._remove_selected_stage_row()
    assert tab.stages_table.rowCount() == 4


def test_move_stage_row_reorders_stages(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.stages_table.selectRow(2)  # "steady"
    tab._move_selected_stage_row(-1)
    kinds = [tab._stage_from_row(row).kind for row in range(4)]
    assert kinds == ["warm_up", "steady", "ramp_up", "ramp_down"]


def test_stage_totals_label_reflects_duration_and_peak_users(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.stages_table.setRowCount(0)
    tab._add_stage_row(kind="steady", duration_seconds=30.0, start_users=8, end_users=8)
    assert "1 stage(s)" in tab.stage_totals_label.text()
    assert "30s" in tab.stage_totals_label.text()
    assert "8 peak virtual users" in tab.stage_totals_label.text()


# ------------------------------------------------------------------ safety limits & thresholds


def test_safety_limits_from_form_respects_optional_fields(app: QApplication) -> None:
    tab = _make_tab(app)
    limits = tab._safety_limits_from_form()
    assert limits.max_total_requests is None  # checkbox unchecked by default
    assert limits.error_rate_stop_threshold == pytest.approx(0.5)  # checked by default, 50%
    assert limits.latency_stop_ms is None  # checkbox unchecked by default
    assert limits.auth_failure_stop is True

    tab.max_requests_checkbox.setChecked(True)
    tab.max_requests_spin.setValue(250)
    tab.latency_stop_checkbox.setChecked(True)
    tab.latency_stop_spin.setValue(2500.0)
    limits = tab._safety_limits_from_form()
    assert limits.max_total_requests == 250
    assert limits.latency_stop_ms == pytest.approx(2500.0)


def test_add_and_remove_threshold_rows(app: QApplication) -> None:
    tab = _make_tab(app)
    tab._add_threshold_row(metric="error_rate", operator="<=", target=0.05, label="Error budget")
    assert tab.thresholds_table.rowCount() == 1
    threshold = tab._threshold_from_row(0)
    assert threshold.metric == "error_rate"
    assert threshold.target == pytest.approx(0.05)
    assert threshold.label == "Error budget"

    tab.thresholds_table.selectRow(0)
    tab._remove_selected_threshold_row()
    assert tab.thresholds_table.rowCount() == 0


# ------------------------------------------------------------------ pre-run summary / plan


def test_build_plan_blocks_start_without_environment_permission(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.confirm_checkbox.setChecked(True)
    plan = tab._build_and_render_plan()
    assert plan is not None
    assert plan.is_executable is False
    assert tab.plan_issues_list.count() >= 1
    assert tab.start_button.isEnabled() is False


def test_build_plan_is_executable_once_safety_controls_are_satisfied(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.environment_permits_checkbox.setChecked(True)
    tab.confirm_checkbox.setChecked(True)
    plan = tab._build_and_render_plan()
    assert plan is not None
    assert plan.is_executable is True
    assert tab.start_button.isEnabled() is True
    assert tab.summary_endpoint_label.text() == "GET https://qa.example.test/Brands/{Id}"
    assert tab.summary_write_label.text() == "No"


def test_start_is_blocked_without_explicit_confirmation(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.environment_permits_checkbox.setChecked(True)
    tab._build_and_render_plan()
    assert tab.start_button.isEnabled() is False


def test_write_endpoint_requires_confirmed_write_endpoints_flag(app: QApplication) -> None:
    tab = _make_tab(app, _catalog(_endpoint("POST")))
    tab.environment_permits_checkbox.setChecked(True)
    tab.confirm_checkbox.setChecked(True)
    plan = tab._build_and_render_plan()
    assert plan is not None
    assert plan.is_executable is False
    tab.confirmed_write_checkbox.setChecked(True)
    plan = tab._build_and_render_plan()
    assert plan is not None
    assert plan.is_executable is True


def test_plan_requires_at_least_one_stage(app: QApplication, quiet_dialogs: None) -> None:
    tab = _make_tab(app)
    tab.stages_table.setRowCount(0)
    tab.environment_permits_checkbox.setChecked(True)
    tab.confirm_checkbox.setChecked(True)
    plan = tab._build_and_render_plan()
    assert plan is None  # _build_scenario raises ValueError, caught and reported


def test_invalid_payload_json_is_rejected_before_planning(app: QApplication, quiet_dialogs: None) -> None:
    tab = _make_tab(app)
    tab.payload_editor.setPlainText("{not valid json")
    tab.environment_permits_checkbox.setChecked(True)
    tab.confirm_checkbox.setChecked(True)
    plan = tab._build_and_render_plan()
    assert plan is None


# ------------------------------------------------------------------ run lifecycle


def test_run_button_disabled_without_a_plan(app: QApplication) -> None:
    tab = _make_tab(app)
    assert tab.start_button.isEnabled() is False
    assert tab.stop_button.isEnabled() is False


def test_live_workspace_matches_operational_dashboard_hierarchy(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.environment_permits_checkbox.setChecked(True)
    tab.confirm_checkbox.setChecked(True)
    plan = tab._build_and_render_plan()

    assert plan is not None
    assert tab.live_title_label.text() == "brands.get — QA"
    assert tab.live_method_label.text() == "GET"
    assert tab.live_endpoint_label.text() == "/Brands/{Id}"
    assert len(tab._stage_progress_bars) == 4
    assert [tab.results_tabs.tabText(index) for index in range(tab.results_tabs.count())] == [
        "Live dashboard",
        "Endpoint",
        "Errors",
        "Warnings",
        "Final report",
    ]


def test_run_end_to_end_produces_a_passing_final_report(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _make_tab(app)
    _make_fast_scenario(tab)
    tab.persist_path.setText(str(tmp_path / "run.db"))

    _run_to_completion(tab, app)

    assert tab._last_summary is not None
    assert tab._last_summary.outcome == "PASS"
    assert tab._last_summary.total_requests > 0
    assert tab.report_outcome_label.text() == "PASS"
    assert "requests" in tab.report_totals_label.text()


def test_run_updates_live_stat_cards_and_charts(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _make_tab(app)
    _make_fast_scenario(tab)
    tab.persist_path.setText(str(tmp_path / "run.db"))

    _run_to_completion(tab, app)

    assert tab.stat_cards["completed"].value_label.text() != "–"
    assert tab.stat_cards["passed"].value_label.text() != "–"
    assert tab.active_users_chart._values, "active-users chart should have received a sample"
    assert tab.throughput_chart._values, "throughput chart should have received a sample"


def test_run_with_failures_records_categorized_errors(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_fake_transport(monkeypatch, status_code=500)
    tab = _make_tab(app)
    _make_fast_scenario(tab)
    db_path = tmp_path / "errors.db"
    tab.persist_path.setText(str(db_path))

    _run_to_completion(tab, app)

    assert tab._last_summary is not None
    assert tab._last_summary.failed > 0
    assert tab.run_errors_list.count() >= 1
    assert "http_5xx" in tab.run_errors_list.item(0).text()


def test_errors_filter_narrows_visible_entries(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_fake_transport(monkeypatch, status_code=500)
    tab = _make_tab(app)
    _make_fast_scenario(tab)
    tab.persist_path.setText(str(tmp_path / "errors.db"))
    _run_to_completion(tab, app)

    total_shown = tab.run_errors_list.count()
    assert total_shown >= 1

    tab.errors_category_filter.setCurrentText("http_5xx")
    assert tab.run_errors_list.count() == total_shown

    tab.errors_category_filter.setCurrentText("authentication")
    assert tab.run_errors_list.count() == 0


def test_stop_run_marks_outcome_as_stopped(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_fake_transport(monkeypatch)
    tab = _make_tab(app)
    tab.stages_table.setRowCount(0)
    tab._add_stage_row(kind="steady", duration_seconds=30.0, start_users=1, end_users=1, think_time_ms=50)
    tab.environment_permits_checkbox.setChecked(True)
    tab.confirm_checkbox.setChecked(True)
    tab.persist_path.setText(str(tmp_path / "stop.db"))

    tab._start_run()
    assert tab._thread is not None
    # Let at least one request complete before requesting a stop.
    for _ in range(40):
        app.processEvents()
        QThread.msleep(10)
    tab._stop_run()

    deadline = 8000
    while tab._thread is not None and deadline > 0:
        app.processEvents()
        QThread.msleep(10)
        deadline -= 10
    app.processEvents()

    assert tab._last_summary is not None
    assert tab._last_summary.outcome == "STOPPED"
    assert tab._last_summary.stop_reason is not None
    assert tab.report_outcome_label.text() == "STOPPED"


def test_run_failed_signal_shows_a_warning_without_crashing(
    app: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_request(method, url, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("api_tester.client.requests.request", fake_request)
    monkeypatch.setattr("api_tester.execution.transport.requests.Session", _FakeSession)
    monkeypatch.setattr(
        "api_tester.load_testing.ui.QMessageBox.warning",
        staticmethod(lambda *a, **k: None),
    )

    tab = _make_tab(app)
    _make_fast_scenario(tab)

    # Force the worker's run() to raise synchronously by breaking the engine.
    tab._start_run()
    assert tab._thread is not None
    deadline = 8000
    while tab._thread is not None and deadline > 0:
        app.processEvents()
        QThread.msleep(5)
        deadline -= 5
    app.processEvents()

    # A run with an unreachable/failing transport still finishes with a
    # summary (requests are classified as errors, not raised out of engine.run()).
    assert tab._last_summary is not None or tab.run_status_label.text() == "Load test failed."


# ------------------------------------------------------------------ stepper & nav wiring


def test_stepper_switches_pages(app: QApplication) -> None:
    tab = _make_tab(app)
    tab._stepper_clicked(2)
    assert tab.steps.currentIndex() == 2
    assert tab.stepper._current == 2


def test_step_change_updates_stepper_current_index(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.steps.setCurrentIndex(3)
    assert tab.stepper._current == 3


def test_refresh_theme_does_not_raise(app: QApplication) -> None:
    tab = _make_tab(app)
    tab.refresh_theme()  # smoke test only — icon colors aren't asserted


def test_build_plan_button_shows_a_label(app) -> None:
    button = _make_tab(app).build_summary_button
    assert button.text() == "Build load plan"
    assert not button.icon().isNull()


def test_stage_rows_use_the_roomy_theme_height(app) -> None:
    from api_tester import theme

    tab = _make_tab(app)
    assert tab.stages_table.objectName() == "roomyEditorTable"
    assert tab.stages_table.verticalHeader().defaultSectionSize() == theme.ROOMY_ROW_HEIGHT


@pytest.mark.parametrize("mode", ["Light", "Dark"])
def test_start_icon_stays_white_on_the_accent_button(app, mode) -> None:
    from PyQt6.QtGui import QIcon
    from api_tester import theme

    theme.apply_theme(app, mode)
    try:
        tab = _make_tab(app)
        tab.refresh_theme()
        for icon_mode in (QIcon.Mode.Normal, QIcon.Mode.Disabled):
            image = tab.start_button.icon().pixmap(18, 18, icon_mode).toImage()
            opaque = [
                image.pixelColor(x, y) for x in range(18) for y in range(18)
                if image.pixelColor(x, y).alpha() > 200
            ]
            assert max(opaque, key=lambda c: c.lightness()).name() == theme.TEXT_INVERSE
    finally:
        theme.apply_theme(app, "Light")

# ------------------------------------------------------------------ completed statistics & saved runs


def _fast_snapshot_options(monkeypatch: pytest.MonkeyPatch) -> None:
    from api_tester.load_testing import ui as load_ui
    from api_tester.load_testing.engine import LoadRunOptions

    monkeypatch.setattr(
        load_ui,
        "LoadRunOptions",
        lambda: LoadRunOptions(metric_snapshot_interval_seconds=0.05, active_user_poll_seconds=0.01),
    )


def _persisted_run(app, monkeypatch, tmp_path, *, name: str = "run.db") -> LoadTestingTab:
    _install_fake_transport(monkeypatch)
    _fast_snapshot_options(monkeypatch)
    tab = _make_tab(app)
    _make_fast_scenario(tab)
    tab.stages_table.setRowCount(0)
    tab._add_stage_row(kind="steady", duration_seconds=0.4, start_users=2, end_users=2, think_time_ms=20, label="Hold")
    tab._add_threshold_row(metric="error_rate", operator="<=", target=0.5, label="Errors")
    tab.persist_path.setText(str(tmp_path / name))
    _run_to_completion(tab, app)
    assert tab._last_summary is not None
    return tab


def test_completed_run_switches_cards_to_min_avg_max(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    tab = _persisted_run(app, monkeypatch, tmp_path)
    cards = tab.stat_cards

    assert cards["throughput"].title_label.text() == "THROUGHPUT · PEAK"
    assert "min" in cards["throughput"].subtitle_label.text() and "avg" in cards["throughput"].subtitle_label.text()
    assert cards["latency"].title_label.text() == "AVG LATENCY"
    assert cards["p99"].title_label.text() == "P99 · AVG"
    assert "max" in cards["p99"].subtitle_label.text()
    assert cards["active_users"].title_label.text() == "USERS · PEAK"
    assert cards["active_users"].value_label.text() == "2"
    # Error rate and Completed keep their live presentation.
    assert cards["error_rate"].title_label.text() == "ERROR RATE"
    assert cards["completed"].value_label.text() == f"{tab._last_summary.total_requests:,}"

    tab._reset_live_metrics()
    assert cards["throughput"].title_label.text() == "THROUGHPUT"
    assert cards["p99"].title_label.text() == "P99 LATENCY"


def test_final_report_is_detailed(app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    tab = _persisted_run(app, monkeypatch, tmp_path)
    report = tab.report_view

    metrics = [report.metric_stats_table.item(row, 0).text() for row in range(report.metric_stats_table.rowCount())]
    assert metrics == [
        "Throughput (req/s)", "Request latency", "P50 latency", "P95 latency", "P99 latency", "Error rate", "Virtual users",
    ]
    assert report.metric_stats_table.item(0, 3).text() != "—"  # max throughput
    assert report.stage_table.rowCount() == 1
    assert report.stage_table.item(0, 0).text() == "1. Hold"
    assert report.thresholds_table.rowCount() == 1
    assert report.thresholds_table.item(0, 4).text() == "PASS"
    assert report.run_id_label.text() == tab._last_summary.run_id
    assert str(tmp_path / "run.db") in report.persisted_label.text()
    assert "GET /Brands/{Id}" in report.configuration_label.text()
    assert "p99" in report.latency_distribution_label.text()


def test_rail_card_reports_persistence_and_size(app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    tab = _persisted_run(app, monkeypatch, tmp_path)

    assert tab.persistence_status_label.text() == "Persisted"
    assert tab.persistence_file_label.text() == "run.db"
    assert tab.persistence_size_label.text().startswith("Size · ")
    assert tab._last_summary.run_id in tab.persistence_size_label.text()
    assert tab.persist_size_label.text().startswith("Size · ")
    assert tab.open_saved_run_button.isEnabled()

    tab.persist_checkbox.setChecked(False)
    # A finished, persisted run keeps its state until the next run starts.
    assert tab.persistence_status_label.text() == "Persisted"


def test_rail_card_without_persistence(app: QApplication) -> None:
    tab = _make_tab(app)
    assert tab.persistence_status_label.text() == "Will be persisted"
    tab.persist_checkbox.setChecked(False)
    assert tab.persistence_status_label.text() == "Not persisted"
    assert tab.persistence_file_label.text() == ""
    assert not tab.persist_path.isEnabled()


def test_saved_run_reopens_with_cards_report_and_chip(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    source = _persisted_run(app, monkeypatch, tmp_path)
    summary = source._last_summary

    tab = _make_tab(app)
    assert tab.open_saved_run(tmp_path / "run.db") is True

    assert tab.saved_run_chip.isVisibleTo(tab)
    assert "run.db" in tab.saved_run_chip_label.text()
    assert tab.live_title_label.text() == "brands.get — QA"
    assert tab.live_status_label.text() == "PASS"
    assert tab.report_outcome_label.text() == "PASS"
    assert tab.stat_cards["throughput"].title_label.text() == "THROUGHPUT · PEAK"
    assert tab.stat_cards["completed"].value_label.text() == f"{summary.total_requests:,}"
    assert tab.persistence_status_label.text() == "Loaded from file"
    assert tab.report_view.stage_table.rowCount() == 1
    assert tab.throughput_chart._values
    assert tab.steps.currentIndex() == 3

    tab.close_saved_run()
    assert not tab.saved_run_chip.isVisibleTo(tab)
    assert tab.live_status_label.text() == "READY"
    assert tab.stat_cards["completed"].value_label.text() == "–"
    assert tab.persistence_status_label.text() == "Will be persisted"


def test_saved_run_picker_is_used_when_a_file_has_several_runs(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    source = _persisted_run(app, monkeypatch, tmp_path)
    first_id = source._last_summary.run_id
    _run_to_completion(source, app)
    second_id = source._last_summary.run_id
    assert first_id != second_id

    tab = _make_tab(app)
    offered: list[list[str]] = []

    def choose(path, runs):
        offered.append([run["run_id"] for run in runs])
        return first_id

    monkeypatch.setattr(tab, "_choose_saved_run", choose)
    assert tab.open_saved_run(tmp_path / "run.db") is True
    assert sorted(offered[0]) == sorted([first_id, second_id])
    assert tab._loaded_run is not None and tab._loaded_run.run_id == first_id

    monkeypatch.setattr(tab, "_choose_saved_run", lambda path, runs: None)
    assert tab.open_saved_run(tmp_path / "run.db") is False
    assert tab._loaded_run.run_id == first_id


def test_reuse_scenario_restores_stages_limits_and_thresholds(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path, quiet_dialogs
) -> None:
    _persisted_run(app, monkeypatch, tmp_path)
    tab = _make_tab(app)
    assert tab.stages_table.rowCount() == 4  # default ramp
    assert tab.environment_permits_checkbox.isChecked() is False
    tab.open_saved_run(tmp_path / "run.db")

    monkeypatch.setattr(tab, "_ask_reuse_scenario", lambda: "reuse")
    tab._configure_clicked()

    assert tab.steps.currentIndex() == 0
    assert tab.stages_table.rowCount() == 1
    assert tab._stage_from_row(0).duration_seconds == pytest.approx(0.4)
    assert tab._stage_from_row(0).label == "Hold"
    assert tab.environment_permits_checkbox.isChecked() is True
    assert tab.thresholds_table.rowCount() == 1
    assert tab._threshold_from_row(0).metric == "error_rate"
    assert tab.current_endpoint is not None and tab.current_endpoint.id == "brands.get"
    assert tab.plan is None


def test_configure_on_saved_run_can_be_cancelled(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _persisted_run(app, monkeypatch, tmp_path)
    tab = _make_tab(app)
    tab.open_saved_run(tmp_path / "run.db")
    monkeypatch.setattr(tab, "_ask_reuse_scenario", lambda: "cancel")
    tab._configure_clicked()
    assert tab.steps.currentIndex() == 3
    assert tab.stages_table.rowCount() == 4


def test_opening_a_saved_run_confirms_before_discarding_unsaved_results(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _persisted_run(app, monkeypatch, tmp_path, name="saved.db")
    _install_fake_transport(monkeypatch)
    tab = _make_tab(app)
    _make_fast_scenario(tab)
    tab.persist_checkbox.setChecked(False)
    _run_to_completion(tab, app)
    assert tab.persistence_status_label.text() == "Not persisted"

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No))
    assert tab.open_saved_run(tmp_path / "saved.db") is False
    assert tab._loaded_run is None

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    assert tab.open_saved_run(tmp_path / "saved.db") is True


def test_opening_an_invalid_file_warns(app: QApplication, tmp_path, quiet_dialogs) -> None:
    tab = _make_tab(app)
    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(b"not sqlite" * 200)
    assert tab.open_saved_run(bogus) is False
    assert tab.open_saved_run(tmp_path / "missing.db") is False
    assert tab._loaded_run is None


def test_starting_a_run_leaves_the_saved_run_view(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    tab = _persisted_run(app, monkeypatch, tmp_path)
    tab.open_saved_run(tmp_path / "run.db")
    assert tab._loaded_run is not None

    _run_to_completion(tab, app)

    assert tab._loaded_run is None
    assert not tab.saved_run_chip.isVisibleTo(tab)
    assert tab.persistence_status_label.text() == "Persisted"


def test_safety_columns_stack_at_the_data_runner_breakpoint(app: QApplication) -> None:
    """Inside the main window this page is ~900 px at minimum, so the breakpoint
    must sit near the Data Runner source page's 1040 px for stacking to happen."""
    from api_tester.data_runner.ui import _SOURCE_STACK_THRESHOLD

    tab = _make_tab(app)
    tab.steps.setCurrentIndex(1)
    columns = tab.safety_columns
    assert abs(columns.threshold() - _SOURCE_STACK_THRESHOLD) <= 40
    tab.show()
    for width, stacked in ((1400, False), (950, True), (1400, False)):
        tab.resize(width, 800)
        app.processEvents()
        assert columns.is_stacked() is stacked, width



# ------------------------------------------------------------------ PDF export & findings


def test_export_is_disabled_until_a_run_finishes(app: QApplication) -> None:
    tab = _make_tab(app)
    assert not tab.report_export_button.isEnabled()
    assert not hasattr(tab, "export_pdf_button")
    assert not tab.report_copy_button.isEnabled()
    with pytest.raises(RuntimeError):
        tab.export_report_pdf("never.pdf")


def test_persisted_run_exports_a_full_report_with_findings(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    tab = _persisted_run(app, monkeypatch, tmp_path)
    assert tab.report_export_button.isEnabled()
    assert tab.report_actions_label.text().startswith("Full report")
    assert tab.report_view.finding_cards
    assert "F1" in tab.report_summary_text()

    result = tab.export_report_pdf(tmp_path / "report.pdf")
    assert result.path.exists() and result.page_count >= 4
    joined = "\n".join(result.texts)
    assert "HTTP status distribution" in joined
    assert "per-request detail is not available" not in joined


def test_unpersisted_run_exports_a_reduced_report(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _install_fake_transport(monkeypatch)
    _fast_snapshot_options(monkeypatch)
    tab = _make_tab(app)
    _make_fast_scenario(tab)
    tab.persist_checkbox.setChecked(False)
    _run_to_completion(tab, app)
    assert tab.report_actions_label.text().startswith("Reduced report")
    result = tab.export_report_pdf(tmp_path / "reduced.pdf")
    assert result.page_count >= 1
    assert any("not persisted" in text for text in result.texts)


def test_saved_run_exports_and_closing_disables_export(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _persisted_run(app, monkeypatch, tmp_path)
    tab = _make_tab(app)
    assert tab.open_saved_run(tmp_path / "run.db") is True
    assert tab.report_export_button.isEnabled()
    result = tab.export_report_pdf(tmp_path / "saved.pdf")
    assert result.path.exists()
    assert tab._default_export_path().endswith(".pdf")

    tab.close_saved_run()
    assert not tab.report_export_button.isEnabled()
    assert tab.report_view.finding_cards == []


def test_copy_summary_puts_findings_on_the_clipboard(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    tab = _persisted_run(app, monkeypatch, tmp_path)
    tab._copy_report_summary()
    assert "F1" in QApplication.clipboard().text()
    assert tab.report_actions_label.text() == "Summary copied to the clipboard."


def test_export_button_writes_the_pdf_through_the_dialog(
    app: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from PyQt6.QtWidgets import QDialog, QFileDialog

    from api_tester.load_testing import ui as ui_module
    from api_tester.load_testing.export_dialog import PdfExportDialog

    tab = _persisted_run(app, monkeypatch, tmp_path)
    target = tmp_path / "out" / "clicked"
    opened: list[str] = []
    monkeypatch.setattr(PdfExportDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), "")))
    monkeypatch.setattr(ui_module.QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toLocalFile())))

    tab._export_pdf_clicked()

    written = tmp_path / "out" / "clicked.pdf"
    assert written.exists()
    assert opened and opened[0].endswith("clicked.pdf")
    assert "clicked.pdf" in tab.report_actions_label.text()
    assert tab._default_export_path().startswith(str(tmp_path / "out"))


def test_status_dot_sits_inside_the_status_pill(app: QApplication) -> None:
    tab = _make_tab(app)
    assert tab.live_status_icon.parentWidget() is tab.live_status_pill
    assert tab.live_status_label.parentWidget() is tab.live_status_pill
    tab._set_live_status("PASS", "pass")
    assert tab.live_status_pill.property("status") == "pass"
    assert tab.live_status_label.property("status") == "pass"
    assert tab.live_status_label.text() == "PASS"


def test_live_stage_label_tracks_the_stage_in_progress(app: QApplication) -> None:
    """Every stage after the active one used to overwrite the index, so the
    header always named the last stage while the run was still mid-way."""
    from api_tester.load_testing.scenario import LoadStage

    tab = _make_tab(app)
    stages = (
        LoadStage(kind="ramp_up", duration_seconds=6, start_users=1, end_users=12, label="Warm up"),
        LoadStage(kind="steady", duration_seconds=8, start_users=12, end_users=12, label="Hold"),
        LoadStage(kind="ramp_down", duration_seconds=4, start_users=12, end_users=1, label="Cool down"),
    )
    tab._view_stages = stages
    tab._rebuild_stage_progress(stages)

    for elapsed, expected in ((0.0, "Stage 1 of 3 · Warm up"), (12.0, "Stage 2 of 3 · Hold"),
                              (15.0, "Stage 3 of 3 · Cool down"), (99.0, "Stage 3 of 3 · Cool down")):
        tab._update_stage_progress(elapsed)
        assert tab.live_stage_label.text() == expected, elapsed
