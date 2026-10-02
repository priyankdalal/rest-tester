"""The suite Timeline tab lays a whole run on one waterfall."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from api_tester import theme
from api_tester.runner import CaseResult, SuiteResult
from api_tester.suite_timeline import SuiteTimelineTab, SuiteWaterfall, _format_ms


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def case(
    case_id: str,
    *,
    start: float = 0.0,
    wall: float = 0.0,
    request_offset: float = 0.0,
    elapsed: int = 0,
    passed: bool = True,
    skipped: bool = False,
    error: str = "",
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        case_name=f"case {case_id}",
        endpoint_id=f"ep-{case_id}",
        method="GET",
        path=f"/{case_id}",
        service="MasterData",
        passed=passed,
        skipped=skipped,
        error=error,
        elapsed_ms=elapsed,
        start_offset_ms=start,
        wall_ms=wall,
        request_offset_ms=request_offset,
    )


def measured_run() -> SuiteResult:
    return SuiteResult(
        suite_name="demo",
        started_at="2026-01-01T00:00:00Z",
        results=[
            case("a", start=0.0, wall=60.0, request_offset=1.0, elapsed=55),
            case("b", start=60.0, wall=40.0, request_offset=2.0, elapsed=35, passed=False),
            case("c", start=100.0, wall=0.1, skipped=True, passed=False),
        ],
    )


def _paint(widget, width: int = 900, height: int = 320) -> QPixmap:
    widget.resize(width, height)
    pixmap = QPixmap(width, height)
    pixmap.fill()
    widget.render(pixmap)
    return pixmap


@pytest.fixture()
def chart(app) -> SuiteWaterfall:
    return SuiteWaterfall()


class TestMeasuredLayout:
    def test_bars_use_the_offsets_recorded_by_the_runner(self, chart):
        chart.set_result(measured_run())
        assert [(row.start, row.wall) for row in chart.rows()] == [
            (0.0, 60.0),
            (60.0, 40.0),
            (100.0, 0.1),
        ]

    def test_the_run_is_marked_as_measured(self, chart):
        chart.set_result(measured_run())
        assert chart.is_measured() is True

    def test_the_total_spans_the_whole_run_not_the_sum_of_calls(self, chart):
        chart.set_result(measured_run())
        assert chart.total_ms() == pytest.approx(100.1)

    def test_the_http_call_is_placed_inside_its_case(self, chart):
        chart.set_result(measured_run())
        first = chart.rows()[0]
        assert first.request_start == pytest.approx(1.0)
        assert first.request_start + first.request <= first.end

    def test_time_not_spent_on_http_is_reported_as_overhead(self, chart):
        chart.set_result(measured_run())
        assert chart.rows()[0].overhead == pytest.approx(5.0)
        assert chart.overhead_ms() == pytest.approx(100.1 - 90)

    def test_every_case_gets_a_row_including_skipped_ones(self, chart):
        chart.set_result(measured_run())
        assert [row.case_id for row in chart.rows()] == ["a", "b", "c"]

    def test_outcomes_are_carried_through_for_colouring(self, chart):
        chart.set_result(measured_run())
        assert [row.outcome for row in chart.rows()] == ["PASS", "FAIL", "SKIPPED"]


class TestApproximateLayout:
    def test_a_run_without_offsets_falls_back_to_end_to_end(self, chart):
        legacy = SuiteResult(
            suite_name="legacy",
            started_at="",
            results=[case("a", elapsed=30), case("b", elapsed=20)],
        )
        chart.set_result(legacy)
        assert chart.is_measured() is False
        assert [(row.start, row.wall) for row in chart.rows()] == [(0.0, 30.0), (30.0, 20.0)]

    def test_the_approximation_is_disclosed_rather_than_implied(self, app):
        tab = SuiteTimelineTab()
        tab.set_result(
            SuiteResult(suite_name="legacy", started_at="", results=[case("a", elapsed=30)])
        )
        assert "Approximate" in tab.summary.text()

    def test_a_measured_run_is_not_labelled_approximate(self, app):
        tab = SuiteTimelineTab()
        tab.set_result(measured_run())
        assert "Approximate" not in tab.summary.text()


class TestEdgeCases:
    def test_no_result_clears_the_chart(self, chart):
        chart.set_result(measured_run())
        chart.set_result(None)
        assert chart.rows() == []
        assert chart.total_ms() == 0.0
        _paint(chart)

    def test_a_run_with_no_cases_is_handled(self, chart):
        chart.set_result(SuiteResult(suite_name="empty", started_at=""))
        assert chart.rows() == []
        _paint(chart)

    def test_an_all_zero_run_paints_without_dividing_by_zero(self, chart):
        chart.set_result(
            SuiteResult(suite_name="z", started_at="", results=[case("a"), case("b")])
        )
        assert chart.total_ms() == 0.0
        _paint(chart)

    def test_the_chart_grows_with_the_number_of_cases(self, chart):
        chart.set_result(measured_run())
        small = chart.sizeHint().height()
        many = SuiteResult(
            suite_name="many",
            started_at="",
            results=[case(str(i), start=i * 10.0, wall=10.0, elapsed=9) for i in range(25)],
        )
        chart.set_result(many)
        assert chart.sizeHint().height() > small

    def test_the_track_stays_positive_when_the_widget_is_tiny(self, chart):
        chart.set_result(measured_run())
        chart.resize(60, 40)
        _, width = chart._track()
        assert width > 0
        _paint(chart, 60, 40)

    def test_hiding_overhead_still_paints(self, chart):
        chart.set_result(measured_run())
        chart.set_show_overhead(False)
        _paint(chart)
        assert chart._show_overhead is False


class TestInteraction:
    def test_clicking_a_bar_selects_that_case(self, chart):
        chart.set_result(measured_run())
        chart.resize(900, 320)
        seen: list[str] = []
        chart.case_selected.connect(seen.append)
        y = chart.TOP + chart.ROW_HEIGHT + 2
        QTest.mouseClick(chart, Qt.MouseButton.LeftButton, pos=QPoint(400, int(y)))
        assert seen == ["b"]

    def test_clicking_below_the_last_row_selects_nothing(self, chart):
        chart.set_result(measured_run())
        chart.resize(900, 320)
        seen: list[str] = []
        chart.case_selected.connect(seen.append)
        y = chart.TOP + len(chart.rows()) * chart.ROW_HEIGHT + 6
        QTest.mouseClick(chart, Qt.MouseButton.LeftButton, pos=QPoint(400, int(y)))
        assert seen == []

    def test_the_tab_forwards_the_selection(self, app):
        tab = SuiteTimelineTab()
        tab.set_result(measured_run())
        seen: list[str] = []
        tab.case_selected.connect(seen.append)
        tab.chart.case_selected.emit("b")
        assert seen == ["b"]


class TestPresentation:
    def test_the_summary_separates_http_time_from_other_work(self, app):
        tab = SuiteTimelineTab()
        tab.set_result(measured_run())
        text = tab.summary.text()
        assert "3 cases" in text
        assert "waiting on HTTP" in text
        assert "everything else" in text

    def test_the_summary_resets_when_the_run_is_cleared(self, app):
        tab = SuiteTimelineTab()
        tab.set_result(measured_run())
        tab.set_result(None)
        assert tab.summary.text() == "No run yet"

    def test_the_chart_paints_with_the_active_theme_surface(self, app, chart):
        chart.set_result(measured_run())
        try:
            for mode in ("Dark", "Light"):
                theme.apply_theme(app, mode)
                chart.update()
                image = _paint(chart).toImage()
                assert image.pixelColor(2, 2).name() == theme.ACTIVE_TOKENS["SURFACE"]
        finally:
            theme.apply_theme(app, "Light")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0.5, "0.50 ms"), (12.34, "12.3 ms"), (210.0, "210 ms"), (15000.0, "15.0 s")],
    )
    def test_durations_are_formatted_to_a_believable_precision(self, value, expected):
        assert _format_ms(value) == expected


class TestLayoutDoesNotWidenTheWindow:
    """A long non-wrapping label here once forced the whole tab to 1048px."""

    def test_the_tab_can_be_narrower_than_its_preferred_width(self, app):
        tab = SuiteTimelineTab()
        assert tab.minimumSizeHint().width() <= 600

    def test_the_legend_can_shrink_far_below_its_preferred_width(self, app):
        tab = SuiteTimelineTab()
        legend = tab.legend
        assert legend.minimumSizeHint().width() <= 100
        assert legend.sizeHint().width() > legend.minimumSizeHint().width()

    def test_a_cramped_legend_drops_entries_instead_of_overflowing(self, app):
        tab = SuiteTimelineTab()
        tab.legend.resize(70, 18)
        _paint(tab.legend, 70, 18)

    def test_the_legend_follows_the_active_theme(self, app):
        tab = SuiteTimelineTab()
        try:
            for mode in ("Dark", "Light"):
                theme.apply_theme(app, mode)
                _paint(tab.legend, 400, 18)
        finally:
            theme.apply_theme(app, "Light")
