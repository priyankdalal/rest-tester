"""Structural JSON baseline comparison."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class BaselineConfig:
    enabled: bool = False
    document: Any | None = None
    ignored_paths: list[str] = field(default_factory=list)
    ignore_array_order: bool = False
    array_identity_keys: dict[str, str] = field(default_factory=dict)
    numeric_tolerance: float = 0.0

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "BaselineConfig":
        if not isinstance(value, dict):
            return cls()
        return cls(
            enabled=bool(value.get("enabled", False)),
            document=value.get("document"),
            ignored_paths=[
                str(item)
                for item in (value.get("ignored_paths", value.get("ignore_paths", [])) or [])
                if str(item).strip()
            ],
            ignore_array_order=bool(value.get("ignore_array_order", False)),
            array_identity_keys={
                str(path): str(key)
                for path, key in dict(value.get("array_identity_keys", {}) or {}).items()
                if str(path).strip() and str(key).strip()
            },
            numeric_tolerance=max(_coerce_float(value.get("numeric_tolerance", 0.0)), 0.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BaselineDifference:
    path: str
    kind: str
    expected: Any = None
    actual: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PATH_PART = re.compile(r"([^.\[\]]+)|\[(\*|\d+)\]")
_PathToken = str | int


def compare_json_baseline(
    expected: Any, actual: Any, config: BaselineConfig | None = None
) -> list[BaselineDifference]:
    settings = config or BaselineConfig()
    ignored = [
        parsed
        for path in settings.ignored_paths
        if len(parsed := _parse_path(path)) > 1
    ]
    identities = {
        tuple(_parse_path(path)): key
        for path, key in settings.array_identity_keys.items()
        if path.strip() and key.strip()
    }
    return _compare(expected, actual, "$", ["$"], settings, ignored, identities)


def serialize_json_text(document: Any) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False)


def _coerce_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _parse_path(path: str) -> list[_PathToken]:
    text = (path or "").strip()
    if not text:
        return ["$"]
    if text.startswith("$"):
        text = text[1:]
    text = text.lstrip(".")
    tokens: list[_PathToken] = ["$"]
    for match in _PATH_PART.finditer(text):
        key, index = match.groups()
        if key is not None:
            tokens.append(key)
        elif index == "*":
            tokens.append("*")
        else:
            tokens.append(int(index))
    return tokens


def _path_ignored(tokens: list[_PathToken], ignored: list[list[_PathToken]]) -> bool:
    for pattern in ignored:
        if len(pattern) > len(tokens):
            continue
        if all(
            expected == "*" and actual != "$" or expected == actual
            for expected, actual in zip(pattern, tokens)
        ):
            return True
    return False


def _key_path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent != "$" else f"$.{key}"


def _index_path(parent: str, index: int) -> str:
    return f"{parent}[{index}]"


def _wildcard_path(parent: str) -> str:
    return f"{parent}[*]"


def _identity_path(parent: str, key: str, value: Any) -> str:
    label = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return f"{parent}[{key}={label}]"


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _compare(
    expected: Any,
    actual: Any,
    path: str,
    tokens: list[_PathToken],
    config: BaselineConfig,
    ignored: list[list[_PathToken]],
    identities: dict[tuple[_PathToken, ...], str],
) -> list[BaselineDifference]:
    if _path_ignored(tokens, ignored):
        return []

    if _is_number(expected) and _is_number(actual):
        tolerance = config.numeric_tolerance
        if abs(float(expected) - float(actual)) <= tolerance:
            return []
        return [BaselineDifference(path, "value", expected, actual)]

    expected_type = _json_type(expected)
    actual_type = _json_type(actual)
    if expected_type != actual_type:
        return [BaselineDifference(path, "type", expected, actual)]

    if isinstance(expected, dict):
        return _compare_dict(expected, actual, path, tokens, config, ignored, identities)
    if isinstance(expected, list):
        return _compare_list(expected, actual, path, tokens, config, ignored, identities)
    if expected != actual:
        return [BaselineDifference(path, "value", expected, actual)]
    return []


def _compare_dict(
    expected: dict[str, Any],
    actual: dict[str, Any],
    path: str,
    tokens: list[_PathToken],
    config: BaselineConfig,
    ignored: list[list[_PathToken]],
    identities: dict[tuple[_PathToken, ...], str],
) -> list[BaselineDifference]:
    differences: list[BaselineDifference] = []
    expected_keys = {str(key) for key in expected}
    actual_keys = {str(key) for key in actual}

    for key in sorted(expected_keys):
        child_path = _key_path(path, key)
        child_tokens = tokens + [key]
        if _path_ignored(child_tokens, ignored):
            continue
        if key not in actual:
            differences.append(BaselineDifference(child_path, "missing", expected[key], None))
            continue
        differences.extend(
            _compare(
                expected[key],
                actual[key],
                child_path,
                child_tokens,
                config,
                ignored,
                identities,
            )
        )

    for key in sorted(actual_keys - expected_keys):
        child_path = _key_path(path, key)
        child_tokens = tokens + [key]
        if not _path_ignored(child_tokens, ignored):
            differences.append(BaselineDifference(child_path, "added", None, actual[key]))
    return differences


def _compare_list(
    expected: list[Any],
    actual: list[Any],
    path: str,
    tokens: list[_PathToken],
    config: BaselineConfig,
    ignored: list[list[_PathToken]],
    identities: dict[tuple[_PathToken, ...], str],
) -> list[BaselineDifference]:
    identity_key = _identity_for_path(tokens, identities)
    if identity_key:
        return _compare_identity_list(
            expected, actual, path, tokens, identity_key, config, ignored, identities
        )
    if config.ignore_array_order:
        return _compare_unordered_list(expected, actual, path, tokens, config, ignored, identities)
    return _compare_ordered_list(expected, actual, path, tokens, config, ignored, identities)


def _identity_for_path(
    tokens: list[_PathToken],
    identities: dict[tuple[_PathToken, ...], str],
) -> str | None:
    exact = identities.get(tuple(tokens))
    if exact:
        return exact
    for pattern, key in identities.items():
        if len(pattern) == len(tokens) and all(
            expected == "*" and actual != "$" or expected == actual
            for expected, actual in zip(pattern, tokens)
        ):
            return key
    return None


def _compare_ordered_list(
    expected: list[Any],
    actual: list[Any],
    path: str,
    tokens: list[_PathToken],
    config: BaselineConfig,
    ignored: list[list[_PathToken]],
    identities: dict[tuple[_PathToken, ...], str],
) -> list[BaselineDifference]:
    differences: list[BaselineDifference] = []
    common = min(len(expected), len(actual))
    for index in range(common):
        differences.extend(
            _compare(
                expected[index],
                actual[index],
                _index_path(path, index),
                tokens + [index],
                config,
                ignored,
                identities,
            )
        )
    for index in range(common, len(expected)):
        item_path = _index_path(path, index)
        item_tokens = tokens + [index]
        if not _path_ignored(item_tokens, ignored):
            differences.append(BaselineDifference(item_path, "missing", expected[index], None))
    for index in range(common, len(actual)):
        item_path = _index_path(path, index)
        item_tokens = tokens + [index]
        if not _path_ignored(item_tokens, ignored):
            differences.append(BaselineDifference(item_path, "added", None, actual[index]))
    return differences


def _compare_identity_list(
    expected: list[Any],
    actual: list[Any],
    path: str,
    tokens: list[_PathToken],
    identity_key: str,
    config: BaselineConfig,
    ignored: list[list[_PathToken]],
    identities: dict[tuple[_PathToken, ...], str],
) -> list[BaselineDifference]:
    if not all(isinstance(item, dict) and identity_key in item for item in expected + actual):
        return _identity_fallback(
            expected, actual, path, tokens, config, ignored, identities
        )

    expected_ids = [item[identity_key] for item in expected]
    actual_ids = [item[identity_key] for item in actual]
    try:
        expected_unique = len(set(expected_ids)) == len(expected_ids)
        actual_unique = len(set(actual_ids)) == len(actual_ids)
    except TypeError:
        return _identity_fallback(
            expected, actual, path, tokens, config, ignored, identities
        )
    if not expected_unique or not actual_unique:
        return _identity_fallback(
            expected, actual, path, tokens, config, ignored, identities
        )

    expected_by_id = {item[identity_key]: item for item in expected}
    actual_by_id = {item[identity_key]: item for item in actual}
    differences: list[BaselineDifference] = []

    for identity in sorted(expected_by_id, key=_canonical):
        item_path = _identity_path(path, identity_key, identity)
        item_tokens = tokens + ["*"]
        if _path_ignored(item_tokens, ignored):
            continue
        if identity not in actual_by_id:
            differences.append(
                BaselineDifference(item_path, "missing", expected_by_id[identity], None)
            )
            continue
        differences.extend(
            _compare(
                expected_by_id[identity],
                actual_by_id[identity],
                item_path,
                item_tokens,
                config,
                ignored,
                identities,
            )
        )

    for identity in sorted(set(actual_by_id) - set(expected_by_id), key=_canonical):
        item_path = _identity_path(path, identity_key, identity)
        if not _path_ignored(tokens + ["*"], ignored):
            differences.append(BaselineDifference(item_path, "added", None, actual_by_id[identity]))
    return differences


def _identity_fallback(
    expected: list[Any],
    actual: list[Any],
    path: str,
    tokens: list[_PathToken],
    config: BaselineConfig,
    ignored: list[list[_PathToken]],
    identities: dict[tuple[_PathToken, ...], str],
) -> list[BaselineDifference]:
    if config.ignore_array_order:
        return _compare_unordered_list(
            expected, actual, path, tokens, config, ignored, identities
        )
    return _compare_ordered_list(
        expected, actual, path, tokens, config, ignored, identities
    )


def _compare_unordered_list(
    expected: list[Any],
    actual: list[Any],
    path: str,
    tokens: list[_PathToken],
    config: BaselineConfig,
    ignored: list[list[_PathToken]],
    identities: dict[tuple[_PathToken, ...], str],
) -> list[BaselineDifference]:
    matched_actual: set[int] = set()
    unmatched_expected: list[Any] = []
    wildcard = _wildcard_path(path)
    wildcard_tokens = tokens + ["*"]

    for expected_item in expected:
        match_index = next(
            (
                index
                for index, actual_item in enumerate(actual)
                if index not in matched_actual
                and not _compare(
                    expected_item,
                    actual_item,
                    wildcard,
                    wildcard_tokens,
                    config,
                    ignored,
                    identities,
                )
            ),
            None,
        )
        if match_index is None:
            unmatched_expected.append(expected_item)
        else:
            matched_actual.add(match_index)

    unmatched_actual = [
        item for index, item in enumerate(actual) if index not in matched_actual
    ]
    differences: list[BaselineDifference] = []
    if not _path_ignored(wildcard_tokens, ignored):
        for item in sorted(unmatched_expected, key=_canonical):
            differences.append(BaselineDifference(wildcard, "missing", item, None))
        for item in sorted(unmatched_actual, key=_canonical):
            differences.append(BaselineDifference(wildcard, "added", None, item))
    return differences


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
