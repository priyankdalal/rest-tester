"""Framework-aware source scanners that discover HTTP endpoints in a folder.

Each scanner turns a project directory into the ``endpoints`` list used by
``data/api_catalog.json``.  Scanners are deliberately static source parsers so
that a project never has to be built, installed, or executed to be cataloged.
"""

from __future__ import annotations

from .base import (
    DiscoveredEndpoint,
    ScanResult,
    endpoint_identity,
    normalise_path,
)
from .registry import (
    FRAMEWORKS,
    Framework,
    detect_frameworks,
    framework_by_key,
    framework_keys,
    scan_project,
)

__all__ = [
    "DiscoveredEndpoint",
    "FRAMEWORKS",
    "Framework",
    "ScanResult",
    "detect_frameworks",
    "endpoint_identity",
    "framework_by_key",
    "framework_keys",
    "normalise_path",
    "scan_project",
]
