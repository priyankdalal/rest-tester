"""Transactional endpoint policy and service evidence editor tests."""

import copy
import json

import pytest

pytest.importorskip("PyQt6")
from PyQt6.QtWidgets import QApplication, QDialog, QFileDialog

from api_tester.security_metadata_ui import SecurityMetadataDialog


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


def test_endpoint_guidance_preserves_unknown_keys_and_explicit_anonymous(qt_app):
    metadata = {"extension": {"keep": [1]}, "document": {"unknown": "snapshot"}}
    original = copy.deepcopy(metadata)
    dialog = SecurityMetadataDialog(metadata)
    dialog.authentication.setCurrentIndex(2)
    dialog.security_override.setChecked(True)
    dialog.security.setPlainText('[{"bearer": [], "key": []}, {}]')
    dialog.sensitive_fields.setPlainText("email,password\nemail")
    dialog.accept()
    result = dialog.result_metadata()
    assert result["require_authentication"] is False
    assert result["security"] == [{"bearer": [], "key": []}, {}]
    assert result["sensitive_response_fields"] == ["email", "password"]
    assert result["extension"] == metadata["extension"]
    assert result["document"] == metadata["document"]
    assert metadata == original
    result["extension"]["keep"].append(2)
    assert dialog.result_metadata()["extension"]["keep"] == [1]


def test_unknown_auth_and_inherited_security_remove_only_explicit_overrides(qt_app):
    dialog = SecurityMetadataDialog({"require_authentication": True, "security": [], "extra": 1})
    dialog.authentication.setCurrentIndex(0)
    dialog.security_override.setChecked(False)
    dialog.accept()
    assert dialog.result_metadata() == {"extra": 1}


@pytest.mark.parametrize("security", ['{}', '[{"bearer": "scope"}]', '[{"bearer": [123]}]', '[null]', 'invalid'])
def test_invalid_security_is_explicit_and_does_not_accept(qt_app, security):
    original = {"security": []}
    dialog = SecurityMetadataDialog(original)
    dialog.security.setPlainText(security)
    dialog.accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert "Invalid security metadata" in dialog.error.text()
    assert dialog.result_metadata() == original


def test_cancel_does_not_mutate_service_snapshot(qt_app):
    metadata = {"document": {"openapi": "3.1.0", "paths": {}}, "source_file": "source.json",
                "source_fingerprint": "hash", "extension": {"keep": True}}
    original = copy.deepcopy(metadata)
    dialog = SecurityMetadataDialog(metadata, service=True)
    dialog._remove_evidence()
    dialog.reject()
    assert dialog.result_metadata() == original
    assert metadata == original


def test_service_import_sanitizes_and_deliberately_replaces_source(qt_app, monkeypatch, tmp_path):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps({
        "openapi": "3.1.0", "paths": {},
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "password": {"default": "sensitive"},
    }), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    original = {"document": {"openapi": "3.0.0", "paths": {}}, "unknown": 1}
    dialog = SecurityMetadataDialog(original, service=True)
    dialog._import_contract()
    assert dialog.version.text() == "3.1.0"
    assert dialog.source.isReadOnly()
    assert "bearer" in dialog.schemes.toPlainText()
    dialog.accept()
    result = dialog.result_metadata()
    assert result["document"]["password"]["default"] == "[REDACTED]"
    assert result["unknown"] == 1
    assert result["source_fingerprint"]
    assert original["document"]["openapi"] == "3.0.0"


def test_service_accept_without_import_preserves_exact_evidence(qt_app):
    metadata = {"document": {"openapi": "3.1.0", "paths": {}, "x-custom": {"default": "literal"}},
                "source_fingerprint": "original"}
    dialog = SecurityMetadataDialog(metadata, service=True)
    dialog.accept()
    assert dialog.result_metadata() == metadata


def test_service_remove_and_bad_import_are_explicit(qt_app, monkeypatch, tmp_path):
    metadata = {"document": {"openapi": "3.1.0", "paths": {}}, "source_file": "old",
                "source_fingerprint": "hash", "keep": 1}
    path = tmp_path / "invalid.json"
    path.write_text('{"paths": {}}', encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args: (str(path), ""))
    dialog = SecurityMetadataDialog(metadata, service=True)
    dialog._import_contract()
    assert "Contract import failed" in dialog.error.text()
    assert dialog.source.text() == "old"
    dialog._remove_evidence()
    dialog.accept()
    assert dialog.result_metadata() == {"keep": 1}
