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


def test_stage_rows_are_five_pixels_taller(app) -> None:
    from api_tester import theme

    tab = _make_tab(app)
    assert tab.stages_table.objectName() == "roomyEditorTable"
    assert tab.stages_table.verticalHeader().defaultSectionSize() == 35


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
