"""Case-insensitive header precedence shared by authoring and execution."""

from __future__ import annotations

import re

import requests

from .catalog import Endpoint


def merge_headers(*layers: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    names: dict[str, str] = {}
    for layer in layers:
        for name, value in layer.items():
            old_name = names.get(name.lower())
            if old_name is not None:
                del result[old_name]
            names[name.lower()] = name
            result[name] = value
    return result


def request_headers(endpoint: Endpoint | None, values: dict[str, str]) -> dict[str, str]:
    declared = {
        parameter.name.lower()
        for parameter in endpoint.parameters if parameter.source == "header"
    } if endpoint else set()
    result: dict[str, str] = {}
    for key, value in values.items():
        if not key.startswith("header:"):
            continue
        name = key.split(":", 1)[1]
        if values.get(f"enabled:header:{name}", "true").lower() == "false":
            continue
        # Blank catalog fields leave inherited/managed values intact.
        if name.lower() in declared and not value:
            continue
        result = merge_headers(result, {name: value})
    return result


def validate_headers(headers: dict[str, str]) -> None:
    for name, value in headers.items():
        if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name):
            raise ValueError("Invalid header name: use an HTTP token without spaces or colons.")
        try:
            requests.utils.check_header_validity((name, value))
            if re.search(r"[\x00-\x08\x0a-\x1f\x7f]", value):
                raise requests.exceptions.InvalidHeader()
            value.encode("latin-1")
        except (requests.exceptions.InvalidHeader, UnicodeEncodeError):
            raise ValueError(
                f"Invalid value for header {name}: no leading whitespace, control characters, "
                "or characters outside Latin-1 are allowed."
            ) from None
