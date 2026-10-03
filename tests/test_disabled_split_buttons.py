import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMenu, QToolButton

from api_tester import theme


@pytest.mark.parametrize("mode", ["Light", "Dark"])
@pytest.mark.parametrize("name", ["endpointSendButton", "endpointSplitButton"])
def test_disabled_split_button_has_no_divider(mode, name):
    app = QApplication.instance() or QApplication([])
    theme.apply_theme(app, mode)
    button = QToolButton()
    button.setObjectName(name)
    button.setProperty("accent", True)
    button.setText("Send")
    button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
    button.setMenu(QMenu(button))
    button.resize(160, 36)
    try:
        button.show()
        app.processEvents()
        button.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
        enabled = button.grab().toImage()
        button.setEnabled(False)
        app.processEvents()
        disabled = button.grab().toImage()
        # Above the arrow and text, only the enabled menu divider interrupts
        # the button's otherwise uniform fill.
        y = 5
        left, right = button.width() - 40, button.width() - 20
        assert len({enabled.pixelColor(x, y).name() for x in range(left, right)}) > 1
        assert len({disabled.pixelColor(x, y).name() for x in range(left, right)}) == 1
    finally:
        button.close()
        button.deleteLater()
        theme.apply_theme(app, "Light")
