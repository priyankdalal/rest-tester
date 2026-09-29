from __future__ import annotations

import copy
import re
import uuid
from datetime import UTC, date, datetime
from typing import Any

from .catalog import Parameter


def sample_for_type(type_name: str, name: str = "") -> Any:
    normalized = type_name.lower().replace("?", "")
    lowered_name = name.lower()
    if "file" in normalized:
        return ""
    if "guid" in normalized:
        return str(uuid.uuid4())
    if "datetime" in normalized:
        return datetime.now(UTC).isoformat()
    if normalized == "dateonly" or normalized == "date":
        return date.today().isoformat()
    if "bool" in normalized:
        return False
    if any(token in normalized for token in ("int", "long", "short", "decimal", "double", "float")):
        return 1
    if normalized.startswith(("list<", "ienumerable<", "icollection<", "hashset<")) or normalized.endswith("[]"):
        return []
    if lowered_name.endswith("id"):
        return 1
    return f"test-{name or 'value'}"


def seed_parameter(parameter: Parameter) -> str:
    value = parameter.sample
    if parameter.values and str(value) not in parameter.values:
        # A sample outside the allowed list would only ever produce a 400, and
        # samples are usually auto-generated rather than deliberate.
        return str(parameter.values[0])
    if value in (None, ""):
        value = sample_for_type(parameter.type, parameter.name)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value)
    return str(value)


def refresh_payload(payload: Any) -> Any:
    value = copy.deepcopy(payload)

    def refresh(item: Any, key: str = "") -> Any:
        if isinstance(item, dict):
            return {child_key: refresh(child, child_key) for child_key, child in item.items()}
        if isinstance(item, list):
            return [refresh(child, key) for child in item]
        if isinstance(item, str):
            if re.fullmatch(r"[0-9a-fA-F-]{36}", item):
                return str(uuid.uuid4())
            if item.startswith("test-"):
                return f"test-{key or 'value'}-{uuid.uuid4().hex[:8]}"
        return item

    return refresh(value)
