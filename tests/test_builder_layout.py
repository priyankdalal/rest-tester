"""Row actions remain readable in the schema-driven query and sort builders."""

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication, QPushButton

from api_tester import theme
from api_tester.builders import QueryBuilder, SortBuilder
from api_tester.schema import FilterSchema


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("mode", ["Light", "Dark"])
@pytest.mark.parametrize("width", [640, 1160])
@pytest.mark.parametrize("point_size", [10, 16])
@pytest.mark.parametrize("builder_type,column", [(QueryBuilder, 4), (SortBuilder, 3)])
def test_builder_row_actions_fit(app, mode, width, point_size, builder_type, column):
    original_font = app.font()
    theme.apply_theme(app, mode)
    schema = FilterSchema.from_dict({
        "name": "Brand",
        "fields": [{
            "name": "Id", "data_type": "Number", "sortable": True,
            "operators": [{"label": "Equal", "value": "eq"}],
        }],
    })
    builder = builder_type(schema)
    font = builder.font()
    font.setPointSize(point_size)
    builder.setFont(font)
    builder.resize(width, 500)
    try:
        if isinstance(builder, QueryBuilder):
            builder.add_condition()
            builder.add_condition()
        else:
            builder.add_entry()
            builder.add_entry()
        builder.show()
        for next_mode in (mode, "Dark" if mode == "Light" else "Light"):
            theme.apply_theme(app, next_mode)
            builder.refresh_theme()
            for _ in range(10):
                app.processEvents()
            for row in range(2):
                holder = builder.table.cellWidget(row, column)
                assert holder.width() >= holder.sizeHint().width()
                for button in holder.findChildren(QPushButton):
                    assert button.width() >= button.sizeHint().width()
                    assert button.height() >= button.sizeHint().height()
                    assert holder.rect().contains(button.geometry())

        holder = builder.table.cellWidget(0, column)
        if isinstance(builder, QueryBuilder):
            builder.table.cellWidget(0, 3).setText("")
            seed = next(button for button in holder.findChildren(QPushButton) if button.text() == "Seed")
            seed.click()
            assert builder.table.cellWidget(0, 3).text() == "1"
            assert builder.filter_string() == "Id__eq:=1;Id__eq:=1"
        else:
            builder.table.cellWidget(0, 2).setCurrentText("Descending")
            assert builder.sort_string() == "Id-,Id"
        remove = holder.findChild(QPushButton, "rowRemoveButton")
        remove.click()
        assert builder.table.rowCount() == 1
    finally:
        builder.close()
        builder.deleteLater()
        app.setFont(original_font)
        theme.apply_theme(app, "Light")
