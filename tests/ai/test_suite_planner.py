"""Offline tests for AI test-suite generation (no model or network needed)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from api_tester.ai.catalog_index import CatalogIndex
from api_tester.ai.config import AiSettings, build_provider
from api_tester.ai.planner import SuitePlanner
from api_tester.ai.provider import ChatMessage, LlmResponse
from api_tester.ai.providers.fake import FakeProvider
from api_tester.ai.request_plan import RequestPlan
from api_tester.ai.suite_plan import (
    SuitePlan,
    response_path_problem,
    suite_plan_schema,
    to_test_suite,
    validate_suite_plan,
)
from api_tester.ai.validation import validate_request_plan
from api_tester.catalog import load_catalog
from api_tester.suite import ASSERTION_KINDS

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_LIST = "f76aeb49ca37"
PRODUCT_BY_ID = "870dcc89426b"
PRODUCT_CREATE = "a31051c8dfa2"
PRODUCT_PATCH = "81ef2e2170c3"
PRODUCT_DELETE = "5aedafc7c6b3"


@pytest.fixture(scope="module")
def index() -> CatalogIndex:
    return CatalogIndex(load_catalog(ROOT / "examples" / "store_catalog.json"))


def _case(key: str, endpoint_id: str, **values: Any) -> dict[str, Any]:
    base = {
        "key": key, "name": key.title(), "endpoint_id": endpoint_id, "parameters": [], "filters": [],
        "filter_join": "and", "sort": [], "payload": None, "expected_status": "200-299", "assertions": [],
        "captures": [], "depends_on": [], "phase": "normal", "always_run": False,
    }
    base.update(values)
    return base


def _lifecycle() -> dict[str, Any]:
    by_id = [{"name": "id", "value": "{{productId}}"}]
    return {
        "status": "plan", "name": "Product lifecycle", "description": "Create, read, rename and delete a product.",
        "variables": [], "stop_on_failure": True,
        "cases": [
            _case(
                "create", PRODUCT_CREATE, expected_status="201",
                payload={"Name": "AI Test Lamp", "Sku": "AI-1", "CategoryId": 1, "Price": 9.5, "Status": "active"},
                assertions=[{"kind": "json_equals", "path": "$.Name", "value": "AI Test Lamp"}],
                captures=[{"name": "productId", "path": "$.Id", "source": "body"}],
            ),
            _case(
                "read", PRODUCT_BY_ID, parameters=by_id, depends_on=["create"],
                assertions=[{"kind": "json_equals", "path": "$.Sku", "value": "AI-1"}],
            ),
            _case("rename", PRODUCT_PATCH, parameters=by_id, depends_on=["create"], payload={"Name": "AI Test Lamp 2"}),
            _case("delete", PRODUCT_DELETE, parameters=by_id, depends_on=["create"], phase="cleanup"),
        ],
        "assumptions": [], "warnings": [], "question": "", "choices": [],
    }


def _response(plan: dict[str, Any]) -> LlmResponse:
    return LlmResponse(text=json.dumps(plan), json=plan, input_tokens=100, output_tokens=50, model="fake")


def _codes(result) -> set[str]:
    return {item.code for item in result.issues}


# -- retrieval and schema ---------------------------------------------------------
def test_candidates_offer_the_whole_resource_lifecycle(index):
    planner = SuitePlanner(FakeProvider(), index, AiSettings())
    offered, detailed = planner.candidates("regression suite for products")
    ids = {item.id for item in offered}
    assert {PRODUCT_LIST, PRODUCT_BY_ID, PRODUCT_CREATE, PRODUCT_PATCH, PRODUCT_DELETE} <= ids
    assert {PRODUCT_CREATE, PRODUCT_BY_ID, PRODUCT_DELETE} <= {item.id for item in detailed}


def test_suite_cards_describe_the_response_shape(index):
    card = index.card(index.get(PRODUCT_BY_ID), include_response=True)
    assert "response: {Id: integer, Name: text" in card
    assert "response:" not in index.card(index.get(PRODUCT_BY_ID))


def test_undeclared_response_falls_back_to_the_list_filter_fields():
    catalog = load_catalog(ROOT / "data" / "api_catalog.json")
    trial = CatalogIndex(catalog)
    by_id = next(
        item
        for service in catalog.services
        for item in service.endpoints
        if item.method == "GET" and item.path == "/Brand/{id}" and not item.response_schema
    )
    card = trial.card(by_id, include_response=True)
    assert "not declared in the catalog; the record likely has: Id, Name" in card


def test_schema_limits_endpoints_and_assertion_kinds():
    schema = suite_plan_schema([PRODUCT_LIST, PRODUCT_CREATE])
    case = schema["properties"]["cases"]["items"]
    assert case["properties"]["endpoint_id"]["enum"] == [PRODUCT_LIST, PRODUCT_CREATE]
    assert case["properties"]["assertions"]["items"]["properties"]["kind"]["enum"] == list(ASSERTION_KINDS)
    assert set(case["required"]) == set(case["properties"])


# -- validation and conversion -------------------------------------------------------
def test_valid_lifecycle_converts_into_a_runnable_suite(index):
    result = validate_suite_plan(SuitePlan.from_json(_lifecycle()), index)
    assert result.ok, [item.as_text() for item in result.issues]
    plan = result.plan
    assert plan.cases[0].payload["Status"] == "Active"
    assert plan.cases[2].payload == [{"op": "replace", "path": "/Name", "value": "AI Test Lamp 2"}]
    assert any("RFC 6902" in text for text in plan.assumptions)
    assert any("1 POST, 1 PATCH, 1 DELETE" in text for text in plan.warnings)

    suite = to_test_suite(plan, index)
    create, read, rename, delete = suite.cases
    assert suite.name == "Product lifecycle" and suite.stop_on_failure
    assert create.expected_status == "201"
    assert create.captures[0].name == "productId" and create.captures[0].path == "$.Id"
    assert read.depends_on == [create.id] and delete.depends_on == [create.id]
    assert read.values["path:id"] == "{{productId}}"
    assert read.payload is None
    assert delete.phase == "cleanup" and delete.always_run
    assert len({case.id for case in suite.cases}) == 4


def test_missing_dependency_for_a_captured_variable_is_added(index):
    raw = _lifecycle()
    raw["cases"][1]["depends_on"] = []
    result = validate_suite_plan(SuitePlan.from_json(raw), index)
    assert result.ok
    assert result.plan.cases[1].depends_on == ("create",)
    assert any("captures {{productId}}" in text for text in result.plan.assumptions)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda raw: raw["cases"][0]["assertions"].append({"kind": "status", "path": "", "value": "201"}), "unknown_assertion"),
        (lambda raw: raw["cases"][1].update(depends_on=["nope"]), "unknown_dependency"),
        (lambda raw: raw["cases"][1].update(parameters=[{"name": "id", "value": "{{other}}"}]), "undefined_variable"),
        (lambda raw: raw["cases"][1].update(parameters=[]), "missing_path_parameter"),
        (
            lambda raw: raw["cases"][1]["assertions"].append({"kind": "json_equals", "path": "$.Id", "value": "{{productId}}"}),
            "variable_in_assertion",
        ),
        (
            lambda raw: raw["cases"][1]["assertions"].append({"kind": "json_exists", "path": "$.data.id", "value": ""}),
            "unknown_response_path",
        ),
        (lambda raw: raw["cases"][0]["captures"].__setitem__(0, {"name": "productId", "path": "$.Identifier", "source": "body"}), "unknown_response_path"),
        (
            lambda raw: raw["cases"][1]["assertions"].append({"kind": "json_length_gte", "path": "$.Tags", "value": "many"}),
            "invalid_value",
        ),
        (lambda raw: raw["cases"][2].update(payload=[{"op": "rename", "path": "/Name", "value": "x"}]), "invalid_patch"),
        (lambda raw: raw["cases"][1].update(key="create"), "duplicate_key"),
        (lambda raw: raw["cases"][1].update(depends_on=["delete"]), "depends_on_cleanup"),
        (lambda raw: raw["cases"][0].update(depends_on=["read"]), "dependency_cycle"),
        (lambda raw: raw.update(cases=[]), "no_cases"),
    ],
)
def test_validator_reports_problems_the_model_can_fix(index, mutate, code):
    raw = copy.deepcopy(_lifecycle())
    mutate(raw)
    result = validate_suite_plan(SuitePlan.from_json(raw), index)
    assert code in _codes(result), [item.as_text() for item in result.issues]


def test_unknown_assertion_kind_explains_expected_status(index):
    raw = _lifecycle()
    raw["cases"][0]["assertions"].append({"kind": "status_code", "path": "", "value": "201"})
    result = validate_suite_plan(SuitePlan.from_json(raw), index)
    assert any("expected_status" in item.message for item in result.issues)


def test_error_cases_skip_response_path_checks(index):
    raw = _lifecycle()
    raw["cases"].append(
        _case(
            "gone", PRODUCT_BY_ID, parameters=[{"name": "id", "value": "{{productId}}"}], depends_on=["delete"],
            phase="cleanup", expected_status="404",
            assertions=[{"kind": "json_exists", "path": "$.title", "value": ""}],
        )
    )
    result = validate_suite_plan(SuitePlan.from_json(raw), index)
    assert result.ok, [item.as_text() for item in result.issues]


def test_generic_response_wrappers_are_checked_only_at_the_top_level():
    schema = {
        "kind": "object", "clr_type": "IEntitiesDto<BrandResponse>",
        "fields": [{"name": "Values", "kind": "array", "item": {"kind": "object", "fields": [{"name": "Id", "kind": "integer"}]}}],
    }
    assert response_path_problem(schema, "$.Values[0].CropName") is None
    assert "no field 'Items'" in response_path_problem(schema, "$.Items")
    concrete = dict(schema, clr_type="ProductList")
    assert "no field 'CropName'" in response_path_problem(concrete, "$.Values[0].CropName")
    assert response_path_problem(concrete, "$.Values.Id") is None


def test_request_validator_keeps_variables_untouched(index):
    plan = RequestPlan(endpoint_id=PRODUCT_BY_ID, parameters=(("id", "{{productId}}"),))
    result = validate_request_plan(plan, index)
    assert result.ok and result.plan.parameters == (("id", "{{productId}}"),)


def test_request_validator_rewrites_a_partial_patch_body(index):
    plan = RequestPlan(endpoint_id=PRODUCT_PATCH, parameters=(("id", "7"),), payload={"Price": 12})
    result = validate_request_plan(plan, index)
    assert result.ok
    assert result.plan.payload == [{"op": "replace", "path": "/Price", "value": 12}]


# -- planner ------------------------------------------------------------------------
@pytest.mark.parametrize("invalid_assertion", [False, True])
def test_planner_deduplicates_unseen_endpoints_by_id(index, monkeypatch, invalid_assertion):
    fixed = _lifecycle()
    repeated = copy.deepcopy(fixed["cases"][1])
    repeated["key"] = "read_again"
    fixed["cases"].append(repeated)
    initial = copy.deepcopy(fixed)
    if invalid_assertion:
        initial["cases"][1]["assertions"][0] = {
            "kind": "json_equals", "path": "$.Id", "value": "{{productId}}",
        }
    fake = FakeProvider([_response(initial), _response(fixed)])
    events = []
    planner = SuitePlanner(fake, index, AiSettings(max_repairs=1), progress=events.append)
    endpoints = [index.get(item) for item in (PRODUCT_CREATE, PRODUCT_BY_ID, PRODUCT_PATCH, PRODUCT_DELETE)]
    assert isinstance(endpoints[0].payload, dict)
    monkeypatch.setattr(planner, "candidates", lambda _prompt: (endpoints, []))

    outcome = planner.plan("product lifecycle")

    assert outcome.ok and outcome.attempts == 2
    assert len(fake.requests) == 2
    follow_up = fake.requests[1]["messages"][-1].content
    cards = follow_up.split("Cards for endpoints you used without seeing them:\n\n", 1)[1]
    assert cards == "\n\n".join(planner._card(item) for item in endpoints)
    assert any("4 endpoints need detailed catalog cards" in event for event in events)
    if invalid_assertion:
        assert "cases[1].assertions[0].value" in follow_up
        assert "assertion values are compared literally" in follow_up
        assert any("variable_in_assertion" in event for event in events)


def test_planner_repairs_an_invalid_suite(index):
    broken = _lifecycle()
    broken["cases"][1]["assertions"] = [{"kind": "json_equals", "path": "$.data.sku", "value": "AI-1"}]
    fake = FakeProvider([_response(broken), _response(_lifecycle())])
    outcome = SuitePlanner(fake, index, AiSettings(), secrets=("tok-123",)).plan(
        "regression suite for products with token tok-123"
    )
    assert outcome.ok and outcome.attempts == 2
    assert outcome.input_tokens == 200
    repair = fake.requests[1]["messages"][-1].content
    assert "no field 'data'" in repair
    assert "tok-123" not in json.dumps([item.content for item in fake.requests[0]["messages"]])
    assert fake.requests[0]["response_schema"]["properties"]["cases"]["items"]["properties"]["endpoint_id"]["enum"]


def test_planner_returns_unsupported_without_calling_the_model(index):
    fake = FakeProvider()
    outcome = SuitePlanner(fake, index, AiSettings()).plan("zzzz qqqq")
    assert outcome.plan.status == "unsupported" and not fake.requests


def test_ollama_requests_the_configured_context_window():
    provider = build_provider(AiSettings(context_window=16384))
    body = provider.build_body(
        [ChatMessage("user", "hi")], model="m", tools=None, response_schema=None, temperature=0, max_output_tokens=10
    )
    assert body["options"]["num_ctx"] == 16384
    assert AiSettings.from_dict({"context_window": 100}).context_window == 2048


# -- dialog and main window ------------------------------------------------------------
@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _wait_for(predicate, attempts: int = 400) -> None:
    import time

    from PyQt6.QtCore import QCoreApplication

    for _ in range(attempts):
        QCoreApplication.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_dialog_suite_mode_previews_and_opens_in_test_suites(qt_app, monkeypatch, tmp_path):
    from api_tester.ai import controller
    from api_tester.ai.ui import AskAiDialog

    fake = FakeProvider([_response(_lifecycle())])
    monkeypatch.setattr(controller, "build_provider", lambda _settings: fake)
    catalog = load_catalog(ROOT / "examples" / "store_catalog.json")
    opened = []
    dialog = AskAiDialog(
        None, catalog=lambda: catalog, secrets=lambda: (), on_open=lambda _r: None,
        settings_path=tmp_path / "ai.json", on_open_suite=opened.append,
    )
    try:
        assert dialog.open_button.text() == "Open in API Explorer"
        dialog.mode_selector.setCurrentIndex(dialog.mode_selector.findData("suite"))
        assert dialog.mode == "suite" and dialog.open_button.text() == "Open in Test Suites"
        dialog.prompt_input.setPlainText("regression suite for products")
        dialog.ask()
        _wait_for(lambda: dialog._task is None and dialog.suite_outcome is not None)
        preview = dialog.result_view.toPlainText()
        assert "Product lifecycle" in preview
        assert "4 cases · 1 cleanup · stops on first failure" in preview
        assert "capture: {{productId}} \u2190 $.Id" in preview
        assert "after Create" in preview
        dialog.open_button.click()
        assert len(opened) == 1 and len(opened[0].cases) == 4
    finally:
        dialog.close()
        dialog.deleteLater()


def test_suite_mode_is_disabled_without_a_suite_handler(qt_app, tmp_path):
    from api_tester.ai.ui import AskAiDialog

    dialog = AskAiDialog(
        None, catalog=lambda: None, secrets=lambda: (), on_open=lambda _r: None, settings_path=tmp_path / "ai.json"
    )
    try:
        dialog.set_mode("suite")
        assert dialog.mode == "request"
        selector = dialog.mode_selector
        suite_item = selector.model().item(selector.findData("suite"))
        assert not suite_item.isEnabled()
        assert selector.currentData() == "request"
    finally:
        dialog.deleteLater()


def test_main_window_replaces_or_appends_the_ai_suite(qt_app, monkeypatch, tmp_path):
    import api_tester.main as main_module
    from api_tester.suite import TestCase, TestSuite

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(main_module, "AI_SETTINGS_PATH", tmp_path / "ai_settings.json")
    window = main_module.MainWindow()
    try:
        endpoint_id = window.catalog.services[0].endpoints[0].id

        def ai_suite() -> TestSuite:
            return TestSuite(name="AI suite", variables={"x": "1"}, cases=[TestCase(endpoint_id, name="AI case")])

        window.suite_tab.suite = TestSuite(name="Mine", cases=[TestCase(endpoint_id, name="Mine case")])
        window.suite_tab._load_suite_into_ui()
        monkeypatch.setattr(window, "_ai_suite_choice", lambda: None)
        window.open_ai_suite(ai_suite())
        assert [case.name for case in window.suite_tab.suite.cases] == ["Mine case"]

        monkeypatch.setattr(window, "_ai_suite_choice", lambda: "append")
        window.open_ai_suite(ai_suite())
        assert window.suite_tab.suite.name == "Mine"
        assert [case.name for case in window.suite_tab.suite.cases] == ["Mine case", "AI case"]
        assert window.suite_tab.suite.variables == {"x": "1"}
        assert window.navigation.currentRow() == 1

        monkeypatch.setattr(window, "_ai_suite_choice", lambda: "replace")
        window.open_ai_suite(ai_suite())
        assert window.suite_tab.suite.name == "AI suite"
        assert window.suite_tab.suite_name.text() == "AI suite"
        assert len(window.suite_tab.suite.cases) == 1
    finally:
        window.close()
        window.deleteLater()
