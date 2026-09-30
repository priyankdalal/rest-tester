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
    # The pixmap is rasterised at the screen's device pixel ratio, so its image
    # dimensions can exceed the requested logical size.
    width = image.width()
    height = image.height()
    painted = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if image.pixelColor(x, y).alpha() > 0
    ]

    assert painted
    xs = [x for x, _y in painted]
    ys = [y for _x, y in painted]
    left_margin = min(xs)
    right_margin = width - 1 - max(xs)
    top_margin = min(ys)
    bottom_margin = height - 1 - max(ys)
    scale = max(1, round(width / size))
    tolerance = scale
    assert abs(left_margin - right_margin) <= tolerance
    assert abs(top_margin - bottom_margin) <= tolerance


@pytest.mark.parametrize("size", [16, 18, 24])
def test_icon_is_rasterised_at_a_supersampled_resolution(
    app: QApplication, size: int
) -> None:
    sizes = icon("settings", "#65758B", size).availableSizes()

    assert sizes
    stored = sizes[0]
    assert stored.width() >= size
    assert stored.width() % size == 0
    assert stored.width() == stored.height()
