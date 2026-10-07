"""Offline tests for the Ask AI request planner (no model or network needed)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from api_tester.ai.catalog_index import CatalogIndex
from api_tester.ai.config import AiSettings, load_ai_settings, save_ai_settings
from api_tester.ai.planner import RequestPlanner
from api_tester.ai.provider import ChatMessage, LlmResponse, LlmResponseError
from api_tester.ai.providers.fake import FakeProvider
from api_tester.ai.providers.ollama import OllamaProvider
from api_tester.ai.redaction import redact_text
from api_tester.ai.request_plan import FilterSpec, RequestPlan, SortSpec, request_plan_schema, to_explorer_request
from api_tester.ai.validation import validate_request_plan
from api_tester.catalog import load_catalog

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_LIST = "f76aeb49ca37"
PRODUCT_BY_ID = "870dcc89426b"
PRODUCT_CREATE = "a31051c8dfa2"
PRODUCT_COUNT = "fa32a625a196"
CUSTOMER_LIST = "e2e737b18d37"


@pytest.fixture(scope="module")
def index() -> CatalogIndex:
    return CatalogIndex(load_catalog(ROOT / "examples" / "store_catalog.json"))


def _plan(**values: Any) -> dict[str, Any]:
    base = {
        "status": "plan", "title": "t", "summary": "s", "endpoint_id": PRODUCT_LIST, "parameters": [],
        "filters": [], "filter_join": "and", "sort": [], "payload": None, "expected_status": "200-299",
        "assumptions": [], "warnings": [], "question": "", "choices": [],
    }
    base.update(values)
    return base


def _response(plan: dict[str, Any], tokens: int = 10) -> LlmResponse:
    return LlmResponse(text=json.dumps(plan), json=plan, input_tokens=tokens, output_tokens=tokens, model="fake")


# -- retrieval -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("list active products sorted by price", PRODUCT_LIST),
        ("get product 42", PRODUCT_BY_ID),
        ("create a new product", PRODUCT_CREATE),
        ("how many products are there", PRODUCT_COUNT),
        ("show the 20 newest products", PRODUCT_LIST),
        ("customers whose name starts with A", CUSTOMER_LIST),
    ],
)
def test_search_ranks_the_intended_endpoint_first(index, prompt, expected):
    assert index.search(prompt)[0].endpoint.id == expected


def test_card_lists_filter_fields_operators_and_lov(index):
    card = index.card(index.get(PRODUCT_LIST))
    assert "Status[LOV Draft|Active|Discontinued: eq,neq]" in card
    assert "sortable" in card and "Price" in card


# -- validation ------------------------------------------------------------------
def test_validator_canonicalises_case_aliases_and_types(index):
    plan = RequestPlan.from_json(
        _plan(
            parameters=[{"name": "pagesize", "value": "a20"}],
            filters=[
                {"field": "status", "op": "=", "values": ["active"]},
                {"field": "Name", "op": "contains", "values": ["corn"]},
            ],
            sort=[{"field": "price", "descending": True}],
        )
    )
    result = validate_request_plan(plan, index)
    assert result.ok, result.issues
    assert result.plan.parameters == (("PageSize", "20"),)
    assert result.plan.filters == (
        FilterSpec("Status", "eq", ("Active",)),
        FilterSpec("Name", "ct", ("corn",)),
    )
    assert result.plan.sort == (SortSpec("Price", True),)


def test_validator_reports_unknown_fields_values_and_operators(index):
    plan = RequestPlan.from_json(
        _plan(
            filters=[
                {"field": "Colour", "op": "eq", "values": ["red"]},
                {"field": "Status", "op": "eq", "values": ["Retired"]},
                {"field": "Status", "op": "ct", "values": ["Act"]},
                {"field": "Price", "op": "bt", "values": ["1"]},
            ],
            sort=[{"field": "Description", "descending": False}],
        )
    )
    result = validate_request_plan(plan, index)
    codes = [issue.code for issue in result.issues]
    assert len(codes) == 5
    assert not result.plan.filters and not result.plan.sort
    messages = " ".join(issue.message for issue in result.issues)
    assert "Draft" in messages and "Price" in messages  # valid options are quoted for the repair round


def test_validator_decodes_raw_filter_text(index):
    plan = RequestPlan.from_json(_plan(parameters=[{"name": "Filter", "value": "Status__eq:=Active;Price__gt:=10"}]))
    result = validate_request_plan(plan, index)
    assert result.ok
    assert [item.field for item in result.plan.filters] == ["Status", "Price"]


def test_validator_drops_body_on_reads_and_checks_payload_fields(index):
    read = validate_request_plan(RequestPlan.from_json(_plan(payload={"Name": "x"})), index)
    assert read.plan.payload is None
    create = validate_request_plan(
        RequestPlan.from_json(_plan(endpoint_id=PRODUCT_CREATE, payload={"Name": "x", "Colour": "red", "status": "draft"})),
        index,
    )
    assert [issue.code for issue in create.issues] == ["unknown_payload_field"]
    assert any("changes data" in warning for warning in create.plan.warnings)


def test_explorer_request_encodes_filters_and_disables_unused_query_params(index):
    plan = RequestPlan.from_json(
        _plan(
            parameters=[{"name": "PageSize", "value": "5"}],
            filters=[{"field": "Status", "op": "eq", "values": ["Active"]}, {"field": "Price", "op": "bt", "values": ["1", "9"]}],
            sort=[{"field": "Name", "descending": False}, {"field": "Price", "descending": True}],
        )
    )
    endpoint = index.get(PRODUCT_LIST)
    request = to_explorer_request(validate_request_plan(plan, index).plan, endpoint, True)
    assert request.values["query:Filter"] == "Status__eq:=Active;Price__bt:=1,9"
    assert request.values["query:Sort"] == "Name,Price-"
    assert request.values["query:PageSize"] == "5"
    assert request.values["enabled:query:ContinuationToken"] == "false"
    assert request.payload is None


def test_plan_schema_restricts_endpoint_to_candidates():
    schema = request_plan_schema(["abc", "def"])
    assert schema["properties"]["endpoint_id"]["enum"] == ["abc", "def", ""]
    assert set(schema["required"]) == set(schema["properties"])


# -- planner ---------------------------------------------------------------------
def test_planner_happy_path_uses_one_call(index):
    provider = FakeProvider([_response(_plan(filters=[{"field": "Status", "op": "eq", "values": ["Active"]}]))])
    outcome = RequestPlanner(provider, index, AiSettings()).plan("list active products")
    assert outcome.ok and outcome.attempts == 1
    assert outcome.plan.filters[0].values == ("Active",)
    request = provider.requests[0]
    assert request["response_schema"]["properties"]["endpoint_id"]["enum"][0] == PRODUCT_LIST
    assert request["temperature"] == 0.0


def test_planner_repairs_with_the_validation_issues(index):
    provider = FakeProvider(
        [
            _response(_plan(filters=[{"field": "Status", "op": "eq", "values": ["Live"]}])),
            _response(_plan(filters=[{"field": "Status", "op": "eq", "values": ["Active"]}])),
        ]
    )
    outcome = RequestPlanner(provider, index, AiSettings()).plan("list live products")
    assert outcome.ok and outcome.attempts == 2
    repair = provider.requests[1]["messages"][-1].content
    assert "'Live' is not a valid Status" in repair and "Active" in repair
    assert outcome.input_tokens == 20


def test_planner_asks_once_for_a_missing_path_parameter(index):
    provider = FakeProvider(
        [
            _response(_plan(endpoint_id=PRODUCT_BY_ID)),
            _response(_plan(endpoint_id=PRODUCT_BY_ID, parameters=[{"name": "id", "value": "42"}])),
        ]
    )
    outcome = RequestPlanner(provider, index, AiSettings()).plan("get product 42")
    assert outcome.plan.parameters == (("id", "42"),)
    assert "id" in provider.requests[1]["messages"][-1].content


def test_planner_recovers_from_invalid_json(index):
    provider = FakeProvider([LlmResponseError("not JSON"), _response(_plan())])
    outcome = RequestPlanner(provider, index, AiSettings()).plan("list products")
    assert outcome.ok and outcome.attempts == 2


def test_planner_gives_up_after_max_repairs(index):
    bad = _plan(sort=[{"field": "Colour", "descending": False}])
    provider = FakeProvider([_response(bad), _response(bad), _response(bad)])
    outcome = RequestPlanner(provider, index, AiSettings(max_repairs=2)).plan("products by colour")
    assert not outcome.ok and outcome.attempts == 3
    assert len(provider.requests) == 3


def test_planner_passes_clarify_questions_through(index):
    plan = _plan(status="clarify", endpoint_id="", question="Which list?", choices=["Products", "Customers"])
    outcome = RequestPlanner(FakeProvider([_response(plan)]), index, AiSettings()).plan("list products or customers")
    assert outcome.plan.status == "clarify" and outcome.plan.choices == ("Products", "Customers")


def test_planner_skips_the_model_when_nothing_matches(index):
    provider = FakeProvider([])
    outcome = RequestPlanner(provider, index, AiSettings()).plan("list them")
    assert outcome.plan.status == "unsupported" and provider.requests == []


def test_planner_never_sends_secrets(index):
    secret = "s3cr3t-api-key-value"
    provider = FakeProvider([_response(_plan())])
    RequestPlanner(provider, index, AiSettings(), secrets=(secret,)).plan(
        f"list products using key {secret} and token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcDEF123_-x"
    )
    sent = json.dumps([message.content for message in provider.requests[0]["messages"]])
    assert secret not in sent and "eyJhbGci" not in sent
    assert "[REDACTED]" in sent


def test_planner_cancellation_stops_before_the_call(index):
    from api_tester.ai.planner import PlanCancelled

    provider = FakeProvider([])
    with pytest.raises(PlanCancelled):
        RequestPlanner(provider, index, AiSettings(), is_cancelled=lambda: True).plan("list products")
    assert provider.requests == []


# -- provider / settings ---------------------------------------------------------------
def test_ollama_body_and_response_parsing():
    provider = OllamaProvider("http://host:11434/", think=False)
    body = provider.build_body(
        [ChatMessage("system", "s"), ChatMessage("user", "u")],
        model="qwen3.6:latest", tools=None, response_schema={"type": "object"}, temperature=0.0, max_output_tokens=50,
    )
    assert provider.endpoint == "http://host:11434"
    assert body["format"] == {"type": "object"} and body["stream"] is False and body["think"] is False
    assert body["options"] == {"temperature": 0.0, "num_predict": 50}
    parsed = OllamaProvider.parse_response(
        {"model": "m", "message": {"content": '```json\n{"a": 1}\n```'}, "prompt_eval_count": 7, "eval_count": 3},
        expect_json=True,
    )
    assert parsed.json == {"a": 1} and parsed.input_tokens == 7 and parsed.output_tokens == 3
    with pytest.raises(LlmResponseError):
        OllamaProvider.parse_response({"message": {"content": "nope"}}, expect_json=True)


def test_settings_round_trip_and_clamping(tmp_path):
    path = tmp_path / "ai.json"
    save_ai_settings(path, AiSettings(planner_model="llama3", max_repairs=50))
    loaded = load_ai_settings(path)
    assert loaded.planner_model == "llama3"
    assert loaded.max_repairs == 50
    save_ai_settings(path, AiSettings(max_repairs=99))
    assert load_ai_settings(path).max_repairs == 50
    save_ai_settings(path, AiSettings(max_repairs=-1))
    assert load_ai_settings(path).max_repairs == 0
    assert load_ai_settings(tmp_path / "missing.json") == AiSettings()


def test_ai_settings_ui_allows_fifty_repair_rounds(tmp_path):
    from PyQt6.QtWidgets import QApplication, QWidget

    from api_tester.settings_ui import SettingsDialog

    app = QApplication.instance() or QApplication([])
    dialog = SettingsDialog(QWidget(), tmp_path / "ai.json")
    try:
        assert dialog.repairs_input.minimum() == 0
        assert dialog.repairs_input.maximum() == 50
        dialog.repairs_input.setValue(50)
        assert dialog.repairs_input.value() == 50
    finally:
        dialog.close()
        dialog.deleteLater()
        app.processEvents()


def test_redaction_keeps_ordinary_identifiers():
    text = "Use GetAllProductCategoriesByCropAndSeason with Bearer abc.def.ghi"
    redacted = redact_text(text, ())
    assert "GetAllProductCategoriesByCropAndSeason" in redacted
    assert "abc.def.ghi" not in redacted
