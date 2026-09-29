import pytest

from api_tester import theme
from api_tester.comparison import ResponseComparison
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app):
    windows = []

    def create(previous, current):
        window = ResponseComparison(previous, current)
        windows.append(window)
        return window

    yield create
    for window in windows:
        window.close()


def test_compact_json_is_formatted_side_by_side(dialog):
    window = dialog('{"id":1,"name":"old"}', '{"id":1,"name":"new"}')
    assert window.pretty.isChecked()
    assert window.left.blockCount() == window.right.blockCount() == 4
    assert window.left.isReadOnly() and window.right.isReadOnly()
    assert window.left.numbers == [1, 2, 3, 4]
    assert window.changes == [2]
    assert len(window.left.extraSelections()) > 1


@pytest.mark.parametrize(
    ("before", "after", "left_numbers", "right_numbers"),
    [
        ("a\nb", "a\nnew\nb", [1, None, 2], [1, 2, 3]),
        ("a\nold\nb", "a\nb", [1, 2, 3], [1, None, 2]),
        ("old", "new\nextra", [1, None], [1, 2]),
        ("", "new", [None], [1]),
        ("old", "", [1], [None]),
    ],
)
def test_insert_delete_and_unequal_replacements_align(
    dialog, before, after, left_numbers, right_numbers
):
    window = dialog(before, after)
    assert window.left.numbers == left_numbers
    assert window.right.numbers == right_numbers
    assert window.left.blockCount() == window.right.blockCount()


@pytest.mark.parametrize("text", ["", "same\ntext", '{"a": 1}'])
def test_identical_response_disables_navigation(dialog, text):
    window = dialog(text, text)
    assert not window.changes
    assert not window.forward.isEnabled()
    assert not window.back.isEnabled()
    assert window.summary.text().startswith("No ")


def test_raw_mode_preserves_whitespace_and_final_newlines(dialog):
    window = dialog('{"id":1}', '{\n  "id": 1\n}\n')
    assert not window.changes
    window.pretty.setChecked(False)
    assert window.changes
    assert window.left.numbers == [1, None, None, None]
    assert window.right.numbers == [1, 2, 3, 4]
    window.pretty.setChecked(True)
    assert not window.changes


def test_non_json_is_not_reformatted_or_interpreted_as_html(dialog):
    window = dialog("<a>old</a>", "<a>new</a>")
    assert not window.pretty.isEnabled()
    assert window.left.toPlainText() == "<a>old</a>"
    assert window.right.toPlainText() == "<a>new</a>"


def test_unicode_character_highlight_uses_qt_offsets(dialog):
    window = dialog("\U0001f600 old", "\U0001f600 new")
    assert window.left.extraSelections()[1].cursor.selectedText() == "old"
    assert window.right.extraSelections()[1].cursor.selectedText() == "new"


def test_navigation_wraps_between_change_regions(dialog):
    window = dialog("old\nsame\nlast", "new\nsame\nend")
    window.forward.click()
    assert window.left.textCursor().blockNumber() == 0
    window.forward.click()
    assert window.right.textCursor().blockNumber() == 2
    window.forward.click()
    assert window.change_index == 0
    window.back.click()
    assert window.change_index == 1


@pytest.mark.parametrize("mode", ["Light", "Dark"])
def test_theme_scrolling_and_resizing(app, dialog, mode):
    previous_mode = "Dark" if theme.ACTIVE_TOKENS["SURFACE"] == "#132539" else "Light"
    try:
        theme.apply_theme(app, mode)
        before = "\n".join(f"line {i}" for i in range(180))
        window = dialog(before, before.replace("line 60", "changed 60"))
        window.show()
        app.processEvents()
        window.left.verticalScrollBar().setValue(50)
        assert window.right.verticalScrollBar().value() == 50
        window.right.verticalScrollBar().setValue(90)
        assert window.left.verticalScrollBar().value() == 90
        assert window.left.extraSelections()[0].format.background().color().name() == theme.DANGER_SOFT
        assert window.right.extraSelections()[0].format.background().color().name() == theme.SUCCESS_SOFT
        window.splitter.setSizes([350, 800])
        app.processEvents()
        assert window.left.width() < window.right.width()
        assert not window.grab().isNull()
    finally:
        theme.apply_theme(app, previous_mode)


def test_compare_button_opens_side_by_side_dialog(app, monkeypatch):
    from api_tester.client import ApiResult
    from api_tester.viewers import ResponseViewer

    opened = []
    monkeypatch.setattr(ResponseComparison, "exec", lambda self: opened.append(self))
    viewer = ResponseViewer()
    for body in ('{"a":1}', '{"a":2}'):
        viewer.show_result(ApiResult(
            passed=True, status_code=200, elapsed_ms=1, url="https://example.test",
            response_headers={}, response_body=body, content=body.encode(),
            content_type="application/json",
        ))
    viewer.compare_button.click()
    assert len(opened) == 1
    assert '"a": 1' in opened[0].left.toPlainText()
    assert '"a": 2' in opened[0].right.toPlainText()
    viewer.clear()
    assert not viewer.compare_button.isEnabled()
    viewer.close()


@pytest.mark.parametrize(
    ("bodies", "enabled"),
    [
        (["", "text"], True),
        (["text", ""], True),
        (["", ""], True),
        (["text", "\x00\x01"], False),
        (["text", "\x00\x01", "new"], False),
    ],
)
def test_empty_bodies_are_comparable_but_binary_bodies_are_not(app, bodies, enabled):
    from api_tester.client import ApiResult
    from api_tester.viewers import ResponseViewer

    viewer = ResponseViewer()
    for body in bodies:
        viewer.show_result(ApiResult(
            passed=True, status_code=200, elapsed_ms=1, url="https://example.test",
            response_headers={}, response_body=body, content=body.encode(),
            content_type="application/octet-stream" if "\x00" in body else "text/plain",
        ))
    assert viewer.compare_button.isEnabled() is enabled
    viewer.close()
