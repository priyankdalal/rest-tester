"""Regression coverage for the hand-drawn application icon renderer."""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from api_tester.icons import icon


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("name", ["settings", "chart", "warning", "verify", "api-explorer"])
@pytest.mark.parametrize("size", [15, 16, 18])
def test_small_icons_keep_transparent_margin_on_every_edge(
    app: QApplication, name: str, size: int
) -> None:
    image = icon(name, "#65758B", size).pixmap(size, size).toImage()
    painted = [
        (x, y)
        for y in range(size)
        for x in range(size)
        if image.pixelColor(x, y).alpha() > 0
    ]

    assert painted
    xs = [x for x, _y in painted]
    ys = [y for _x, y in painted]
    left_margin = min(xs)
    right_margin = size - 1 - max(xs)
    top_margin = min(ys)
    bottom_margin = size - 1 - max(ys)
    assert abs(left_margin - right_margin) <= 1
    assert abs(top_margin - bottom_margin) <= 1
