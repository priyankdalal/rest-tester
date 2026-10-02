"""Shared test infrastructure for the Qt-heavy suite.

Every ``MainWindow`` built by a test creates roughly 5,500 widgets, and two
separate effects made the suite unusably slow:

* Closing a window does not destroy it, so leaked windows accumulated and every
  ``QApplication.setStyleSheet`` repolished all of them -- measured at 0.8s for
  the first window and over 70s by the fourth.
* Once the first window installs the 32KB application stylesheet, every widget
  built by a *later* test is polished against it at construction time. Clearing
  the sheet between tests took a repeated build from 16.4s back to 1.9s.

Dropping the leaked objects and resetting the stylesheet between tests fixes
both without touching the per-file fixtures.
"""

from __future__ import annotations

import gc
import os

import pytest

# Qt must be headless before any QApplication is constructed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session", autouse=True)
def _qt_application():
    """Keep Qt and process-wide signal stores alive across test modules."""
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        yield None
        return
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _release_qt_objects():
    try:
        from PyQt6 import sip
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        yield
        return
    app = QApplication.instance()
    existing = {
        sip.unwrapinstance(widget) for widget in app.topLevelWidgets()
    } if app is not None else set()
    yield

    gc.collect()
    try:
        from PyQt6.QtCore import QCoreApplication, QEvent, Qt
    except ImportError:  # PyQt6 is optional for non-GUI tests
        return
    app = QCoreApplication.instance()
    if app is None:
        return

    # Drop the application stylesheet *first*. Widgets are unpolished against
    # it as they are destroyed, so tearing down a window with the 32KB sheet
    # still installed is slow enough to blow a 180s per-test timeout.
    if hasattr(app, "setStyleSheet") and app.styleSheet():
        app.setStyleSheet("")

    # Tracebacks and signal closures may retain closed windows past gc.collect.
    # Preserve module fixtures, but explicitly destroy windows created by this test.
    # Popups (e.g. QCompleter's list) are owned and deleted by their QObject owner;
    # deleting them here would make the owner double-delete them.
    for widget in app.topLevelWidgets():
        if (
            widget.parent() is None
            and sip.unwrapinstance(widget) not in existing
            and widget.windowType() != Qt.WindowType.Popup
        ):
            widget.deleteLater()

    # ``deleteLater`` only takes effect once the queue is drained. Flush *only*
    # the deferred-delete events: a full ``processEvents`` also delivers timers
    # and queued signals into objects the test has already torn down, which
    # crashes the interpreter with 0xC0000409.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
