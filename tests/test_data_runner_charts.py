"""Coverage for the hand-drawn Data Runner charts (no rendering assertions,
just that state updates and paint don't raise)."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from api_tester.data_runner.charts import (
    LabeledBarChart,
    MultiSeriesLineChart,
    OutcomeBreakdownChart,
    ScatterChart,
    SparklineChart,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_outcome_breakdown_chart_accepts_counts_and_repaints(app: QApplication) -> None:
    chart = OutcomeBreakdownChart()
    chart.resize(200, 60)
    chart.set_counts({"passed": 3, "failed": 1})
    chart.show()
    chart.repaint()
    chart.hide()


def test_outcome_breakdown_chart_handles_empty_counts(app: QApplication) -> None:
    chart = OutcomeBreakdownChart()
    chart.resize(200, 60)
    chart.set_counts({})
    chart.repaint()


def test_sparkline_chart_line_mode_accepts_values(app: QApplication) -> None:
    chart = SparklineChart(mode="line")
    chart.resize(200, 80)
    for value in (10.0, 20.0, 5.0, 30.0):
        chart.append(value)
    chart.repaint()
    assert len(chart._values) == 4


def test_sparkline_chart_bar_mode_accepts_values(app: QApplication) -> None:
    chart = SparklineChart(mode="bar")
    chart.resize(200, 80)
    chart.set_values([1, 2, 3])
    chart.repaint()
    assert chart._values == [1.0, 2.0, 3.0]


def test_sparkline_chart_bounds_history_to_max_points(app: QApplication) -> None:
    chart = SparklineChart(mode="line", max_points=5)
    for value in range(10):
        chart.append(float(value))
    assert len(chart._values) == 5
    assert chart._values == [5.0, 6.0, 7.0, 8.0, 9.0]


def test_sparkline_chart_clear_resets_values(app: QApplication) -> None:
    chart = SparklineChart(mode="line")
    chart.append(1.0)
    chart.clear()
    assert chart._values == []


def test_sparkline_chart_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError):
        SparklineChart(mode="pie")


def test_sparkline_chart_renders_without_data(app: QApplication) -> None:
    chart = SparklineChart(mode="line")
    chart.resize(200, 80)
    chart.repaint()


def test_multi_series_chart_keeps_each_series_bounded(app: QApplication) -> None:
    chart = MultiSeriesLineChart((("P50", "#00aaff"), ("P95", "#ffaa00")), max_points=3)
    for value in range(5):
        chart.append({"P50": value, "P95": value * 2})

    assert chart._values["P50"] == [2.0, 3.0, 4.0]
    assert chart._values["P95"] == [4.0, 6.0, 8.0]

    chart.clear()
    assert chart._values == {"P50": [], "P95": []}


def test_labeled_bar_chart_sorts_and_caps_entries_by_default(app: QApplication) -> None:
    chart = LabeledBarChart(max_entries=2)
    chart.resize(200, 90)
    chart.set_entries({"200": 5, "404": 20, "500": 1})
    chart.repaint()
    assert [label for label, _value, _color in chart._entries] == ["404", "200"]


def test_labeled_bar_chart_preserves_order_when_unsorted(app: QApplication) -> None:
    chart = LabeledBarChart()
    chart.resize(200, 90)
    chart.set_entries([("p50", 100.0), ("p90", 50.0)], sort_descending=False)
    chart.repaint()
    assert [label for label, _value, _color in chart._entries] == ["p50", "p90"]


def test_labeled_bar_chart_renders_without_data(app: QApplication) -> None:
    chart = LabeledBarChart()
    chart.resize(200, 90)
    chart.repaint()


def test_scatter_chart_bounds_history_to_max_points(app: QApplication) -> None:
    chart = ScatterChart(max_points=3)
    for index in range(5):
        chart.append(float(index), float(index) * 2)
    assert chart._points == [(2.0, 4.0), (3.0, 6.0), (4.0, 8.0)]


def test_scatter_chart_clear_resets_points(app: QApplication) -> None:
    chart = ScatterChart()
    chart.append(1.0, 2.0)
    chart.clear()
    assert chart._points == []


def test_scatter_chart_renders_without_data(app: QApplication) -> None:
    chart = ScatterChart()
    chart.resize(200, 90)
    chart.repaint()
