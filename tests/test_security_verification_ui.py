"""Offscreen authoring/approval tests; no network access is used."""

from __future__ import annotations

import copy
import json

import pytest
from PyQt6.QtWidgets import QLineEdit, QMessageBox

from api_tester import security_verification_ui as ui


def approved_dialog():
    dialog = ui.SecurityVerificationDialog()
    dialog.origin.setText("http://127.0.0.1:9876")
    dialog.approval.setChecked(True)
    return dialog


def test_default_approval_is_required_and_no_network(monkeypatch):
    dialog = ui.SecurityVerificationDialog()
    assert not dialog.approval.isChecked()
    assert not dialog.mutations.isChecked()
    assert not dialog.risks.isChecked()
    monkeypatch.setattr(ui, "execute_verification",
                        lambda *_: pytest.fail("Validation cannot send requests."))
    dialog.validate()
    assert "No requests sent" in dialog.status.text()
    with pytest.raises(ValueError):
        dialog.build_plan()
    dialog.reject()


def test_masked_identity_values_only_in_memory_and_clear_on_close():
    dialog = approved_dialog()
    dialog.add_identity("owner", "Authorization", "Bearer unique-memory-token")
    assert dialog.identities.cellWidget(0, 2).echoMode() == QLineEdit.EchoMode.Password
    plan = dialog.build_plan()
    assert plan.identities["owner"]["Authorization"] == "Bearer unique-memory-token"
    assert "unique-memory-token" not in dialog.cases.toPlainText()
    assert "unique-memory-token" not in repr(plan)
    dialog.reject()
    assert dialog.identities.rowCount() == 0


def test_plan_validation_has_no_requests_or_store_writes(monkeypatch):
    dialog = approved_dialog()
    monkeypatch.setattr(ui, "execute_verification",
                        lambda *_: pytest.fail("Validation cannot send requests."))
    dialog.validate()
    assert "Valid bounded plan" in dialog.status.text()
    assert "No requests sent" in dialog.status.text()
    dialog.reject()


@pytest.mark.parametrize("invalid_json", [
    '{"cases": [], "cleanup": []}',
    '{"cases": [{"name":"x","category":"API8","method":"GET","path":"/health"}],"cleanup":[]}',
    '{"cases": [{"name":"x","category":"API8","method":"GET","path":"//evil","expected_status":[200]}],"cleanup":[]}',
    '{"cases": {}, "cleanup": []}',
    '{"cases": [], "cleanup": [], "identities": {"token": "never-save"}}',
    '{"cases": [null], "cleanup": []}',
    "not-json-secret-token",
])
def test_invalid_authored_json_safe_error(invalid_json):
    dialog = approved_dialog()
    dialog.cases.setPlainText(invalid_json)
    dialog.validate()
    assert "Plan rejected" in dialog.status.text()
    assert "not-json-secret-token" not in dialog.status.text()
    dialog.reject()


def test_duplicate_identity_headers_and_credential_case_headers_rejected():
    dialog = approved_dialog()
    dialog.add_identity("owner", "Authorization", "Bearer a")
    dialog.add_identity("owner", "authorization", "Bearer b")
    with pytest.raises(ValueError, match="Duplicate"):
        dialog.build_plan()
    dialog.identities.setCurrentCell(1, 0)
    dialog.remove_identity()
    assert dialog.identities.rowCount() == 1
    authored = json.loads(dialog.cases.toPlainText())
    authored["cases"][0]["headers"] = {"Authorization": "Bearer must-use-identity"}
    dialog.cases.setPlainText(json.dumps(authored))
    with pytest.raises(ValueError, match="identity"):
        dialog.build_plan()
    dialog.reject()


def test_final_confirmation_decline_does_not_create_runner(monkeypatch):
    dialog = approved_dialog()
    confirmations = []
    def confirm(*args):
        confirmations.append(args)
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", confirm)
    monkeypatch.setattr(ui, "SecurityReviewDialog",
                        lambda *_args, **_kwargs: pytest.fail("Declined plan cannot run."))
    dialog.execute()
    assert len(confirmations) == 1
    assert "http://127.0.0.1:9876" in confirmations[0][2]
    assert "Assertions: 1" in confirmations[0][2]
    assert "mutating requests: 0" in confirmations[0][2]
    assert confirmations[0][-1] == QMessageBox.StandardButton.No
    dialog.reject()


def test_approved_runner_uses_shared_dialog_and_clears_credentials(monkeypatch):
    store = object()
    dialog = ui.SecurityVerificationDialog(store=store)
    dialog.origin.setText("http://127.0.0.1:9876")
    dialog.approval.setChecked(True)
    dialog.add_identity("owner", "Authorization", "Bearer memory-only-token")
    authored = json.loads(dialog.cases.toPlainText())
    authored["cases"][0]["identity"] = "owner"
    dialog.cases.setPlainText(json.dumps(authored))
    captured = {}
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *_: QMessageBox.StandardButton.Yes)
    def executor(plan, cancel):
        captured["plan"] = copy.deepcopy(plan)
        captured["cancel"] = cancel
        return "synthetic-report"
    monkeypatch.setattr(ui, "execute_verification", executor)
    class Runner:
        def __init__(self, scan, parent, *, store):
            captured["parent"] = parent
            captured["store"] = store
            self.scan = scan
        def exec(self):
            import threading
            captured["result"] = self.scan(threading.Event())
    monkeypatch.setattr(ui, "SecurityReviewDialog", Runner)
    dialog.execute()
    assert captured["parent"] is dialog
    assert captured["store"] is store
    assert captured["result"] == "synthetic-report"
    assert captured["plan"].approved is True
    assert captured["plan"].identities["owner"]["Authorization"] == "Bearer memory-only-token"
    assert dialog.identities.rowCount() == 0
    assert "memory-only-token" not in dialog.status.text()
    dialog.reject()


def test_mutation_gate_and_cleanup_summary(monkeypatch):
    dialog = approved_dialog()
    authored = {
        "cases": [{
            "name": "Controlled mutation", "category": "API6", "method": "POST",
            "path": "/workflow", "expected_status": [409], "fixture": "Synthetic conflict fixture",
        }],
        "cleanup": [{
            "name": "Cleanup", "category": "API6", "method": "DELETE",
            "path": "/fixture", "expected_status": [204], "fixture": "Synthetic cleanup",
        }],
    }
    dialog.cases.setPlainText(json.dumps(authored))
    with pytest.raises(ValueError, match="Mutations"):
        dialog.build_plan()
    dialog.mutations.setChecked(True)
    dialog.risks.setChecked(True)
    text = []
    def confirm(*args):
        text.append(args[2])
        return QMessageBox.StandardButton.No
    monkeypatch.setattr(QMessageBox, "question", confirm)
    dialog.execute()
    assert "Assertions: 1; reserved cleanup: 1; mutating requests: 2" in text[0]
    dialog.reject()
