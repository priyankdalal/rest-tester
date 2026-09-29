"""Framework scanners: canonical paths, prefix scoping, parameter extraction."""

from pathlib import Path

import pytest

from api_tester.scanners import base, registry


FIXTURES = Path(__file__).parent / "fixtures"


def scan(name: str, framework: str):
    result = registry.scan_project(FIXTURES / name, "Demo", framework)
    return {f"{item.method} {item.path}": item for item in result.endpoints}


def test_canonical_path_normalises_every_supported_syntax() -> None:
    assert base.canonical_path("/users/:id") == "/users/{id}"
    assert base.canonical_path("/users/<int:id>") == "/users/{id}"
    assert base.canonical_path("/users/<id>") == "/users/{id}"
    assert base.canonical_path(r"^users/(?P<pk>[0-9]+)/$") == "/users/{pk}"
    assert base.canonical_path("/users/{id:int}") == "/users/{id:int}"


def test_balanced_segment_keeps_nested_parentheses() -> None:
    text = "foo(@Query('a', new Pipe(1)) a: string)"
    segment = base.balanced_segment(text, text.index("("))
    assert segment == "@Query('a', new Pipe(1)) a: string"


def test_every_framework_is_registered() -> None:
    keys = {framework.key for framework in registry.FRAMEWORKS}
    assert {
        "dotnet",
        "fastapi",
        "flask",
        "django",
        "express",
        "nestjs",
        "spring",
        "jaxrs",
        "laravel",
        "symfony",
        "slim",
        "openapi",
    } <= keys


def test_fastapi_router_prefix_does_not_leak_to_app_routes() -> None:
    routes = scan("fastapi_app", "fastapi")
    assert "GET /health" in routes
    assert "GET /brands/{brand_id}" in routes
    assert "POST /brands" in routes
    query = {
        parameter["name"]
        for parameter in routes["GET /brands/{brand_id}"].parameters
        if parameter["source"] == "query"
    }
    assert "expand" in query
    assert routes["POST /brands"].payload_type is None


def test_fastapi_response_model_produces_response_schema() -> None:
    result = registry.scan_project(FIXTURES / "fastapi_app", "Demo", "fastapi")
    routes = {f"{item.method} {item.path}": item for item in result.endpoints}
    endpoint = routes["GET /brands/{brand_id}"]
    assert endpoint.response_schema == "BrandResponse"
    schema = result.response_schemas["BrandResponse"]
    assert {field["name"] for field in schema["fields"]} == {
        "id",
        "name",
        "active",
    }


def test_flask_blueprint_prefix_is_scoped_and_paths_are_canonical() -> None:
    routes = scan("flask_app", "flask")
    assert all("<" not in path for path in routes)
    assert "GET /ping" in routes
    assert "GET /trials/{trial_id}" in routes
    assert "DELETE /trials/{trial_id}" in routes
    assert "POST /trials" in routes


def test_flask_typed_return_produces_response_schema() -> None:
    result = registry.scan_project(FIXTURES / "flask_app", "Demo", "flask")
    routes = {f"{item.method} {item.path}": item for item in result.endpoints}
    assert routes["GET /trials/{trial_id}"].response_schema == "TrialResponse"
    schema = result.response_schemas["TrialResponse"]
    assert {field["name"] for field in schema["fields"]} == {"id", "name"}


def test_express_mount_prefix_is_scoped() -> None:
    routes = scan("express_app", "express")
    assert all(":" not in path.split(" ", 1)[1] for path in routes)
    assert "GET /catalog/products/{productId}" in routes
    assert "POST /catalog/products" in routes
    assert "PUT /catalog/products/{productId}" in routes
    assert "DELETE /catalog/products/{productId}" in routes


def test_nestjs_captures_query_parameters_from_long_signatures() -> None:
    routes = scan("nest_app", "nestjs")
    assert "GET /seasons/{seasonId}" in routes
    names = {
        parameter["name"]
        for endpoint in routes.values()
        for parameter in endpoint.parameters
    }
    assert {"seasonId", "include"} <= names


def test_spring_captures_optional_request_parameters() -> None:
    routes = scan("spring_app", "spring")
    assert "GET /growers/{growerId}" in routes
    assert "POST /growers" in routes
    query = {
        parameter["name"]
        for parameter in routes["GET /growers/{growerId}"].parameters
        if parameter["source"] == "query"
    }
    assert "region" in query


def test_django_reports_over_approximated_function_views() -> None:
    result = registry.scan_project(FIXTURES / "django_app", "Demo", "django")
    paths = {f"{item.method} {item.path}" for item in result.endpoints}
    assert "GET /reports/{report_id}" in paths
    assert "GET /exports" in paths
    assert all(
        "(?P<" not in endpoint.path and "<int:" not in endpoint.path
        for endpoint in result.endpoints
    )
    assert result.warnings


def test_django_viewset_serializer_produces_response_schema() -> None:
    result = registry.scan_project(FIXTURES / "django_app", "Demo", "django")
    routes = {f"{item.method} {item.path}": item for item in result.endpoints}
    assert routes["GET /plots"].response_schema == "PlotSerializerList"
    assert routes["GET /plots/{id}"].response_schema == "PlotSerializer"
    schema = result.response_schemas["PlotSerializer"]
    assert {field["name"] for field in schema["fields"]} == {
        "id",
        "name",
        "archived",
    }


@pytest.mark.parametrize("name,framework", [("laravel_app", "laravel"), ("symfony_app", "symfony")])
def test_php_scanners_do_not_invent_payload_types(name: str, framework: str) -> None:
    routes = scan(name, framework)
    assert routes
    assert all(endpoint.payload_type != "array" for endpoint in routes.values())


def test_laravel_group_prefix_is_positionally_scoped() -> None:
    routes = scan("laravel_app", "laravel")
    assert "GET /fields" in routes
    assert "POST /fields" in routes
    assert "GET /admin/audits/{auditId}" in routes


def test_symfony_class_prefix_is_applied() -> None:
    routes = scan("symfony_app", "symfony")
    assert "GET /measurements/{measurementId}" in routes
    assert "POST /measurements" in routes


def test_openapi_import_produces_canonical_paths() -> None:
    result = registry.scan_project(FIXTURES / "openapi_app", "Demo", "openapi")
    routes = {f"{item.method} {item.path}": item for item in result.endpoints}
    assert "GET /varieties" in routes
    assert "POST /varieties" in routes
    assert "GET /varieties/{varietyId}" in routes
    query = {
        parameter["name"]
        for parameter in routes["GET /varieties"].parameters
        if parameter["source"] == "query"
    }
    assert {"Filter", "PageSize"} <= query
    assert routes["POST /varieties"].response_schema == "VarietyResponse"
    response = result.response_schemas["VarietyResponse"]
    assert {field["name"] for field in response["fields"]} == {
        "id",
        "name",
        "status",
    }
    assert routes["GET /varieties"].response_schema == "ListVarietiesResponse"
    assert result.response_schemas["ListVarietiesResponse"]["kind"] == "array"


def test_detect_frameworks_scores_the_right_fixture() -> None:
    detected = registry.detect_frameworks(FIXTURES / "fastapi_app")
    assert detected
    assert detected[0][0].key in {"fastapi", "flask"}
