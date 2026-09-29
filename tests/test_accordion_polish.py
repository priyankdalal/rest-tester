from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def test_accordion_summary_is_optional_and_right_aligned(qt_app) -> None:
    from PyQt6.QtWidgets import QLabel

    from api_tester.widgets import AccordionSection

    section = AccordionSection("Request", QLabel("body"), summary="JSON / 200")
    section.resize(360, section.sizeHint().height())
    section.show()
    qt_app.processEvents()

    assert section.summary() == "JSON / 200"
    assert section.header.summary_label.objectName() == "accordionSummary"
    assert section.header.summary_label.isVisible()
    assert section.header.summary_label.geometry().right() <= section.header.rect().right()
    assert section.header.summary_label.geometry().left() > section.header.width() // 2
    assert section.header.summary_label.geometry().top() >= 6
    assert (
        section.header.rect().bottom()
        - section.header.summary_label.geometry().bottom()
        >= 6
    )
    assert section.header.property("hasSummary") is True


def test_collapsed_sections_hide_body_and_expose_state(qt_app) -> None:
    from PyQt6.QtWidgets import QLabel, QSizePolicy

    from api_tester.widgets import AccordionSection

    section = AccordionSection("Request", QLabel("body"))
    states: list[bool] = []
    section.expandedChanged.connect(states.append)
    section.show()

    assert not section.is_expanded()
    assert not section.body.isVisible()
    assert section.property("collapsed") is True
    assert section.header.property("collapsed") is True
    assert section.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed

    section.set_expanded(True)
    qt_app.processEvents()
    assert section.is_expanded()
    assert section.body.isVisible()
    assert section.property("expanded") is True
    assert section.header.property("expanded") is True
    assert section.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Preferred
    assert states == [True]

    section.set_expanded(False)
    assert not section.body.isVisible()
    assert section.property("collapsed") is True
    assert section.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed
    assert states == [True, False]


def test_add_section_keeps_existing_call_shape_and_updates_summary(qt_app) -> None:
    from PyQt6.QtWidgets import QLabel

    from api_tester.widgets import AccordionScrollArea

    accordion = AccordionScrollArea()
    section = accordion.add_section("Request", QLabel("body"))
    assert section.summary() == ""
    assert section.header.property("hasSummary") is False

    section.set_summary("3 fields")
    assert section.summary() == "3 fields"
    assert section.header.property("hasSummary") is True
