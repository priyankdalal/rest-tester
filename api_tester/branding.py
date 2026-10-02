"""Application name and the rules for showing a catalog's name.

The window title is always the product name. The header shows the *catalog's*
name, because one build of the app can be pointed at any catalog, and the
catalog is what the user is actually working in.
"""

from __future__ import annotations

APP_NAME = "Rest Tester"
APP_VERSION = "2.1.0"
APP_TAGLINE = "A desktop workbench for exploring, testing and load-testing REST APIs."
APP_DESCRIPTION = (
    "Rest Tester turns an API catalog into a guided workspace: browse every endpoint, "
    "build requests from their schemas, verify responses, organise regression suites, "
    "drive requests from CSV data and measure behaviour under load."
)
APP_COPYRIGHT = "Copyright © 2026 Rest Tester contributors"
#: SPDX identifier of the Rest Tester license. GPL because the packaged build
#: bundles PyQt6, which is GPL-3.0-only; see LICENSE at the repository root.
APP_LICENSE_ID = "GPL-3.0-or-later"
APP_LICENSE_NAME = "GNU General Public License v3.0 or later"
APP_LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"

#: Longest catalog name shown in the header, ellipsis included.
CATALOG_NAME_LIMIT = 100

ELLIPSIS = "\u2026"


def display_name(catalog_name: str | None) -> str:
    """The header caption for a catalog name.

    Falls back to the product name when the catalog does not name itself, and
    never returns more than :data:`CATALOG_NAME_LIMIT` characters.
    """
    text = "" if catalog_name is None else str(catalog_name).strip()
    if not text:
        return APP_NAME
    if len(text) <= CATALOG_NAME_LIMIT:
        return text
    kept = text[: CATALOG_NAME_LIMIT - len(ELLIPSIS)].rstrip()
    return kept + ELLIPSIS
