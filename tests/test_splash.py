from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtWidgets import QApplication

from api_tester.splash import MINIMUM_VISIBLE_MS, SPLASH_SIZE, SplashScreen, splash_pixmap


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_splash_pixmap_has_the_approved_dimensions_and_opaque_background(
    app: QApplication,
) -> None:
    pixmap = splash_pixmap()

    assert pixmap.size() == QSize(760, 600) == SPLASH_SIZE
    assert not pixmap.isNull()
    assert pixmap.toImage().pixelColor(0, 0).alpha() == 255


def test_splash_contains_visible_artwork_away_from_the_background(
    app: QApplication,
) -> None:
    image = splash_pixmap().toImage()

    background = image.pixelColor(0, 0)
    icon_centre = image.pixelColor(380, 212)
    request_path = image.pixelColor(86, 322)

    assert icon_centre != background
    assert request_path != background


def test_splash_is_frameless_and_kept_above_starting_windows(
    app: QApplication,
) -> None:
    splash = SplashScreen()
    try:
        assert splash.objectName() == "startupSplash"
        assert splash.windowFlags() & Qt.WindowType.SplashScreen
        assert splash.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert splash.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert MINIMUM_VISIBLE_MS >= 500
    finally:
        splash.close()


def test_splash_progress_tracks_and_clamps_completed_work(
    app: QApplication,
) -> None:
    splash = SplashScreen()
    try:
        initial = splash.pixmap().toImage()
        splash.set_progress(70, "Preparing service workspace")
        updated = splash.pixmap().toImage()

        assert splash.progress == 70
        assert splash.status == "Preparing service workspace"
        assert updated.pixelColor(400, 516) != initial.pixelColor(400, 516)

        splash.set_progress(120, "Workspace ready")
        assert splash.progress == 100
    finally:
        splash.close()


def test_main_window_reports_real_startup_milestones(
    app: QApplication,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import api_tester.main as main_module

    monkeypatch.setattr(main_module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main_module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    updates: list[tuple[int, str]] = []
    window = main_module.MainWindow(startup_progress=lambda *args: updates.append(args))
    try:
        assert updates == [
            (10, "Loading settings"),
            (25, "Loading API catalog"),
            (40, "Building application shell"),
            (70, "Preparing service workspace"),
            (82, "Restoring workspace"),
            (90, "Applying interface theme"),
            (96, "Finalizing workspace"),
        ]
    finally:
        window.close()
