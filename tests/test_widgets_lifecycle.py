import sys

from PyQt6 import sip
from PyQt6.QtWidgets import QApplication, QPushButton, QTableWidget, QVBoxLayout, QWidget

from api_tester.widgets import settle_table_rows


def test_deferred_row_fit_is_cancelled_when_table_is_destroyed(monkeypatch):
    app = QApplication.instance()
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, error, traceback: errors.append(error))
    table = QTableWidget(1, 1)
    holder = QWidget()
    layout = QVBoxLayout(holder)
    button = QPushButton("Tall control")
    button.setMinimumHeight(80)
    layout.addWidget(button)
    table.setCellWidget(0, 0, holder)
    table.show()
    app.processEvents()
    assert holder.sizeHint().height() > holder.height()
    original_height = table.rowHeight(0)
    settle_table_rows(table)
    assert table.rowHeight(0) > original_height
    sip.delete(table)
    app.processEvents()
    assert errors == []
