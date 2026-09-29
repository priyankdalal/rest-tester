from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QColor, QMouseEvent, QPixmap
from PyQt6.QtWidgets import QApplication

from api_tester import theme
from api_tester.visualizer import TimingChart, _format_duration


@pytest.fixture
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _case(name: str, elapsed: int, outcome: str = "PASS"):
    return SimpleNamespace(
        case_id=name.lower(),
        case_name=name,
        service="Accounts",
        elapsed_ms=elapsed,
        skipped=outcome == "SKIPPED",
        outcome=outcome,
    )


def test_duration_labels_are_humanized() -> None:
    assert _format_duration(8) == "8.00 ms"
    assert _format_duration(125) == "125 ms"
    assert _format_duration(1250) == "1.2 s"


def test_chart_click_selects_bar_and_preserves_case_signal(app: QApplication) -> None:
    chart = TimingChart()
    chart.resize(500, 240)
    chart.set_results([_case("Login", 120), _case("Create", 800, "FAIL")])
    selected = []
    chart.bar_clicked.connect(selected.append)

    chart.mousePressEvent(
        QMouseEvent(
            QEvent.Type.MouseButtonPress,
            chart._bar_rect(1).center(),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
    )

    assert selected == [1]
    assert chart._selected_index == 1


def test_chart_uses_active_theme_and_renders_bars(app: QApplication) -> None:
    chart = TimingChart()
    chart.resize(500, 240)
    chart.set_results([_case("Login", 120)])
    original = theme.SURFACE
    try:
        theme.SURFACE = "#102030"
        pixmap = QPixmap(chart.size())
        chart.render(pixmap)
        assert pixmap.toImage().pixelColor(1, 1) == QColor("#102030")
    finally:
        theme.SURFACE = original
