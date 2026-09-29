from tools.schema_extractor import Property, TypeInfo, build_response_schema


def test_build_response_schema_resolves_closed_generic_wrappers() -> None:
    types = {
        "GetAllResponse": TypeInfo(
            name="GetAllResponse",
            type_parameters=["T"],
            properties=[
                Property(name="Values", type="List<T>"),
                Property(name="NextPageUri", type="string?"),
            ],
        ),
        "ProductResponse": TypeInfo(
            name="ProductResponse",
            properties=[
                Property(name="Id", type="int"),
                Property(name="Name", type="string"),
            ],
        ),
    }

    schema = build_response_schema("GetAllResponse<ProductResponse>", types, {})

    assert schema is not None
    values = next(field for field in schema["fields"] if field["name"] == "Values")
    assert values["kind"] == "array"
    assert values["item"]["kind"] == "object"
    assert [field["name"] for field in values["item"]["fields"]] == ["Id", "Name"]
    assert values["item"]["fields"][0]["kind"] == "integer"


def test_build_response_schema_returns_plain_response_dto_fields() -> None:
    types = {
        "ProductResponse": TypeInfo(
            name="ProductResponse",
            properties=[
                Property(name="Name", type="string"),
                Property(name="Status", type="ProductStatus"),
            ],
        ),
    }
    enums = {"ProductStatus": ["Draft", "Active"]}

    schema = build_response_schema("ProductResponse", types, enums)

    assert schema == {
        "kind": "object",
        "name": "ProductResponse",
        "clr_type": "ProductResponse",
        "fields": [
            {
                "name": "Name",
                "clr_type": "string",
                "nullable": False,
                "required": False,
                "display_name": "Name",
                "kind": "text",
            },
            {
                "name": "Status",
                "clr_type": "ProductStatus",
                "nullable": False,
                "required": False,
                "display_name": "Status",
                "kind": "enum",
                "lov": ["Draft", "Active"],
            },
        ],
    }


def test_build_response_schema_returns_none_when_no_properties_exist() -> None:
    schema = build_response_schema("EmptyResponse", {"EmptyResponse": TypeInfo(name="EmptyResponse")}, {})

    assert schema is None
