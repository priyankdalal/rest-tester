from __future__ import annotations

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.data_runner.mapping import (
    ColumnMapping,
    RowMapper,
    Transform,
    mapping_targets,
)
from api_tester.execution.models import RequestTemplate
from api_tester.schema import encode_filter


def _catalog_and_endpoint() -> tuple[Catalog, Endpoint]:
    endpoint = Endpoint(
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
        payload_schema="BrandUpdate",
        filter_entity="Brand",
        expected_status="200-299",
    )
    catalog = Catalog(
        services=(Service("TrialAuth", "TrialAuth", "https://example.test", (endpoint,)),),
        filter_schemas={
            "Brand": {
                "name": "Brand",
                "fields": [
                    {
                        "name": "Brand.Name",
                        "display_name": "Brand name",
                        "data_type": "Text",
                        "clr_type": "string",
                        "nullable": False,
                        "sortable": False,
                        "lov": [],
                        "operators": [
                            {"label": "Equals", "value": "eq"},
                            {"label": "Contains", "value": "ct"},
                        ],
                    },
                    {
                        "name": "DisplayName",
                        "display_name": "Display name",
                        "data_type": "Text",
                        "clr_type": "string",
                        "nullable": False,
                        "sortable": True,
                        "lov": [],
                        "operators": [{"label": "Equals", "value": "eq"}],
                    },
                    {
                        "name": "Category",
                        "display_name": "Category",
                        "data_type": "Text",
                        "clr_type": "string",
                        "nullable": False,
                        "sortable": True,
                        "lov": ["Retail", "Clinical"],
                        "operators": [{"label": "Equals", "value": "eq"}],
                    },
                ],
            }
        },
        payload_schemas={
            "BrandUpdate": {
                "name": "BrandUpdate",
                "kind": "object",
                "fields": [
                    {
                        "name": "Name",
                        "kind": "text",
                        "required": True,
                        "nullable": False,
                        "clr_type": "string",
                        "lov": [],
                        "constraints": {},
                    },
                    {
                        "name": "Address",
                        "kind": "object",
                        "required": False,
                        "nullable": False,
                        "clr_type": "AddressDto",
                        "fields": [
                            {
                                "name": "Country",
                                "kind": "text",
                                "required": False,
                                "nullable": True,
                                "clr_type": "string",
                                "lov": [],
                                "constraints": {},
                            }
                        ],
                    },
                    {
                        "name": "Category",
                        "kind": "text",
                        "required": False,
                        "nullable": False,
                        "clr_type": "string",
                        "lov": ["Retail", "Clinical"],
                        "constraints": {},
                    },
                ],
            }
        },
        enums={},
    )
    return catalog, endpoint


def test_mapping_targets_include_parameters_filters_sorts_payload_and_meta() -> None:
    catalog, endpoint = _catalog_and_endpoint()

    targets = {target.key: target for target in mapping_targets(endpoint, catalog)}

    assert targets["path:Id"].group == "path"
    assert targets["path:Id"].required is True
    assert targets["query:Status"].group == "query"
    assert targets["filter:Brand.Name:eq"].group == "filter"
    assert targets["filter:Category:eq"].allowed_values == ("Retail", "Clinical")
    assert targets["sort:DisplayName"].group == "sort"
    assert targets["payload:Name"].required is True
    assert targets["payload:Address.Country"].group == "payload"
    assert targets["payload:Category"].allowed_values == ("Retail", "Clinical")
    assert targets["expected_status"].group == "meta"
    assert targets["correlation_key"].group == "meta"
    assert targets["skip_row"].group == "meta"


def test_row_mapper_resolves_valid_row_and_builds_nested_payload_and_queries() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [
            ColumnMapping("brand_id", "path:Id", (Transform("trim"), Transform("to_integer"))),
            ColumnMapping("status", "query:Status", (Transform("trim"),)),
            ColumnMapping("brand_filter", "filter:Brand.Name:ct", (Transform("trim"),)),
            ColumnMapping("sort_name", "sort:DisplayName", (Transform("trim"),)),
            ColumnMapping("name", "payload:Name", (Transform("trim"),)),
            ColumnMapping("country", "payload:Address.Country", (Transform("trim"),)),
            ColumnMapping(
                "category",
                "payload:Category",
                (Transform("trim"), Transform("enum_validate")),
            ),
            ColumnMapping("expected", "expected_status", (Transform("trim"),)),
            ColumnMapping("row_key", "correlation_key", (Transform("trim"),)),
        ],
    )

    resolved = mapper.resolve(
        7,
        {
            "brand_id": " 42 ",
            "status": " active ",
            "brand_filter": " Acme ",
            "sort_name": "desc",
            "name": " Northwind ",
            "country": " CA ",
            "category": "Retail",
            "expected": "201",
            "row_key": "brand-42",
        },
    )

    assert resolved.is_valid is True
    assert resolved.path_values == {"Id": "42"}
    assert resolved.query_values["Status"] == "active"
    assert resolved.query_values["Filter"] == encode_filter(
        [{"name": "Brand.Name", "operator": "ct", "value": "Acme", "and": True}]
    )
    assert resolved.query_values["Sort"] == "DisplayName-"
    assert resolved.payload == {
        "Name": "Northwind",
        "Address": {"Country": "CA"},
        "Category": "Retail",
    }
    assert resolved.expected_status == "201"
    assert resolved.correlation_key == "brand-42"


def test_missing_required_path_value_marks_row_invalid() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [ColumnMapping("brand_id", "path:Id", (Transform("trim"),))],
    )

    resolved = mapper.resolve(1, {"brand_id": ""})

    assert resolved.is_valid is False
    assert any(
        issue.target_key == "path:Id"
        and issue.severity == "error"
        and issue.message == "Required value is missing."
        for issue in resolved.issues
    )


def test_to_integer_transform_failure_reports_clear_error() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [ColumnMapping("brand_id", "path:Id", (Transform("to_integer"),))],
    )

    resolved = mapper.resolve(3, {"brand_id": "abc"})

    messages = [issue.message for issue in resolved.issues if issue.target_key == "path:Id"]
    assert "'abc' is not a valid integer." in messages
    assert resolved.is_valid is False


def test_enum_validate_rejects_invalid_value_and_accepts_valid_value() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [
            ColumnMapping("brand_id", "path:Id"),
            ColumnMapping("name", "payload:Name"),
            ColumnMapping("category", "payload:Category", (Transform("enum_validate"),)),
        ],
    )

    invalid = mapper.resolve(4, {"brand_id": "4", "name": "Acme", "category": "Other"})
    assert any("Value must be one of:" in issue.message for issue in invalid.issues)

    valid = mapper.resolve(5, {"brand_id": "5", "name": "Acme", "category": "Retail"})
    assert valid.payload["Category"] == "Retail"
    assert valid.is_valid is True


def test_empty_is_omitted_skips_payload_key() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [
            ColumnMapping("brand_id", "path:Id"),
            ColumnMapping("name", "payload:Name"),
            ColumnMapping("country", "payload:Address.Country", (Transform("empty_is_omitted"),)),
        ],
    )

    resolved = mapper.resolve(6, {"brand_id": "6", "name": "Acme", "country": ""})

    assert resolved.payload == {"Name": "Acme"}
    assert "Address" not in resolved.payload


def test_empty_is_null_sets_payload_key_to_none() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [
            ColumnMapping("brand_id", "path:Id"),
            ColumnMapping("name", "payload:Name"),
            ColumnMapping("country", "payload:Address.Country", (Transform("empty_is_null"),)),
        ],
    )

    resolved = mapper.resolve(8, {"brand_id": "8", "name": "Acme", "country": ""})

    assert resolved.payload["Address"]["Country"] is None


def test_invalid_filter_operator_suffix_raises_value_error() -> None:
    catalog, endpoint = _catalog_and_endpoint()

    try:
        RowMapper(
            endpoint,
            catalog,
            [ColumnMapping("brand_filter", "filter:Brand.Name:bad")],
        )
    except ValueError as exc:
        assert "Invalid filter operator" in str(exc)
    else:
        raise AssertionError("Expected ValueError for invalid operator")


def test_sort_mapping_only_emits_sort_when_column_has_value() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [
            ColumnMapping("brand_id", "path:Id"),
            ColumnMapping("name", "payload:Name"),
            ColumnMapping("sort_name", "sort:DisplayName", (Transform("trim"),)),
        ],
    )

    blank = mapper.resolve(9, {"brand_id": "9", "name": "Acme", "sort_name": ""})
    assert "Sort" not in blank.query_values

    descending = mapper.resolve(10, {"brand_id": "10", "name": "Acme", "sort_name": "desc"})
    assert descending.query_values["Sort"] == "DisplayName-"


def test_skip_row_mapping_sets_skip_flag() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [
            ColumnMapping("brand_id", "path:Id"),
            ColumnMapping("name", "payload:Name"),
            ColumnMapping("skip", "skip_row", (Transform("trim"),)),
        ],
    )

    resolved = mapper.resolve(11, {"brand_id": "11", "name": "Acme", "skip": "skip"})

    assert resolved.skip is True
    assert resolved.is_valid is False


def test_build_request_template_uses_source_name_keys_and_resolved_status() -> None:
    catalog, endpoint = _catalog_and_endpoint()
    mapper = RowMapper(
        endpoint,
        catalog,
        [
            ColumnMapping("brand_id", "path:Id"),
            ColumnMapping("status", "query:Status"),
            ColumnMapping("name", "payload:Name"),
            ColumnMapping("expected", "expected_status"),
        ],
    )
    resolved = mapper.resolve(
        12,
        {"brand_id": "12", "status": "active", "name": "Acme", "expected": "202"},
    )
    base = RequestTemplate(
        endpoint_id=endpoint.id,
        service=endpoint.service,
        method=endpoint.method,
        path=endpoint.path,
        expected_status=endpoint.expected_status,
    )

    template = mapper.build_request_template(base, resolved)

    assert template.values["path:Id"] == "12"
    assert template.values["query:Status"] == "active"
    assert template.payload == {"Name": "Acme"}
    assert template.expected_status == "202"


def test_unknown_mapping_target_raises_value_error() -> None:
    catalog, endpoint = _catalog_and_endpoint()

    try:
        RowMapper(endpoint, catalog, [ColumnMapping("x", "query:Missing")])
    except ValueError as exc:
        assert "Unknown mapping target" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unknown target")
