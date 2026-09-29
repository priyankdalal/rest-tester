import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QAbstractItemView, QGroupBox, QWidget

from api_tester.catalog import Catalog
from api_tester.runner import CaseResult
from api_tester.suite_ui import SuiteTab


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def tab(app, tmp_path):
    widget = SuiteTab(
        Catalog((), {}, {}, {}), tmp_path, tmp_path / "history.json", lambda: {}
    )
    yield widget
    widget.close()


def result(**kwargs):
    return CaseResult(
        case_id="one", case_name="Case one", endpoint_id="endpoint",
        method="GET", path="/items", service="Demo", passed=True, **kwargs,
    )


@pytest.mark.parametrize(
    ("url", "display"),
    [
        ("https://example.test/items?Filter=Name__eq%3A%3DRed%20Apple",
         "https://example.test/items?Filter=Name__eq:=Red Apple"),
        ("https://example.test/a+b?q=Red+Apple%2BTea#part+one",
         "https://example.test/a+b?q=Red Apple+Tea#part+one"),
        ("https://example.test/%E6%B5%8B%E8%AF%95?q=%2520",
         "https://example.test/\u6d4b\u8bd5?q=%20"),
        ("https://example.test/?q=%3Cb%3Etext%3C%2Fb%3E",
         "https://example.test/?q=<b>text</b>"),
        ("", ""),
    ],
)
def test_url_display_is_decoded_without_changing_result_or_diagnostics(tab, url, display):
    case_result = result(url=url)
    tab._show_case_result(case_result)
    assert tab.case_response.url_label.text() == display
    assert tab.case_response.url_label.textFormat() == Qt.TextFormat.PlainText
    assert case_result.url == url
    assert tab.case_response._diagnostic["url"] == url


def test_captured_variables_have_separate_read_only_rows(tab):
    values = {"id": "42", "name": "a,b = c", "empty": "", "json": '{"id":1}'}
    tab._show_case_result(result(captured=values))
    table = tab.case_captured
    assert table.rowCount() == 4
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert [table.horizontalHeaderItem(i).text() for i in range(2)] == ["Variable", "Value"]
    assert [
        (table.item(i, 0).text(), table.item(i, 1).text()) for i in range(4)
    ] == list(values.items())
    assert table.item(3, 1).toolTip() == values["json"]
    assert tab.captured_summary.text() == "4 captured"
    assert tab.case_tables_splitter.orientation() == Qt.Orientation.Horizontal
    assert tab.case_tables_splitter.count() == 2
    assert tab.case_result_accordion.widgetResizable()
    assert tab.result_summary_section.is_expanded()
    assert tab.result_response_section.is_expanded()


def test_result_summary_uses_flat_labeled_columns(tab):
    summary = tab.findChild(QWidget, "caseResultSummary")
    assert summary is not None
    assert tab.assertion_results.objectName() == "caseResultTable"
    assert tab.case_captured.objectName() == "caseResultTable"
    columns = summary.findChildren(QWidget, "caseResultColumn")
    assert len(columns) == 2
    assert all(not column.findChildren(QGroupBox) for column in columns)
    assert tab.case_tables_splitter.handleWidth() == 18
    assert (
        tab.assertion_results_stack.currentWidget()
        is tab.assertion_empty_state
    )
    assert tab.captured_results_stack.currentWidget() is tab.captured_empty_state


def test_result_tables_have_a_gutter_and_aligned_headers(tab, app):
    tab.resize(1000, 700)
    tab.show()
    tab.detail_tabs.setCurrentIndex(2)
    app.processEvents()

    assertions_right = tab.assertion_results.mapTo(
        tab, tab.assertion_results.rect().topRight()
    ).x()
    captured_left = tab.case_captured.mapTo(
        tab, tab.case_captured.rect().topLeft()
    ).x()
    assert captured_left - assertions_right >= 18
    assert (
        tab.assertion_results.horizontalHeader().mapTo(
            tab, tab.assertion_results.horizontalHeader().rect().topLeft()
        ).y()
        == tab.case_captured.horizontalHeader().mapTo(
            tab, tab.case_captured.horizontalHeader().rect().topLeft()
        ).y()
    )


def test_switching_cases_and_clearing_removes_stale_captures(tab):
    tab._show_case_result(result(captured={"old": "1", "second": "2"}))
    tab._show_case_result(result(captured={"new": "3"}))
    assert tab.case_captured.rowCount() == 1
    assert tab.case_captured.item(0, 0).text() == "new"
    tab._show_case_result(result())
    assert tab.case_captured.rowCount() == 0
    assert tab.captured_summary.text() == "None captured"
    tab._show_case_result(None)
    assert tab.case_captured.rowCount() == 0
    assert tab.captured_summary.text() == "Not run yet"
    assert tab.case_captured.horizontalHeaderItem(0).text() == "Variable"
    assert tab.case_response.url_label.text() == ""


def test_assertion_outcomes_use_theme_status_treatment(tab):
    from api_tester import theme
    from api_tester.suite import Assertion, AssertionOutcome

    tab._show_case_result(
        result(
            assertions=[
                AssertionOutcome(
                    Assertion(kind="json_exists", path="id"), True, "found"
                ),
                AssertionOutcome(
                    Assertion(kind="json_exists", path="name"), False, "missing"
                ),
            ]
        )
    )

    passed = tab.assertion_results.item(0, 0)
    failed = tab.assertion_results.item(1, 0)
    assert passed.textAlignment() == int(Qt.AlignmentFlag.AlignCenter)
    assert passed.background().color().name() == theme.SUCCESS_SOFT
    assert failed.background().color().name() == theme.DANGER_SOFT
    assert tab.assertion_results_stack.currentWidget() is tab.assertion_results
