"""Catalog-aware AI workflow plans using a fake provider, never HTTP."""

import json
from dataclasses import replace

import pytest

from api_tester.ai.catalog_index import CatalogIndex
from api_tester.ai.config import AiSettings
from api_tester.ai.planner import PlanOutcome
from api_tester.ai.provider import LlmResponse
from api_tester.ai.providers.fake import FakeProvider
from api_tester.ai.workflow_plan import (
    WorkflowPlan, WorkflowPlanner, to_workflow_request, validate_workflow_plan,
)
from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.data_runner.mapping import ColumnMapping, Transform
from api_tester.load_testing.scenario import LoadStage, ThresholdDefinition


def index():
    endpoint = Endpoint(
        "brands.get", "TrialAuth", "Brands", "Get", "GET", "/Brands/{Id}",
        parameters=(Parameter("Id", "path", "int", True),),
    )
    return CatalogIndex(Catalog(
        (Service("TrialAuth", "TrialAuth", "https://example.test", (endpoint,)),),
        {}, {}, {},
    ))


def test_load_planner_preserves_stages_and_usage():
    catalog = index()
    draft = WorkflowPlan(
        mode="load", endpoint_id="brands.get", parameters=(("Id", "42"),),
        stages=(LoadStage("steady", 30, 5, 5, 200),),
        thresholds=(ThresholdDefinition("p95_ms", "<=", 500),),
    )
    value = draft.to_json()
    provider = FakeProvider([LlmResponse(
        text=json.dumps(value), json=value, input_tokens=12, output_tokens=4, model="fake",
    )])
    outcome = WorkflowPlanner(
        provider, catalog, AiSettings(max_repairs=0), mode="load",
    ).plan("load test brand 42")
    assert outcome.ok, outcome.issues
    assert isinstance(outcome.plan, WorkflowPlan)
    assert outcome.plan.stages == draft.stages
    assert outcome.plan.thresholds == draft.thresholds
    assert outcome.usage[0].total_tokens == 16


def test_data_path_mapping_does_not_require_a_fabricated_id():
    catalog = index()
    draft = WorkflowPlan(
        mode="data", endpoint_id="brands.get",
        mappings=(ColumnMapping("BrandId", "path:Id", (Transform("to_integer"),)),),
    )
    result = validate_workflow_plan(draft, catalog, ("BrandId",))
    assert result.ok, result.issues
    assert isinstance(result.plan, WorkflowPlan)
    request = to_workflow_request(result.plan, catalog)
    assert not request.values.get("path:Id")
    assert all("{{csv." not in value for value in request.values.values())


def test_unknown_csv_column_or_destination_is_blocked():
    catalog = index()
    draft = WorkflowPlan(
        mode="data", endpoint_id="brands.get",
        mappings=(ColumnMapping("BrandId", "path:Id"),),
    )
    assert validate_workflow_plan(draft, catalog, ("Other",)).issues
    invalid = replace(draft, mappings=(ColumnMapping("BrandId", "path:Missing"),))
    assert validate_workflow_plan(invalid, catalog, ("BrandId",)).issues


def test_load_bounds_and_unresolved_placeholders_are_blocked():
    catalog = index()
    draft = WorkflowPlan(
        mode="load", endpoint_id="brands.get", parameters=(("Id", "42"),),
        stages=(LoadStage("steady", 30, 5, 5),),
    )
    too_many_users = replace(draft, stages=(LoadStage("steady", 30, 100001, 100001),))
    assert validate_workflow_plan(too_many_users, catalog).issues
    unresolved = replace(draft, parameters=(("Id", "{{missing}}"),))
    assert validate_workflow_plan(unresolved, catalog).issues


def test_workflow_json_can_be_copied_from_the_standard_outcome():
    plan = WorkflowPlan(
        mode="load", endpoint_id="brands.get", parameters=(("Id", "42"),),
        stages=(LoadStage("steady", 30, 5, 5),),
    )
    value = PlanOutcome(plan).plan.to_json()
    assert value["mode"] == "load"
    assert value["stages"][0]["end_users"] == 5


def test_data_planner_receives_only_supplied_column_names():
    catalog = index()
    plan = WorkflowPlan(
        mode="data", endpoint_id="brands.get",
        mappings=(ColumnMapping("BrandId", "path:Id", (Transform("to_integer"),)),),
    )
    value = plan.to_json()
    provider = FakeProvider([LlmResponse(text=json.dumps(value), json=value, model="fake")])
    outcome = WorkflowPlanner(
        provider, catalog, AiSettings(max_repairs=0), mode="data", columns=("BrandId",),
    ).plan("Map the CSV to get each brand")
    assert outcome.ok, outcome.issues
    assert isinstance(outcome.plan, WorkflowPlan)
    assert outcome.plan.mappings == plan.mappings
    assert "BrandId" in "\n".join(
        message.content for message in provider.requests[0]["messages"]
    )
    assert provider.requests[0]["max_output_tokens"] == 4000


@pytest.mark.parametrize("override", [False, True])
def test_load_ai_cannot_override_native_sla_stage_defaults(override):
    plan = WorkflowPlan(
        mode="load", endpoint_id="brands.get", parameters=(("Id", "42"),),
        stages=(LoadStage("steady", 30, 5, 5, counts_toward_sla=override),),
    )
    result = validate_workflow_plan(plan, index())
    assert [(issue.path, issue.code) for issue in result.issues] == [
        ("stages[0].counts_toward_sla", "unsupported_sla_override"),
    ]
    defaults = replace(plan, stages=(replace(plan.stages[0], counts_toward_sla=None),))
    assert validate_workflow_plan(defaults, index()).ok
