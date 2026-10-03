from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

from api_tester.ai.ui import AskAiDialog


def test_warning_uses_available_width_and_settings_opens_callback(tmp_path):
    app = QApplication.instance() or QApplication([])
    opened = []
    dialog = AskAiDialog(
        None, catalog=lambda: None, secrets=lambda: (), on_open=lambda _request: None,
        on_settings=lambda: opened.append("settings"), settings_path=tmp_path / "settings.json",
    )
    try:
        dialog.show()
        dialog.model_summary.setText("Sarvam AI: Sarvam AI / sarvam-105b (Hosted)")
        for width in (640, 800, 1200):
            dialog.resize(width, 700)
            app.processEvents()
            available = dialog.width() - 28
            natural = dialog.review_warning.fontMetrics().horizontalAdvance(dialog.review_warning.text()) + 28
            assert dialog.warning_group.width() >= min(available, natural) - 24
            assert abs(dialog.warning_group.geometry().center().x() - dialog.rect().center().x()) < 4
            assert dialog.review_warning.x() - dialog.review_warning_icon.geometry().right() <= 9
            assert dialog.review_warning.alignment() == Qt.AlignmentFlag.AlignCenter
            assert not dialog.model_summary.wordWrap()
            assert dialog.model_summary.width() >= dialog.model_summary.fontMetrics().horizontalAdvance(
                dialog.model_summary.text()
            )
            assert dialog.model_summary.toolTip() == "Sarvam AI: Sarvam AI / sarvam-105b (Hosted)"
            assert dialog.settings_button.geometry().left() > dialog.model_summary.geometry().right()
        assert dialog.settings_button.cursor().shape() == Qt.CursorShape.PointingHandCursor
        dialog.settings_button.click()
        assert opened == ["settings"]
        dialog._set_busy(True)
        dialog.settings_button.click()
        assert opened == ["settings"]
        dialog._set_busy(False)
        dialog.settings_button.click()
        assert opened == ["settings", "settings"]
    finally:
        dialog.close()


def test_settings_is_hidden_without_callback_and_disabled_for_history(tmp_path):
    dialog = AskAiDialog(
        None, catalog=lambda: None, secrets=lambda: (), on_open=lambda _request: None,
        settings_path=tmp_path / "settings.json",
    )
    try:
        assert dialog.settings_button.isHidden()
        dialog.read_only_result = True
        dialog._set_busy(False)
        assert not dialog.settings_button.isEnabled()
    finally:
        dialog.close()
