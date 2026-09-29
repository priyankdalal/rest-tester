"""Verifies the encoder/decoder mirrors Rest Tester's FilterSortService grammar.

The wire format is ``Name__op:=value[,value]`` with conditions joined by ``;``
(AND) or ``|`` (OR); sort fields are comma separated with a trailing ``-`` for
descending. These expectations come from
``TrialManagement.Commons/Helpers/Filters/FilterSortService.cs``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from api_tester.schema import (
    FilterSchema,
    decode_filter,
    decode_sort,
    encode_filter,
    encode_sort,
    seed_filter_value,
    seed_payload,
)

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "data" / "api_catalog.json"


def catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "encoded",
    [
        "Status__eq:=active",
        "Status__eq:=active;Id__eq:=1",
        "id__in:=1,4",
        "status__nin:=Deleted,Inactive;CropCode__eq:=CORN",
        "CreatedDate__bt:=2024-01-01,2024-12-31",
        "Name__ct:=see|Name__sw:=bay",
        "Name__eq:=",
    ],
)
def test_filter_round_trip(encoded: str) -> None:
    assert encode_filter(decode_filter(encoded)) == encoded


def test_decode_filter_tracks_or_conditions() -> None:
    conditions = decode_filter("Name__ct:=see|Status__eq:=Active;Id__gt:=3")
    assert [condition["and"] for condition in conditions] == [True, False, True]
    assert [condition["operator"] for condition in conditions] == ["ct", "eq", "gt"]
    assert conditions[1]["value"] == "Active"


def test_decode_filter_splits_multi_values() -> None:
    condition = decode_filter("Id__in:=1,4,9")[0]
    assert condition["value"].split(",") == ["1", "4", "9"]


def test_empty_value_is_preserved_for_null_checks() -> None:
    condition = decode_filter("Name__eq:=")[0]
    assert condition["value"] == ""
    assert encode_filter([condition]) == "Name__eq:="


@pytest.mark.parametrize(
    "encoded,expected",
    [
        ("Name", [("Name", False)]),
        ("Name,CreatedDate-", [("Name", False), ("CreatedDate", True)]),
        ("", []),
    ],
)
def test_sort_round_trip(encoded: str, expected: list[tuple[str, bool]]) -> None:
    entries = decode_sort(encoded)
    assert [(entry["name"], entry["descending"]) for entry in entries] == expected
    assert encode_sort(entries) == encoded


def brand_schema() -> FilterSchema:
    return FilterSchema.from_dict(catalog()["filter_schemas"]["Brand"])


def field(name: str):
    return next(item for item in brand_schema().fields if item.name == name)


def test_between_seeds_exactly_two_values() -> None:
    assert len(seed_filter_value(field("CreatedDate"), "bt").split(",")) == 2


def test_in_seeds_multiple_values() -> None:
    assert len(seed_filter_value(field("Id"), "in").split(",")) > 1


def test_lov_seed_uses_real_enum_members() -> None:
    status = field("Status")
    assert status.lov, "Brand.Status should expose its enum members"
    for value in seed_filter_value(status, "eq").split(","):
        assert value in status.lov


def test_contains_seeds_a_fragment_not_a_whole_value() -> None:
    value = seed_filter_value(field("Name"), "ct")
    assert "-" not in value and value.islower()


def test_date_fields_exclude_set_operators() -> None:
    operators = {operation.value for operation in field("CreatedDate").operators}
    assert "in" not in operators and "nin" not in operators
    assert "bt" in operators


def test_flag_and_lov_fields_only_support_equality() -> None:
    for schema in catalog()["filter_schemas"].values():
        for item in schema["fields"]:
            if item["data_type"] not in ("Flag", "LOV"):
                continue
            operators = {operation["value"] for operation in item["operators"]}
            assert operators <= {"eq", "neq"}


def test_seed_payload_covers_required_fields() -> None:
    schema = catalog()["payload_schemas"]["BrandPayload"]
    payload = seed_payload(schema)
    for item in schema["fields"]:
        if item.get("required"):
            assert payload.get(item["name"]), f"{item['name']} must be seeded"


def test_seed_payload_respects_enum_values() -> None:
    schema = catalog()["payload_schemas"]["BrandPayload"]
    status = next(item for item in schema["fields"] if item["name"] == "Status")
    assert seed_payload(schema)["Status"] in status["lov"]
