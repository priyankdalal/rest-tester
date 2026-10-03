"""Offline coverage for AI draft hand-offs; no execution engines are started."""

from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.data_runner.csv_source import CsvImportSettings, CsvSource
from api_tester.data_runner.mapping import ColumnMapping, Transform
from api_tester.data_runner.ui import DataRunnerTab
from api_tester.execution.models import ExecutionEnvironmentSnapshot
from api_tester.load_testing.scenario import LoadStage, ThresholdDefinition
from api_tester.load_testing.ui import LoadTestingTab


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def catalog():
    endpoint = Endpoint(
        "brands.get", "TrialAuth", "Brands", "Get", "GET", "/Brands/{Id}",
        parameters=(Parameter("Id", "path", "int", True),),
    )
    return Catalog(
        (Service("TrialAuth", "TrialAuth", "https://example.test", (endpoint,)),),
        {}, {}, {},
    )


def environment():
    return ExecutionEnvironmentSnapshot(
        environment_id="qa", environment_name="QA",
        base_urls={"TrialAuth": "https://example.test"},
    )


def test_header_reader_rejects_ambiguous_columns(tmp_path: Path):
    path = tmp_path / "input.csv"
    for header in ("", "Id,Id", "Id,"):
        path.write_text(header + "\n42,43\n", encoding="utf-8")
        with pytest.raises(ValueError):
            CsvSource(CsvImportSettings(str(path))).read_columns()


def test_data_draft_preserves_mapping_and_does_not_start(app, catalog, tmp_path: Path):
    path = tmp_path / "input.csv"
    path.write_text("Id\n42\n", encoding="utf-8")
    source = CsvImportSettings(str(path), encoding="", delimiter="")
    mappings = (ColumnMapping("Id", "path:Id", (Transform("to_integer"),)),)
    tab = DataRunnerTab(catalog, environment)
    try:
        assert tab.load_ai_draft(
            catalog.services[0].endpoints[0], {}, None, source, ("Id",), mappings, "200",
        )
        assert tab._current_mappings() == list(mappings)
        assert tab.default_expected_status.text() == "200"
        assert tab.steps.currentIndex() == 1
        assert tab.runner is None and tab.plan is None
        assert not tab.run_button.isEnabled()
    finally:
        tab.shutdown()
        tab.close()


def test_changed_csv_is_rejected_before_overwriting_draft(app, catalog, tmp_path: Path):
    path = tmp_path / "input.csv"
    path.write_text("Other\n42\n", encoding="utf-8")
    tab = DataRunnerTab(catalog, environment)
    try:
        with pytest.raises(ValueError, match="CSV columns changed"):
            tab.load_ai_draft(
                catalog.services[0].endpoints[0], {}, None,
                CsvImportSettings(str(path)), ("Id",), (ColumnMapping("Id", "path:Id"),), "200",
            )
        assert tab.csv_path.text() == ""
    finally:
        tab.shutdown()
        tab.close()


def test_load_draft_preserves_exact_settings_and_clears_approvals(app, catalog):
    tab = LoadTestingTab(catalog, environment)
    try:
        tab.environment_permits_checkbox.setChecked(True)
        tab.confirmed_write_checkbox.setChecked(True)
        tab.confirm_checkbox.setChecked(True)
        stages = (LoadStage("steady", 30, 5, 5, 200),)
        thresholds = (ThresholdDefinition("p95_ms", "<=", 500),)
        assert tab.load_ai_draft(
            catalog.services[0].endpoints[0], {"path:Id": "42"}, None,
            name="AI draft", stages=stages, thresholds=thresholds, expected_status="200",
        )
        scenario = tab._build_scenario()
        assert scenario.stages == stages and scenario.thresholds == thresholds
        assert scenario.template.expected_status == "200"
        assert not scenario.limits.environment_permits_load_test
        assert not scenario.limits.confirmed_write_endpoints
        assert not tab.confirm_checkbox.isChecked()
        assert tab.engine is None and not tab.start_button.isEnabled()
    finally:
        tab.close()
