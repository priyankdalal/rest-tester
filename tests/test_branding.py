from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from api_tester.branding import APP_NAME, CATALOG_NAME_LIMIT, display_name
from api_tester.catalog import load_catalog
from api_tester.catalog_builder import CatalogDocument, merge_documents


@pytest.fixture
def quiet_dialogs(monkeypatch):
    """Silences every modal the shell or builder may raise.

    A modal blocks forever under the offscreen platform, so any test that can
    reach one must neutralise it.
    """
    from PyQt6.QtWidgets import QMessageBox

    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(
            QMessageBox,
            name,
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
        )


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


# --------------------------------------------------------------- display_name


def test_a_catalog_name_is_shown_as_written() -> None:
    assert display_name("Rest Tester Microservices") == "Rest Tester Microservices"


@pytest.mark.parametrize("value", ["", "   ", None])
def test_a_missing_name_falls_back_to_the_product_name(value) -> None:
    assert display_name(value) == APP_NAME


def test_a_name_is_trimmed_of_surrounding_whitespace() -> None:
    assert display_name("  Acme APIs  ") == "Acme APIs"


def test_a_name_at_the_limit_is_untouched() -> None:
    name = "N" * CATALOG_NAME_LIMIT
    assert display_name(name) == name


def test_a_longer_name_is_ellipsised_within_the_limit() -> None:
    shown = display_name("N" * (CATALOG_NAME_LIMIT + 50))
    assert len(shown) == CATALOG_NAME_LIMIT
    assert shown.endswith("\u2026")


def test_ellipsis_does_not_leave_a_dangling_space() -> None:
    name = "A" * (CATALOG_NAME_LIMIT - 1) + " tail"
    assert not display_name(name).rstrip("\u2026").endswith(" ")


# ------------------------------------------------------------------- catalog


def _catalog_document(**extra) -> dict:
    document = {
        "services": [
            {
                "name": "Demo",
                "repository": "demo",
                "default_base_url": "",
                "endpoints": [],
            }
        ],
        "filter_schemas": {},
        "payload_schemas": {},
        "form_schemas": {},
        "enums": {},
    }
    document.update(extra)
    return document


def _write(document: dict) -> Path:
    path = Path(tempfile.mkdtemp()) / "catalog.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


COMMITTED_CATALOG = Path("data/api_catalog.json")


def _catalog_copy(tmp_path: Path) -> Path:
    """A throwaway copy of the shipped catalog.

    The builder can write back to the file it opened - on close, or through a
    save prompt - so a test must never hand it the committed catalog.
    """
    target = tmp_path / "api_catalog.json"
    target.write_text(COMMITTED_CATALOG.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def test_a_catalog_carries_its_name() -> None:
    assert load_catalog(_write(_catalog_document(name="Acme"))).name == "Acme"


def test_a_catalog_without_a_name_loads_with_an_empty_one() -> None:
    assert load_catalog(_write(_catalog_document())).name == ""


def test_a_catalog_name_is_trimmed_on_load() -> None:
    assert load_catalog(_write(_catalog_document(name="  Acme  "))).name == "Acme"


def test_the_shipped_catalog_names_itself() -> None:
    catalog = load_catalog(COMMITTED_CATALOG)
    assert catalog.name
    assert display_name(catalog.name) == catalog.name


# ------------------------------------------------------------------- builder


def test_the_builder_document_round_trips_a_name() -> None:
    document = CatalogDocument.from_dict(_catalog_document(name="Acme"))
    assert document.name == "Acme"
    assert document.to_dict()["name"] == "Acme"


def test_the_builder_writes_a_name_key_even_when_unnamed() -> None:
    assert CatalogDocument().to_dict()["name"] == ""


def test_merging_keeps_the_first_name() -> None:
    first = CatalogDocument.from_dict(_catalog_document(name="First"))
    second = CatalogDocument.from_dict(_catalog_document(name="Second"))
    assert merge_documents([first, second]).name == "First"


def test_merging_takes_a_name_from_a_later_document_when_the_first_has_none() -> None:
    first = CatalogDocument.from_dict(_catalog_document())
    second = CatalogDocument.from_dict(_catalog_document(name="Second"))
    assert merge_documents([first, second]).name == "Second"


def test_renaming_a_catalog_is_not_drift() -> None:
    from tools.generate_catalog import catalog_differences

    base = {"services": [], "name": "Original"}
    renamed = {"services": [], "name": "Renamed"}
    assert catalog_differences(renamed, base) == []


def test_the_generator_accepts_a_name() -> None:
    import inspect

    import tools.generate_catalog as generator

    parameter = inspect.signature(generator.build_catalog).parameters["name"]
    assert parameter.default == generator.CATALOG_NAME


def test_the_generator_preserves_a_hand_authored_name() -> None:
    """The display name is authored, not extracted, so a rewrite must keep it."""
    from tools.generate_catalog import comparable_catalog

    assert "name" not in comparable_catalog({"name": "Anything", "services": []})


# ------------------------------------------------------------------- the icon


def test_the_application_icon_is_drawn_at_every_declared_size(qt_app) -> None:
    from api_tester.icons import APP_ICON_SIZES, app_icon, app_pixmap

    for size in APP_ICON_SIZES:
        pixmap = app_pixmap(size)
        assert not pixmap.isNull()
        assert pixmap.size().width() == size
    assert not app_icon().isNull()


def test_the_icon_actually_paints_something(qt_app) -> None:
    from api_tester.icons import app_pixmap

    image = app_pixmap(64).toImage()
    corner = image.pixelColor(0, 0)
    centre = image.pixelColor(32, 32)
    assert corner.alpha() == 0, "the tile should have transparent rounded corners"
    assert centre.alpha() == 255


@pytest.mark.parametrize(
    "name",
    ["api-explorer", "test-suites", "bookmark", "folder", "globe", "settings"],
)
def test_navigation_line_icons_paint_semantic_glyphs(qt_app, name) -> None:
    from api_tester.icons import icon

    image = icon(name, "#DCE9F8", 24).pixmap(24, 24).toImage()
    assert any(
        image.pixelColor(x, y).alpha() > 0
        for x in range(image.width())
        for y in range(image.height())
    )


def test_the_windows_icon_file_is_a_valid_multi_size_ico(qt_app, tmp_path) -> None:
    import struct

    from tools.make_icon import ICO_SIZES, build_ico

    target = build_ico(tmp_path / "app.ico")
    data = target.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind) == (0, 1)
    assert count == len(ICO_SIZES)

    seen = []
    for index in range(count):
        entry = data[6 + 16 * index : 22 + 16 * index]
        width, _, _, _, _, _, length, offset = struct.unpack("<BBBBHHII", entry)
        seen.append(width or 256)
        assert data[offset : offset + 8] == b"\x89PNG\r\n\x1a\n"
        assert length > 0
    assert seen == list(ICO_SIZES)


def test_qt_can_read_the_generated_ico_back(qt_app, tmp_path) -> None:
    from PyQt6.QtGui import QIcon

    from tools.make_icon import build_ico

    icon = QIcon(str(build_ico(tmp_path / "app.ico")))
    assert not icon.isNull()
    assert 256 in [size.width() for size in icon.availableSizes()]


def test_the_build_spec_ships_the_icon() -> None:
    spec = Path("RestTester.spec").read_text(encoding="utf-8")
    assert "tools.make_icon" in spec
    assert "icon=str(icon_path)" in spec


def test_claiming_the_taskbar_identity_is_safe_to_call() -> None:
    from api_tester.main import _claim_windows_taskbar_identity

    _claim_windows_taskbar_identity()


# ------------------------------------------------------------------ the shell


@pytest.fixture
def window(qt_app, monkeypatch, tmp_path):
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    value = main_module.MainWindow()
    yield value
    value.close()
    value.deleteLater()


def test_the_title_bar_always_shows_the_product_name(window) -> None:
    assert window.windowTitle() == APP_NAME


def test_the_window_has_an_icon(window) -> None:
    assert not window.windowIcon().isNull()


def test_the_header_shows_the_catalog_name(window) -> None:
    assert window.app_title.full_text() == window.catalog.name


def test_the_header_falls_back_to_the_product_name(window) -> None:
    window.catalog = type(window.catalog)(
        **{**window.catalog.__dict__, "name": ""}
    )
    window._refresh_app_title()
    assert window.app_title.full_text() == APP_NAME


def test_a_long_catalog_name_does_not_widen_the_window(window) -> None:
    """The header caption must shrink, not push the shell wider.

    A plain QLabel reports its full text width as its minimum size, which is
    how a long caption forces horizontal growth.
    """
    window.resize(1600, 950)
    window.show()
    before = window.minimumSizeHint().width()
    window.catalog = type(window.catalog)(
        **{**window.catalog.__dict__, "name": "Extremely Long Catalog " * 8}
    )
    window._refresh_app_title()
    assert len(window.app_title.full_text()) == CATALOG_NAME_LIMIT
    assert window.minimumSizeHint().width() == before


def test_the_header_caption_keeps_a_small_minimum_width(window) -> None:
    window.catalog = type(window.catalog)(
        **{**window.catalog.__dict__, "name": "N" * CATALOG_NAME_LIMIT}
    )
    window._refresh_app_title()
    assert window.app_title.minimumSizeHint().width() <= 80


def test_loading_a_catalog_updates_the_header(window, tmp_path, monkeypatch) -> None:
    path = tmp_path / "other.json"
    path.write_text(json.dumps(_catalog_document(name="Acme Platform")), "utf-8")

    import api_tester.main as main_module

    # _load_catalog_from ends with a modal confirmation, which would block.
    monkeypatch.setattr(
        main_module.QMessageBox,
        "information",
        staticmethod(lambda *a, **k: main_module.QMessageBox.StandardButton.Ok),
    )
    assert window._load_catalog_from(path)
    assert window.app_title.full_text() == "Acme Platform"
    assert window.windowTitle() == APP_NAME


# ----------------------------------------------------------- the builder page


def test_the_builder_exposes_a_name_field(qt_app, quiet_dialogs, tmp_path) -> None:
    from api_tester.catalog_builder_ui import CatalogBuilderWindow

    builder = CatalogBuilderWindow(_catalog_copy(tmp_path))
    try:
        assert builder.catalog_name.text() == builder.document.name
        assert builder.catalog_name.maxLength() == CATALOG_NAME_LIMIT
    finally:
        builder.close()


def test_editing_the_builder_name_does_not_touch_the_workspace_root(
    qt_app, quiet_dialogs, tmp_path
) -> None:
    from api_tester.catalog_builder_ui import CatalogBuilderWindow

    builder = CatalogBuilderWindow(_catalog_copy(tmp_path))
    try:
        root = builder.document.workspace_root
        builder.catalog_name.setText("Renamed")
        assert builder.document.name == "Renamed"
        assert builder.document.workspace_root == root
        assert builder.dirty
    finally:
        builder.close()


def test_builder_name_accepts_spaces_and_backspace(
    qt_app, quiet_dialogs, tmp_path
) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    from api_tester.catalog_builder_ui import CatalogBuilderWindow

    builder = CatalogBuilderWindow(_catalog_copy(tmp_path))
    try:
        builder.catalog_name.clear()
        builder.catalog_name.setFocus()
        QTest.keyClicks(builder.catalog_name, "Trial Wyze")
        assert builder.catalog_name.text() == "Trial Wyze"
        assert builder.document.name == "Trial Wyze"

        QTest.keyClick(builder.catalog_name, Qt.Key.Key_Backspace)
        assert builder.catalog_name.text() == "Trial Wyz"
        assert builder.document.name == "Trial Wyz"
        assert builder.dirty
    finally:
        builder.close()
