"""Focused, headless tests for the shared passive security report viewer."""

from __future__ import annotations

import copy
import json
import threading
import time
from unittest.mock import Mock

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtCore import QCoreApplication, QDate, QEvent, Qt
from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox

from api_tester.security import SecurityReport, scan_catalog
from api_tester import security_ui
from api_tester.security_ui import SecurityReportPanel, SecurityReviewDialog


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)


def _wait(app, condition, timeout=3):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert condition(), "Qt operation did not finish within the test deadline"


def _report():
    def finding(rule, severity, path):
        return {
            "rule_id": rule, "title": "<b>Untrusted title</b>", "severity": severity,
            "category": "authorization", "evidence_class": "static", "service": "TrialAuth",
            "endpoint_id": rule, "method": "GET", "path": path,
            "location": "<img src='file:///secret'>", "evidence": "<script>alert(1)</script>",
            "explanation": "Review access controls", "remediation": "Verify policy",
            "limitation": "Static evidence is not proof", "disposition": "open", "reason": "",
        }
    return SecurityReport.from_dict({
        "id": "report-1", "created_at": "2026-10-13T09:00:00Z", "mode": "static",
        "scope": "TrialAuth", "catalog_fingerprint": "<b>catalog</b>",
        "source_fingerprint": "source-123", "rule_version": "1", "environment": "local",
        "endpoints_reviewed": 2, "state": "complete",
        "findings": [finding("AUTH-1", "high", "/Users"), finding("AUTH-2", "low", "/Teams")],
        "coverage": [{"category": "runtime authorization", "status": "unknown",
                      "explanation": "<a href='https://untrusted'>Not tested</a>"}],
        "diagnostics": ["héllo <b>diagnostic</b>"],
    })


def _complete(app, *, store=None):
    dialog = SecurityReviewDialog(lambda cancel: _report(), store=store)
    dialog.show()
    _wait(app, lambda: dialog.report is not None and not dialog._running)
    return dialog


def test_scan_starts_on_show_and_completes_off_ui_thread(qt_app):
    calls = []
    ui_thread = threading.get_ident()

    def scan(cancel):
        calls.append((threading.get_ident(), cancel))
        return _report()

    store = Mock()
    dialog = SecurityReviewDialog(scan, store=store)
    assert not calls
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert calls[0][0] != ui_thread
    assert isinstance(calls[0][1], threading.Event)
    assert dialog.windowTitle() == "API Security Review"
    assert "2 endpoints reviewed" in dialog.summary.text()
    assert "0 partially assessed; 1 not assessed" in dialog.summary.text()
    assert "Frozen snapshot" in dialog.summary.text()
    assert "require a rescan" in dialog.summary.text()
    assert "catalog" in dialog.metadata.text()
    assert dialog.metadata.textFormat() == Qt.TextFormat.PlainText
    assert not dialog.progress.isVisible()
    store.save_security_report.assert_called_once_with(dialog.report.to_dict())
    dialog.close()


def test_redesigned_cards_filters_copy_and_diagnostics(qt_app):
    report = _report()
    report.findings[1].category = "configuration"
    report.findings[1].service = "Second service"
    dialog = SecurityReviewDialog(lambda cancel: report)
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert dialog.state_badge.text() == "Complete"
    assert dialog.metric_values["endpoints"].text() == "2"
    assert dialog.metric_values["findings"].text() == "2"
    assert dialog.metadata_values["scope"].text() == "TrialAuth"
    assert dialog.metadata_values["environment"].text() == "local"
    dialog.copy_fingerprint.click()
    assert qt_app.clipboard().text() == report.catalog_fingerprint
    dialog.category_filter.setCurrentText("configuration")
    assert dialog.table.rowCount() == 1
    dialog.service_filter.setCurrentText("TrialAuth")
    assert dialog.table.rowCount() == 0
    assert not dialog.apply_disposition.isEnabled()
    assert dialog.empty_findings.isVisible()
    dialog.clear_filters_button.click()
    assert dialog.table.rowCount() == 2
    assert dialog.category_filter.currentIndex() == dialog.service_filter.currentIndex() == 0
    dialog.diagnostics_banner.click()
    assert dialog.tabs.currentWidget() is dialog.diagnostics
    assert "diagnostic" in dialog.diagnostics_banner.toolTip()
    dialog.close()


@pytest.mark.parametrize("mode", ["Light", "Dark"])
def test_redesign_palette_and_empty_layout(qt_app, mode):
    from api_tester import theme
    from api_tester.icons import icon

    theme.apply_theme(qt_app, mode)
    report = _report()
    report.findings.clear()
    dialog = SecurityReviewDialog(lambda cancel: report)
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert theme.BACKGROUND in dialog.styleSheet()
    assert dialog.empty_findings.isVisible()
    assert dialog.table.height() >= 180
    assert dialog.close_button.isVisible()
    for name in ("security-shield", "calendar", "fingerprint", "security-scope",
                 "security-environment", "security-report", "security-search", "history"):
        assert not icon(name).pixmap(24).isNull()
    dialog.close()
    theme.apply_theme(qt_app, "Light")


@pytest.mark.parametrize("mode", ["Light", "Dark"])
def test_review_reflows_without_clipping_at_compact_sizes(qt_app, mode):
    from api_tester import theme
    from PyQt6.QtCore import QPoint

    theme.apply_theme(qt_app, mode)
    dialog = _complete(qt_app)
    for width, height, columns in ((1380, 960, 5), (1000, 700, 3),
                                    (800, 600, 2), (640, 480, 2), (1380, 960, 5)):
        dialog.resize(width, height)
        for _ in range(8):
            qt_app.processEvents()
        assert (dialog.width(), dialog.height()) == (width, height)
        assert dialog._responsive_columns == columns
        assert dialog.content.width() <= dialog.scroll_area.viewport().width()
        for widget in dialog._footer_widgets:
            position = widget.mapTo(dialog, QPoint(0, 0))
            assert position.x() >= 0 and position.y() >= 0
            assert position.x() + widget.width() <= width
            assert position.y() + widget.height() <= height
        for widgets in (dialog._metadata_cards, dialog._filter_widgets,
                        dialog._disposition_widgets, dialog._expiry_widgets):
            for widget in widgets:
                position = widget.mapTo(dialog.content, QPoint(0, 0))
                assert position.x() >= 0
                assert position.x() + widget.width() <= dialog.content.width()
        if width <= 1000:
            assert dialog.scroll_area.verticalScrollBar().maximum() > 0
            dialog.scroll_area.ensureWidgetVisible(dialog.apply_disposition)
            for _ in range(4):
                qt_app.processEvents()
            assert dialog.scroll_area.verticalScrollBar().value() > 0
        assert dialog.report is not None
        assert dialog.table.rowCount() == 2
    dialog.close()
    theme.apply_theme(qt_app, "Light")


def test_scan_failure_is_explicit_not_a_completed_report(qt_app):
    def scan(cancel):
        raise RuntimeError("<b>scanner broke</b>")

    store = Mock()
    dialog = SecurityReviewDialog(scan, store=store)
    dialog.show()
    _wait(qt_app, lambda: dialog._started and dialog._thread is not None and not dialog._running)
    assert dialog.report is None
    assert "Security review failed" in dialog.status.text()
    assert "scanner broke" in dialog.status.text()
    assert "failed" in dialog.summary.text()
    assert not any(button.isEnabled() for button in dialog.export_buttons.values())
    store.save_security_report.assert_not_called()
    dialog.close()


@pytest.mark.parametrize("closing", ["cancel", "close", "reject", "done"])
def test_cooperative_cancel_keeps_thread_alive_until_finished(qt_app, closing):
    started = threading.Event()
    release = threading.Event()
    seen_cancel = []

    def scan(cancel):
        started.set()
        release.wait(3)
        seen_cancel.append(cancel.is_set())
        return _report()

    store = Mock()
    dialog = SecurityReviewDialog(scan, store=store)
    dialog.show()
    try:
        _wait(qt_app, started.is_set)
        if closing == "cancel":
            dialog.cancel_scan()
        elif closing == "close":
            dialog.close()
        elif closing == "reject":
            dialog.reject()
        else:
            dialog.done(QDialog.DialogCode.Rejected)
        assert dialog.isVisible()
        assert dialog._thread.isRunning()
        assert dialog._cancel.is_set()
    finally:
        release.set()
        _wait(qt_app, lambda: not dialog._running)
    assert seen_cancel == [True]
    assert dialog.report is None
    assert "cancelled" in dialog.status.text()
    store.save_security_report.assert_not_called()
    if closing != "cancel":
        assert not dialog.isVisible()
    dialog.close()


def test_filtering_plain_text_details_and_coverage(qt_app):
    dialog = _complete(qt_app)
    assert dialog.table.rowCount() == 2
    assert "<script>alert(1)</script>" in dialog.details.toPlainText()
    assert "Limitations\nStatic evidence" in dialog.details.toPlainText()
    assert "Location\n<img" in dialog.details.toPlainText()
    assert "Remediation\nVerify policy" in dialog.details.toPlainText()
    assert "Confidence\nhigh for the documented condition; enforcement unverified" in dialog.details.toPlainText()
    assert dialog.coverage_table.item(0, 1).text() == "unknown"
    assert "<b>diagnostic</b>" in dialog.diagnostics.toPlainText()
    dialog.severity_filter.setCurrentText("high")
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == "AUTH-1"
    dialog.text_filter.setText("teams")
    assert dialog.table.rowCount() == 0
    assert not dialog.apply_disposition.isEnabled()
    dialog.severity_filter.setCurrentIndex(0)
    assert dialog.table.rowCount() == 1
    assert dialog.table.item(0, 1).text() == "AUTH-2"
    dialog.text_filter.clear()
    assert dialog.table.rowCount() == 2
    dialog.close()


@pytest.mark.parametrize("disposition", ["acknowledged", "accepted risk", "false positive"])
def test_disposition_requires_reason_preserves_original_evidence(qt_app, disposition):
    saved = []
    store = Mock()
    store.save_security_report.side_effect = lambda payload: saved.append(copy.deepcopy(payload))
    dialog = _complete(qt_app, store=store)
    original = dialog.report.findings[0].evidence
    dialog.disposition.setCurrentText(disposition)
    dialog.reason.setText("  ")
    dialog.apply_disposition.click()
    assert dialog.report.findings[0].disposition == "open"
    assert len(saved) == 1
    assert "Reason required" in dialog.status.text()
    dialog.reason.setText(" Independently reviewed ")
    dialog.apply_disposition.click()
    assert len(saved) == 2
    assert saved[-1]["findings"][0]["reason"] == "Independently reviewed"
    assert saved[-1]["findings"][0]["disposition"] == disposition
    assert len(dialog.report.findings) == 2
    assert dialog.report.findings[0].evidence == original
    dialog.disposition.setCurrentText("open")
    dialog.reason.clear()
    dialog.apply_disposition.click()
    assert dialog.report.findings[0].disposition == "open"
    dialog.close()


@pytest.mark.parametrize("kind", ["JSON", "HTML", "CSV"])
def test_exports_utf8_and_engine_helpers(qt_app, monkeypatch, kind):
    dialog = _complete(qt_app)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("report.out", ""))
    html = Mock(return_value="héllo HTML")
    csv = Mock(return_value="héllo CSV")
    monkeypatch.setattr(security_ui, "report_html", html)
    monkeypatch.setattr(security_ui, "report_csv", csv)
    write = Mock()
    monkeypatch.setattr(security_ui.Path, "write_text", write)
    dialog.export_report(kind)
    assert write.call_args.kwargs == {"encoding": "utf-8"}
    payload = write.call_args.args[0]
    if kind == "JSON":
        assert json.loads(payload) == dialog.report.to_dict()
    else:
        assert payload == f"héllo {kind}"
        (html if kind == "HTML" else csv).assert_called_once_with(dialog.report)
    dialog.close()


def test_export_cancel_and_failure(qt_app, monkeypatch):
    dialog = _complete(qt_app)
    write = Mock(side_effect=OSError("Permission denied"))
    monkeypatch.setattr(security_ui.Path, "write_text", write)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("", ""))
    dialog.export_report("JSON")
    write.assert_not_called()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: ("report.json", ""))
    dialog.export_report("JSON")
    assert "Report export failed" in dialog.status.text()
    assert "Permission denied" in dialog.status.text()
    dialog.close()


def test_persistence_failure_is_visible(qt_app):
    store = Mock()
    store.save_security_report.side_effect = OSError("disk full")
    dialog = _complete(qt_app, store=store)
    assert "Report persistence failed" in dialog.status.text()
    assert "disk full" in dialog.status.text()
    assert dialog.report is not None
    dialog.close()


def test_history_loads_completed_snapshot_without_rescanning(qt_app, monkeypatch):
    store = Mock()
    previous = _report().to_dict()
    previous["id"] = "previous"
    previous["mode"] = "<b>saved</b>"
    store.security_reports.return_value = [
        {**previous, "id": "failed", "state": "failed"}, previous
    ]
    store.security_report.return_value = previous
    scan = Mock(return_value=_report())
    dialog = SecurityReviewDialog(scan, store=store)
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: (args[3][0], True))
    dialog.history_button.click()
    store.security_reports.assert_called_once_with(limit=50)
    store.security_report.assert_called_once_with("previous")
    assert dialog.report.id == "previous"
    assert "historical snapshot" in dialog.heading.text()
    assert "Historical snapshot" in dialog.summary.text()
    assert "require a rescan" in dialog.summary.text()
    assert "not a current scan" in dialog.status.text()
    assert "<b>saved</b>" in dialog.metadata.text()
    scan.assert_called_once()
    assert store.save_security_report.call_count == 1
    dialog.close()


def test_reason_and_scope_credentials_are_masked_before_persistence(qt_app):
    report = _report()
    report.scope = "Bearer scope-token https://user:password@example.test"
    report.findings[0].reason = "Previously reviewed Bearer previous-token"
    saved = []
    store = Mock()
    store.save_security_report.side_effect = lambda payload: saved.append(copy.deepcopy(payload))
    dialog = SecurityReviewDialog(lambda cancel: report, store=store)
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert "scope-token" not in json.dumps(saved)
    assert "previous-token" not in json.dumps(saved)
    assert "user:password" not in dialog.metadata.text()
    dialog.disposition.setCurrentText("acknowledged")
    dialog.reason.setText(
        "Reviewed Bearer pasted-token https://user:password@example.test?access_token=url-token"
    )
    dialog.apply_disposition.click()
    payload = json.dumps(saved[-1])
    for secret in ("pasted-token", "url-token", "user:password"):
        assert secret not in payload
        assert secret not in dialog.details.toPlainText()
        assert secret not in dialog.reason.text()
    assert "Reviewed" in dialog.report.findings[0].reason
    assert "[REDACTED]" in dialog.report.findings[0].reason
    dialog.close()


def test_failed_report_is_not_presented_as_clean(qt_app):
    report = _report()
    report.state = "failed"
    report.findings.clear()
    report.diagnostics = ["Unable to finish review"]
    dialog = SecurityReviewDialog(lambda cancel: report)
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert "State: failed" in dialog.summary.text()
    assert dialog.status.text() == "Review state: failed"
    assert "Unable to finish review" in dialog.diagnostics.toPlainText()
    dialog.close()


def test_disposition_persistence_failure_is_not_overwritten(qt_app):
    store = Mock()
    dialog = _complete(qt_app, store=store)
    store.save_security_report.side_effect = OSError("disk full")
    dialog.disposition.setCurrentText("acknowledged")
    dialog.reason.setText("Reviewed independently")
    dialog.apply_disposition.click()
    assert "Report persistence failed" in dialog.status.text()
    assert "disk full" in dialog.status.text()
    assert "Disposition updated" not in dialog.status.text()
    dialog.close()


def test_real_catalog_report_counts_partial_and_unassessed_coverage(qt_app):
    contract = {
        "openapi": "3.1.0",
        "info": {"title": "Demo", "version": "1"},
        "security": [{"bearer": []}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "paths": {"/items": {"get": {"responses": {
            "200": {"description": "OK", "content": {"application/json": {"schema": {
                "type": "object", "properties": {"id": {"type": "integer"}},
            }}}},
            "401": {"description": "Unauthenticated"},
            "403": {"description": "Forbidden"},
        }}}},
    }
    document = {"services": [{
        "name": "Demo", "repository": "", "security_metadata": {"document": contract},
        "endpoints": [{"id": "demo", "service": "Demo", "method": "GET", "path": "/items"}],
    }]}
    dialog = SecurityReviewDialog(lambda cancel: scan_catalog(document, cancel))
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert dialog.report.endpoints_reviewed == 1
    assert {c.category.split(":")[0] for c in dialog.report.coverage if c.status == "partial"} == {
        "API2", "API4", "API7", "API8", "API9",
    }
    assert "5 partially assessed; 5 not assessed" in dialog.summary.text()
    assert "unknown coverage" not in dialog.summary.text()
    dialog.close()


def test_empty_catalog_summary_is_not_a_clean_result(qt_app):
    dialog = SecurityReviewDialog(lambda cancel: scan_catalog({"services": []}, cancel))
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert "0 endpoints reviewed" in dialog.summary.text()
    assert "no endpoint evidence, not a clean result" in dialog.summary.text()
    assert "Catalog has no services" in dialog.summary.text()
    assert "Diagnostics:" in dialog.summary.text()
    dialog.close()


def test_pdf_export_uses_qt_printer(qt_app, monkeypatch, tmp_path):
    dialog = _complete(qt_app)
    path = tmp_path / "security.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(path), ""))
    dialog.export_report("PDF")
    assert path.read_bytes().startswith(b"%PDF")
    dialog.close()


def test_freshness_timer_and_historical_label(qt_app):
    current = [_report().catalog_fingerprint]
    dialog = SecurityReviewDialog(lambda cancel: _report(), current_fingerprint=lambda: current[0])
    dialog._freshness_timer.setInterval(5)
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert "matches" in dialog.freshness.text()
    current[0] = "changed"
    _wait(qt_app, lambda: "STALE" in dialog.freshness.text())
    dialog._display_report(dialog.report, historical=True)
    assert "Historical" in dialog.freshness.text()
    assert "STALE" not in dialog.freshness.text()
    dialog.close()


def test_expiration_validation_and_reopen(qt_app):
    dialog = _complete(qt_app)
    dialog.disposition.setCurrentText("accepted risk")
    dialog.reason.setText("Approved independently")
    dialog.expiry_enabled.setChecked(True)
    dialog.expiry_date.setDate(QDate.currentDate())
    dialog._update_disposition()
    assert dialog.report.findings[0].disposition == "open"
    assert "Invalid expiration" in dialog.status.text()
    dialog.expiry_date.setDate(QDate.currentDate().addDays(7))
    expected = dialog.expiry_date.date().toString("yyyy-MM-dd")
    dialog._update_disposition()
    assert dialog.report.findings[0].expires_at == expected
    assert expected in dialog.details.toPlainText()
    dialog.reopen_button.click()
    assert dialog.report.findings[0].disposition == "open"
    assert dialog.report.findings[0].expires_at == ""
    dialog.close()


@pytest.mark.parametrize("state", ["complete", "partial", "failed", "cancelled"])
def test_comparison_plain_text_export_and_partial_guard(qt_app, monkeypatch, state):
    baseline = _report().to_dict()
    baseline.update(id="baseline", state=state)
    baseline["findings"] = baseline["findings"][:1]
    store = Mock()
    store.security_reports.return_value = [baseline, {**baseline, "id": "different", "scope": "other"}]
    store.security_report.return_value = baseline
    dialog = _complete(qt_app, store=store)
    monkeypatch.setattr(QInputDialog, "getItem", lambda *args: (args[3][0], True))
    dialog._compare()
    assert "Baseline: baseline" in dialog.comparison.toPlainText()
    if state == "complete":
        assert "<b>Untrusted title</b>" in dialog.comparison.toPlainText()
        assert "&lt;b&gt;" in dialog._export_html()
    assert dialog.report.comparison["baseline_id"] == "baseline"
    if state != "complete":
        assert dialog.report.comparison["resolved"] == []
        assert dialog.report.comparison["incomparable"]
    dialog.close()


def test_active_report_disclaimer(qt_app):
    report = _report()
    report.mode = "active verification"
    dialog = SecurityReviewDialog(lambda cancel: report)
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert "explicitly approved requests" in dialog.disclaimer.text()
    assert "no extra requests" not in dialog.disclaimer.text()
    dialog.close()


def test_panel_auto_is_background_persisted_and_reused(qt_app):
    store = Mock()
    report = _report()
    inherited = report.to_dict()
    inherited["findings"][0].update(disposition="acknowledged", reason="Earlier review")
    store.inherit_security_dispositions.return_value = inherited
    calls = []
    panel = SecurityReportPanel()
    ui_thread = threading.get_ident()

    def scan(cancel):
        calls.append(threading.get_ident())
        return report

    panel.configure(scan, store=store, auto=True)
    _wait(qt_app, lambda: panel.report is not None and panel._thread is None)
    assert calls == [calls[0]]
    assert calls[0] != ui_thread
    assert panel._dialog is None
    assert panel.report.findings[0].disposition == "acknowledged"
    store.save_security_report.assert_called_once()
    panel.review()
    qt_app.processEvents()
    assert panel._dialog.report is panel.report
    assert len(calls) == 1
    panel.clear()
    assert panel.report is None
    assert not panel.review_button.isEnabled()
    assert panel.shutdown()
    panel.close()


def test_panel_clear_drops_old_worker_result_and_reconfigures(qt_app):
    started = threading.Event()
    release = threading.Event()
    store = Mock()
    panel = SecurityReportPanel()

    def old_scan(cancel):
        started.set()
        release.wait(3)
        return _report()

    panel.configure(old_scan, store=store, auto=True)
    _wait(qt_app, started.is_set)
    panel.clear()
    assert not panel.shutdown()
    panel.configure(lambda cancel: _report(), store=store, auto=True)
    release.set()
    _wait(qt_app, lambda: panel.report is not None and panel._thread is None)
    store.save_security_report.assert_called_once()
    panel.close()


def test_panel_destruction_cancels_without_destroying_running_thread(qt_app):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    cancellation = []
    store = Mock()
    panel = SecurityReportPanel()

    def scan(cancel):
        cancellation.append(cancel)
        started.set()
        release.wait(3)
        return _report()

    panel.configure(scan, store=store, auto=True)
    _wait(qt_app, started.is_set)
    worker = panel._thread
    worker.finished.connect(finished.set)
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert cancellation[0].is_set()
    assert worker.isRunning()
    release.set()
    _wait(qt_app, finished.is_set)
    store.save_security_report.assert_not_called()


def test_panel_manual_saved_scan_and_shutdown_gate(qt_app):
    started = threading.Event()
    release = threading.Event()
    panel = SecurityReportPanel()
    payload = _report().to_dict()

    def saved_scan(cancel):
        started.set()
        release.wait(3)
        return SecurityReport.from_dict(payload)

    panel.configure(saved_scan)
    panel.review()
    _wait(qt_app, started.is_set)
    dialog = panel._dialog
    assert not panel.shutdown()
    release.set()
    _wait(qt_app, lambda: not dialog._running)
    assert panel.shutdown()
    panel.close()


def test_saved_comparison_reappears_in_historical_view(qt_app):
    report = _report()
    report.comparison = {
        "baseline_id": "baseline", "incomparable": False, "reason": "",
        "new": [vars(report.findings[0])], "resolved": [], "changed": [], "unchanged": [],
    }
    dialog = SecurityReviewDialog(lambda cancel: report)
    dialog._display_report(SecurityReport.from_dict(report.to_dict()), historical=True)
    assert "Baseline: baseline" in dialog.comparison.toPlainText()
    assert "<b>Untrusted title</b>" in dialog.comparison.toPlainText()
    dialog.close()


def test_active_verification_uses_mode_appropriate_wording(qt_app):
    payload = _report().to_dict()
    payload["mode"] = "active verification"
    payload["coverage"] = [
        {"category": f"Assertion {index}", "status": status, "explanation": "Bounded check"}
        for index, status in enumerate(("verified", "assertion failed", "unknown", "not tested"))
    ]
    report = SecurityReport.from_dict(payload)
    dialog = SecurityReviewDialog(lambda cancel: report)
    assert "no active testing" not in dialog.disclaimer.text()
    dialog.show()
    _wait(qt_app, lambda: dialog.report is not None and not dialog._running)
    assert "explicitly approved requests" in dialog.disclaimer.text()
    assert "Passive" not in dialog.disclaimer.text()
    assert "1 verified; 1 assertion failed; 1 unknown; 1 not tested" in dialog.summary.text()
    assert "0 partially assessed" not in dialog.summary.text()
    dialog.close()
