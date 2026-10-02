import sys

from PyQt6 import sip
from PyQt6.QtWidgets import QApplication

from api_tester import main
from api_tester.catalog import Catalog


def test_queued_probe_completion_is_cancelled_when_window_is_destroyed(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(main, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(main, "load_catalog", lambda *args, **kwargs: Catalog((), {}, {}, {}))
    monkeypatch.setattr(main.ConnectionProbe, "run", lambda self: None)
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, error, traceback: errors.append(error))
    window = main.MainWindow()
    window.app_settings.active_service = "Test"
    window._refresh_connection_state()
    probes = list(window._connection_probes)
    assert probes
    assert all(probe.wait(1000) for probe in probes)
    window.close()
    sip.delete(window)
    assert all(sip.isdeleted(probe) for probe in probes)
    QApplication.instance().processEvents()
    assert errors == []
