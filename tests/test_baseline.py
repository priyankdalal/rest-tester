from __future__ import annotations

from api_tester.baseline import BaselineConfig, compare_json_baseline


def test_baseline_config_defaults_from_legacy_missing_value() -> None:
    config = BaselineConfig.from_dict(None)

    assert config.enabled is False
    assert config.document is None
    assert config.ignored_paths == []
    assert config.ignore_array_order is False
    assert config.array_identity_keys == {}
    assert config.numeric_tolerance == 0.0


def test_equal_objects_ignore_key_order() -> None:
    assert compare_json_baseline({"b": 2, "a": 1}, {"a": 1, "b": 2}) == []


def test_reports_missing_added_type_and_value_differences_deterministically() -> None:
    differences = compare_json_baseline(
        {"missing": 1, "same": True, "typed": 1, "value": "old"},
        {"added": 9, "same": True, "typed": "1", "value": "new"},
    )

    assert [(item.path, item.kind) for item in differences] == [
        ("$.missing", "missing"),
        ("$.typed", "type"),
        ("$.value", "value"),
        ("$.added", "added"),
    ]
    assert differences[1].expected == 1
    assert differences[1].actual == "1"


def test_ignored_paths_support_rootless_dotted_and_wildcard_array_paths() -> None:
    config = BaselineConfig(
        ignored_paths=["updatedAt", "$.values[*].id"],
    )

    differences = compare_json_baseline(
        {"updatedAt": "old", "values": [{"id": 1, "name": "A"}]},
        {"updatedAt": "new", "values": [{"id": 2, "name": "B"}]},
        config,
    )

    assert [(item.path, item.kind) for item in differences] == [("$.values[0].name", "value")]


def test_ordered_arrays_compare_by_index() -> None:
    differences = compare_json_baseline([1, 2], [2, 1])

    assert [(item.path, item.kind) for item in differences] == [
        ("$[0]", "value"),
        ("$[1]", "value"),
    ]


def test_unordered_arrays_match_equivalent_items() -> None:
    config = BaselineConfig(ignore_array_order=True)

    assert compare_json_baseline([{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 1}], config) == []


def test_identity_key_arrays_match_by_configured_key() -> None:
    config = BaselineConfig(array_identity_keys={"$.values": "id"})

    differences = compare_json_baseline(
        {"values": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]},
        {"values": [{"id": 2, "name": "changed"}, {"id": 1, "name": "A"}]},
        config,
    )

    assert len(differences) == 1
    assert differences[0].path == '$.values[id=2].name'
    assert differences[0].kind == "value"


def test_identity_key_arrays_report_missing_and_added_items() -> None:
    config = BaselineConfig(array_identity_keys={"$.values": "id"})

    differences = compare_json_baseline(
        {"values": [{"id": 1, "name": "A"}]},
        {"values": [{"id": 2, "name": "B"}]},
        config,
    )

    assert [(item.path, item.kind) for item in differences] == [
        ('$.values[id=1]', "missing"),
        ('$.values[id=2]', "added"),
    ]


def test_unhashable_identity_values_fall_back_without_crashing() -> None:
    config = BaselineConfig(array_identity_keys={"$.values": "id"})

    differences = compare_json_baseline(
        {"values": [{"id": {"part": 1}, "name": "A"}]},
        {"values": [{"id": {"part": 1}, "name": "B"}]},
        config,
    )

    assert [(item.path, item.kind) for item in differences] == [
        ("$.values[0].name", "value")
    ]


def test_duplicate_identity_values_fall_back_to_ordered_comparison() -> None:
    config = BaselineConfig(array_identity_keys={"$.values": "id"})

    differences = compare_json_baseline(
        {"values": [{"id": 1, "name": "A"}, {"id": 1, "name": "B"}]},
        {"values": [{"id": 1, "name": "A"}]},
        config,
    )

    assert [(item.path, item.kind) for item in differences] == [
        ("$.values[1]", "missing")
    ]


def test_wildcard_identity_path_applies_to_nested_arrays() -> None:
    config = BaselineConfig(array_identity_keys={"$.groups[*].items": "id"})

    differences = compare_json_baseline(
        {"groups": [{"items": [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]}]},
        {"groups": [{"items": [{"id": 2, "name": "changed"}, {"id": 1, "name": "A"}]}]},
        config,
    )

    assert [(item.path, item.kind) for item in differences] == [
        ('$.groups[0].items[id=2].name', "value")
    ]


def test_root_ignore_rule_does_not_disable_all_comparison() -> None:
    config = BaselineConfig(ignored_paths=["$"])

    differences = compare_json_baseline({"name": "A"}, {"name": "B"}, config)

    assert [(item.path, item.kind) for item in differences] == [
        ("$.name", "value")
    ]


def test_numeric_tolerance_allows_small_deltas() -> None:
    config = BaselineConfig(numeric_tolerance=0.01)

    assert compare_json_baseline({"score": 1.0}, {"score": 1.005}, config) == []
    differences = compare_json_baseline({"score": 1.0}, {"score": 1.02}, config)
    assert [(item.path, item.kind) for item in differences] == [("$.score", "value")]
