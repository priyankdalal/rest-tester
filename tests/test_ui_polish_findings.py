"""Guards for the three UI findings fixed in the polish pass.

Each test here failed before its fix and passes after it:

1. ``QCheckBox`` cannot word-wrap, so a long label sets a minimum dialog
   width the user can never shrink below.
2. Read-only tables showed a blank grid instead of an empty state.
3. ``QTableWidget`` sorts on the display string, so numeric columns ordered
   100 < 25 < 9 until the cells carried their value in ``UserRole``.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QTableWidget,
    QTableWidgetItem,
)

from api_tester import theme
from api_tester.icons import icon
from api_tester.widgets import (
    NumericTableItem,
    attach_table_empty_state,
    enable_result_sorting,
)

# A dialog wider than this cannot be tiled side by side on a 1366px laptop.
MAX_DIALOG_WIDTH = 820
MAX_CHECKBOX_WIDTH = 440


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, "Light")
    return app


def _widest_checkbox(widget) -> tuple[int, str]:
    boxes = widget.findChildren(QCheckBox)
    if not boxes:
        return 0, ""
    worst = max(boxes, key=lambda box: box.minimumSizeHint().width())
    return worst.minimumSizeHint().width(), worst.text()


def test_auth_profile_dialog_fits_a_small_screen(qt_app: QApplication) -> None:
    """Every auth method must leave the dialog shrinkable.

    The persistence checkbox used to read "Remember this credential and
    session between restarts (encrypted)", forcing 1209px.
    """
    from api_tester.authentication_ui import AuthProfileDialog

    dialog = AuthProfileDialog(None)
    combo = max(dialog.findChildren(QComboBox), key=lambda box: box.count())
    offenders: list[tuple[str, int, str]] = []
    for index in range(combo.count()):
        combo.setCurrentIndex(index)
        qt_app.processEvents()
        dialog.adjustSize()
        width = dialog.minimumSizeHint().width()
        box_width, box_text = _widest_checkbox(dialog)
        if width > MAX_DIALOG_WIDTH or box_width > MAX_CHECKBOX_WIDTH:
            offenders.append((combo.itemText(index), width, box_text))
    assert not offenders, f"dialog too wide for: {offenders}"


def test_transform_editor_dialog_fits_a_small_screen(qt_app: QApplication) -> None:
    from api_tester.data_runner.mapping import MappingTarget
    from api_tester.data_runner.ui import TransformEditorDialog

    target = MappingTarget("body:name", "Name", "Body", True, "String")
    dialog = TransformEditorDialog(target, ())
    dialog.adjustSize()
    box_width, box_text = _widest_checkbox(dialog)
    assert dialog.minimumSizeHint().width() <= MAX_DIALOG_WIDTH
    assert box_width <= MAX_CHECKBOX_WIDTH, box_text


def test_enum_values_do_not_inflate_the_transform_dialog(qt_app: QApplication) -> None:
    """Allowed values belong in the tooltip; inlining them blew up the width."""
    from api_tester.data_runner.mapping import MappingTarget
    from api_tester.data_runner.ui import TransformEditorDialog

    values = tuple(f"SomeLongEnumMember{n}" for n in range(8))
    target = MappingTarget(
        "body:status", "Status", "Body", True, "String", allowed_values=values
    )
    dialog = TransformEditorDialog(target, ())
    dialog.adjustSize()
    assert dialog.minimumSizeHint().width() <= MAX_DIALOG_WIDTH
    assert _widest_checkbox(dialog)[0] <= MAX_CHECKBOX_WIDTH
    # The information is still reachable, just not in the label.
    assert "SomeLongEnumMember7" in dialog.enum_validate.toolTip()


def test_persistence_checkbox_still_explains_what_is_remembered(
    qt_app: QApplication,
) -> None:
    """Shortening the label must not lose the information it carried."""
    from api_tester.authentication_ui import AuthProfileDialog

    dialog = AuthProfileDialog(None)
    combo = max(dialog.findChildren(QComboBox), key=lambda box: box.count())
    seen: set[str] = set()
    for index in range(combo.count()):
        combo.setCurrentIndex(index)
        qt_app.processEvents()
        tooltip = dialog.persistent_input.toolTip()
        if dialog.persistent_input.isVisibleTo(dialog) or tooltip:
            assert "encrypted" in tooltip.lower(), combo.itemText(index)
            seen.add(tooltip)
    # The wording varies by what the method actually stores.
    assert len(seen) >= 2


def test_empty_state_icon_names_all_resolve(qt_app: QApplication) -> None:
    """``icon()`` returns a blank pixmap for an unknown name rather than raising.

    A typo therefore ships as an invisible glyph, so assert the names used by
    the empty states are ones ``icons.py`` actually draws.
    """
    import re
    from pathlib import Path

    source = Path(icon.__code__.co_filename).read_text(encoding="utf-8")
    known = set(re.findall(r'name == "([a-z0-9\-]+)"', source))
    assert "table" not in known, "update this test if a 'table' glyph is added"

    roots = Path(__file__).resolve().parent.parent / "api_tester"
    used: set[str] = set()
    for path in roots.rglob("*.py"):
        body = path.read_text(encoding="utf-8")
        used.update(re.findall(r'icon_name="([a-z0-9\-]+)"', body))
    unknown = sorted(used - known)
    assert not unknown, f"empty states reference glyphs icons.py cannot draw: {unknown}"


def test_table_empty_state_tracks_row_count(qt_app: QApplication) -> None:
    table = QTableWidget(0, 2)
    table.resize(400, 200)
    overlay = attach_table_empty_state(
        table,
        icon_name="verify",
        title="Nothing here",
        guidance="Add a row.",
    )
    table.show()
    qt_app.processEvents()
    assert overlay.isVisible()

    table.insertRow(0)
    table.setItem(0, 0, QTableWidgetItem("x"))
    qt_app.processEvents()
    assert not overlay.isVisible()

    table.setRowCount(0)
    qt_app.processEvents()
    assert overlay.isVisible()
    table.hide()


def test_numeric_cells_sort_by_value_not_by_text(qt_app: QApplication) -> None:
    """A plain item would order these 100, 25, 9."""
    table = QTableWidget(0, 1)
    for value in (9, 100, 25):
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, NumericTableItem(value))
    enable_result_sorting(table)
    table.sortItems(0)
    assert [table.item(row, 0).text() for row in range(3)] == ["9", "25", "100"]


def test_numeric_cells_keep_their_formatting(qt_app: QApplication) -> None:
    item = NumericTableItem(1234.56, "1.2 s")
    assert item.text() == "1.2 s"
    assert item < NumericTableItem(9999.0, "10.0 s")


def test_visualizer_result_tables_sort_numerically(qt_app: QApplication) -> None:
    from api_tester.visualizer import RunVisualizer

    view = RunVisualizer()
    for table in (view.slowest, view.by_service, view.failures):
        assert table.isSortingEnabled()
