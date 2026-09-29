"""Tests for the waterfall rendering of response timings."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

from api_tester import theme
from api_tester.viewers import TIMELINE_PHASES, ResponseViewer, WaterfallTimeline, _format_ms

FULL = {"dns_ms": 12.5, "tcp_ms": 3.2, "tls_ms": 48.0, "http_ms": 210.0}


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _paint(widget, width: int = 800, height: int = 260) -> QPixmap:
    """Renders the widget, proving ``paintEvent`` runs end to end."""
    widget.resize(width, height)
    pixmap = QPixmap(width, height)
    pixmap.fill()
    widget.render(pixmap)
    return pixmap


@pytest.fixture()
def timeline(app):
    return WaterfallTimeline()


def test_phases_are_laid_end_to_end(timeline):
    timeline.set_timings(FULL)
    starts = [round(start, 2) for _, start, _, _, _ in timeline.rows()]
    assert starts == [0.0, 12.5, 15.7, 63.7]


def test_total_is_the_sum_of_the_phases(timeline):
    timeline.set_timings(FULL)
    assert timeline.total_ms() == pytest.approx(sum(FULL.values()))


def test_every_declared_phase_is_rendered(timeline):
    timeline.set_timings(FULL)
    assert [row[0] for row in timeline.rows()] == [phase[1] for phase in TIMELINE_PHASES]


def test_probe_phases_are_flagged_apart_from_the_request(timeline):
    timeline.set_timings(FULL)
    probe = {label: is_probe for label, _, _, _, is_probe in timeline.rows()}
    assert probe == {
        "DNS lookup": True,
        "TCP connect": True,
        "TLS handshake": True,
        "HTTP request and response": False,
    }


def test_a_missing_phase_is_skipped_without_leaving_a_gap(timeline):
    timeline.set_timings({"dns_ms": 10.0, "http_ms": 40.0})
    assert [(label, start) for label, start, _, _, _ in timeline.rows()] == [
        ("DNS lookup", 0.0),
        ("HTTP request and response", 10.0),
    ]


def test_a_non_numeric_duration_is_skipped_rather_than_crashing(timeline):
    timeline.set_timings({"dns_ms": "unavailable", "http_ms": 10.0})
    assert [row[0] for row in timeline.rows()] == ["HTTP request and response"]


def test_a_zero_duration_phase_still_occupies_a_row(timeline):
    timeline.set_timings({"dns_ms": 0.0, "http_ms": 25.0})
    assert len(timeline.rows()) == 2
    assert timeline.rows()[0][2] == 0.0


def test_an_all_zero_timing_set_paints_without_dividing_by_zero(timeline):
    timeline.set_timings({"dns_ms": 0.0, "tcp_ms": 0.0, "http_ms": 0.0})
    assert timeline.total_ms() == 0.0
    _paint(timeline)


def test_an_empty_timing_set_clears_the_chart(timeline):
    timeline.set_timings(FULL)
    timeline.set_timings({})
    assert timeline.rows() == []
    assert timeline.total_ms() == 0.0
    _paint(timeline)


def test_a_probe_error_is_kept_out_of_the_bars(timeline):
    timeline.set_timings({"http_ms": 90.0, "probe_error": "getaddrinfo failed"})
    assert [row[0] for row in timeline.rows()] == ["HTTP request and response"]
    assert timeline._probe_error == "getaddrinfo failed"
    _paint(timeline)


def test_a_probe_error_makes_room_for_its_caption(timeline):
    timeline.set_timings({"http_ms": 90.0})
    without = timeline.sizeHint().height()
    timeline.set_timings({"http_ms": 90.0, "probe_error": "getaddrinfo failed"})
    assert timeline.sizeHint().height() > without


def test_the_chart_paints_with_the_active_theme_surface(app, timeline):
    timeline.set_timings(FULL)
    try:
        for mode in ("Dark", "Light"):
            theme.apply_theme(app, mode)
            timeline.update()
            image = _paint(timeline).toImage()
            assert image.pixelColor(2, 2).name() == theme.ACTIVE_TOKENS["SURFACE"]
    finally:
        theme.apply_theme(app, "Light")


def test_the_track_stays_positive_when_the_widget_is_tiny(timeline):
    timeline.set_timings(FULL)
    timeline.resize(80, 60)
    _, width = timeline._track()
    assert width > 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.5, "0.50 ms"), (12.34, "12.3 ms"), (210.0, "210 ms")],
)
def test_durations_are_formatted_to_a_believable_precision(value, expected):
    assert _format_ms(value) == expected


def test_the_response_viewer_feeds_its_timings_to_the_waterfall(app):
    viewer = ResponseViewer()
    assert isinstance(viewer.timeline, WaterfallTimeline)
    viewer._show_timeline(dict(FULL))
    assert viewer.timeline.total_ms() == pytest.approx(sum(FULL.values()))


def test_clearing_the_response_viewer_empties_the_waterfall(app):
    viewer = ResponseViewer()
    viewer._show_timeline(dict(FULL))
    viewer.clear()
    assert viewer.timeline.rows() == []
