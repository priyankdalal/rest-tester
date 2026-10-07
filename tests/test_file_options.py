from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import shlex

import pytest
import requests
from PyQt6.QtWidgets import QApplication, QDialog, QMessageBox

from api_tester.catalog import Catalog, Endpoint, Parameter, Service
from api_tester.client import execute_endpoint, generate_curl, generate_python, prepare_endpoint_request
from api_tester.file_options import resolve_file_options
from api_tester.file_options_ui import FileOptionsDialog
from api_tester.request_editor import RequestEditor
from api_tester.saved_requests import SavedRequest
from api_tester.suite import TestCase as Case, TestSuite as Suite, load_suite, save_suite
from api_tester.viewers import FilePicker


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def upload(tmp_path):
    path = tmp_path / "sample.png"
    path.write_bytes(b"file-content")
    endpoint = Endpoint(
        "upload", "Demo", "Items", "Upload", "POST", "/upload",
        parameters=(Parameter("File", "form", "IFormFile", True),
                    Parameter("Label", "form", "string", False)),
        form_schema="Upload",
    )
    return endpoint, path


def catalog_for(endpoint):
    return Catalog(
        (Service("Demo", "Demo", "https://example.test", (endpoint,)),), {}, {}, {},
        form_schemas={"Upload": {"fields": [
            {"name": "File", "kind": "file", "required": True, "clr_type": "IFormFile"},
            {"name": "Label", "kind": "text"},
        ]}},
    )


def upload_values(path):
    return {
        "form:File": str(path), "form:Label": "test",
        "file:File:filename": "renamed.pdf",
        "file:File:content_type": "application/pdf",
        "file:File:header:X-Part-Id": "part-1",
    }


def prepared(upload, values=None, **kwargs):
    endpoint, path = upload
    return prepare_endpoint_request(
        endpoint, "https://example.test", "", "", values or {"form:File": str(path)},
        None, **kwargs,
    )


def test_default_detection_and_fallback(upload):
    request = prepared(upload)
    options = request.file_options["File"]
    assert options.filename == "sample.png"
    assert options.content_type == "image/png"
    assert not options.headers
    assert resolve_file_options("File", "unknown.no-such-extension", {}).content_type == "application/octet-stream"


def test_override_filename_drives_auto_content_type(upload):
    _, path = upload
    request = prepared(upload, {"form:File": str(path), "file:File:filename": "renamed.pdf"})
    assert request.file_options["File"].content_type == "application/pdf"
    request = prepared(upload, upload_values(path))
    assert request.file_options["File"].headers == {"X-Part-Id": "part-1"}


@pytest.mark.parametrize("key,value", [
    ("filename", "bad\nname.png"), ("content_type", "image/png\r\nInjected: true"),
    ("content_type", "not-a-media-type"), ("header:Bad Name", "private"),
    ("content_type", "application/pdf;missing=value;invalid"),
    ("header:X-Id", "private\ninjected"), ("header:Content-Type", "image/png"),
    ("header:Content-Disposition", "attachment"), ("header:Content-Length", "999"),
])
def test_invalid_or_structural_file_headers_are_rejected(upload, key, value):
    _, path = upload
    with pytest.raises(ValueError):
        prepared(upload, {"form:File": str(path), f"file:File:{key}": value})


def test_outer_content_type_is_rejected_to_preserve_boundary(upload):
    with pytest.raises(ValueError, match="boundary"):
        prepared(upload, custom_headers={"content-type": "multipart/form-data"})


@pytest.fixture
def transport(monkeypatch):
    calls = []
    handles = []

    def request(method, url, **kwargs):
        files = kwargs.get("files") or {}
        handles.extend(item[1] for item in files.values())
        wire = requests.Request(method, url, headers=kwargs["headers"],
                                data=kwargs.get("data"), files=files or None).prepare()
        calls.append(wire)
        return SimpleNamespace(
            status_code=200, content=b"{}", headers={"content-type": "application/json"},
            url=url, request=wire, reason="OK", raise_for_status=lambda: None,
        )

    monkeypatch.setattr("api_tester.client.requests.request", request)
    monkeypatch.setattr("api_tester.client.requests.Session.request", lambda self, *args, **kwargs: request(*args, **kwargs))
    monkeypatch.setattr("api_tester.client.measure_connection", lambda *args: {})
    return calls, handles


def test_actual_multipart_bytes_include_options_and_boundary(upload, transport):
    endpoint, path = upload
    result = execute_endpoint(
        endpoint, "https://example.test", "", "", upload_values(path), None, "200",
    )
    calls, handles = transport
    assert result.passed
    wire = calls[0]
    boundary = wire.headers["Content-Type"].split("boundary=", 1)[1]
    assert ("--" + boundary).encode() in wire.body
    assert b'name="File"; filename="renamed.pdf"' in wire.body
    assert b"Content-Type: application/pdf\r\n" in wire.body
    assert b"X-Part-Id: part-1\r\n" in wire.body
    assert b"file-content" in wire.body
    assert b'name="Label"' in wire.body
    assert all(handle.closed for handle in handles)


def test_each_file_has_independent_metadata(upload, transport):
    endpoint, path = upload
    endpoint = replace(endpoint, parameters=endpoint.parameters + (Parameter("Second", "form", "IFormFile", True),))
    values = {**upload_values(path), "form:Second": str(path),
              "file:Second:filename": "other.txt", "file:Second:header:X-Part-Id": "part-2"}
    execute_endpoint(endpoint, "https://example.test", "", "", values, None, "200")
    body = transport[0][0].body
    assert b'filename="other.txt"' in body
    assert b"Content-Type: text/plain" in body
    assert b"X-Part-Id: part-2" in body
    assert b"X-Part-Id: part-1" in body


def test_generated_python_is_executable_and_closes_files(upload, transport):
    _, path = upload
    code = generate_python(prepared(upload, upload_values(path)))
    compile(code, "<generated-upload>", "exec")
    exec(code, {})
    calls, handles = transport
    assert b'filename="renamed.pdf"' in calls[0].body
    assert b"X-Part-Id: part-1" in calls[0].body
    assert all(handle.closed for handle in handles)


def test_upload_failure_closes_files_and_masks_part_secrets(upload, monkeypatch):
    endpoint, path = upload
    handles = []

    def fail(method, url, **kwargs):
        handles.append(kwargs["files"]["File"][1])
        raise RuntimeError("Failed with private-part-value")

    monkeypatch.setattr("api_tester.client.requests.request", fail)
    monkeypatch.setattr("api_tester.client.measure_connection", lambda *args: {})
    with pytest.raises(RuntimeError) as error:
        execute_endpoint(
            endpoint, "https://example.test", "", "",
            {**upload_values(path), "file:File:header:Authorization": "private-part-value"},
            None, "200",
        )
    assert handles and all(handle.closed for handle in handles)
    assert "private-part-value" not in str(error.value)


def test_generated_curl_includes_part_options_and_masks_credentials(upload):
    _, path = upload
    values = {**upload_values(path), "file:File:header:Authorization": "private-part-value"}
    request = prepared(upload, values)
    text = generate_curl(request)
    assert "private-part-value" not in text
    args = shlex.split(text)
    file_arg = next(arg for arg in args if "filename=" in arg)
    assert 'filename="renamed.pdf"' in file_arg
    assert 'headers="Content-Type: application/pdf"' in file_arg
    assert 'headers="X-Part-Id: part-1"' in file_arg
    assert 'headers="Authorization: ******"' in file_arg
    assert "private-part-value" not in generate_python(request)


def test_part_credentials_are_redacted_from_result_and_saved_values(upload, transport):
    endpoint, path = upload
    values = {**upload_values(path), "file:File:header:Authorization": "private-part-value"}
    result = execute_endpoint(endpoint, "https://example.test", "", "", values, None, "200")
    assert b"private-part-value" in transport[0][0].body
    assert "private-part-value" not in result.request_body
    restored = SavedRequest.from_dict(SavedRequest(endpoint.id, "Upload", values=values).to_dict())
    assert restored.values["file:File:filename"] == "renamed.pdf"
    assert "file:File:header:Authorization" not in restored.values


def test_no_auth_omits_part_credentials_but_retains_custom_part_headers(upload, transport):
    endpoint, path = upload
    execute_endpoint(
        endpoint, "https://example.test", "", "",
        {**upload_values(path), "file:File:header:Cookie": "private-part-value"},
        None, "200", auth_mode="none",
    )
    assert b"Cookie:" not in transport[0][0].body
    assert b"X-Part-Id: part-1" in transport[0][0].body


def test_schema_and_fallback_file_editors_roundtrip_options(app, upload):
    endpoint, path = upload
    catalog = catalog_for(endpoint)
    editor = RequestEditor()
    values = upload_values(path)
    for current in (endpoint, replace(endpoint, form_schema=None)):
        editor.load(catalog, current, values)
        actual = editor.values()
        for key, value in values.items():
            assert actual[key] == value
        picker = editor.form_builder._editors[0][1] if current.form_schema else editor.query_parameters.cellWidget(0, 3)
        assert isinstance(picker, FilePicker)
        assert picker.options_button.text() == "File options *"
        editor.load(catalog, current, {"form:File": str(path)})
        assert "file:File:filename" not in editor.values()


def test_dialog_defaults_manual_overrides_and_validation(app, upload, monkeypatch):
    _, path = upload
    dialog = FileOptionsDialog(None, str(path), {})
    assert "image/png" in dialog.detected.text()
    dialog.filename.setText("report.pdf")
    assert "application/pdf" in dialog.detected.text()
    dialog.content_type.setText("application/octet-stream")
    dialog.part_headers.add_row("X-Part-Id", "id")
    dialog._accept()
    assert dialog.result() == QDialog.DialogCode.Accepted
    assert dialog.values()["header:X-Part-Id"] == "id"
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[-1]))
    invalid = FileOptionsDialog(None, str(path), {"header:Content-Type": "image/png"})
    invalid._accept()
    assert warnings
    assert invalid.result() != QDialog.DialogCode.Accepted


@pytest.mark.parametrize("theme_name,font_size,width", [
    ("Light", 10, 720), ("Dark", 10, 720), ("Light", 16, 900), ("Dark", 16, 900),
])
def test_options_dialog_renders_readable_controls(app, upload, theme_name, font_size, width):
    from PyQt6.QtGui import QFont
    from api_tester import theme

    _, path = upload
    theme.apply_theme(app, theme_name)
    dialog = FileOptionsDialog(None, str(path), {"header:Authorization": "private-part-value"})
    dialog.setFont(QFont("Segoe UI", font_size))
    dialog.resize(width, 700)
    dialog.show()
    app.processEvents()
    assert dialog.width() == width
    assert dialog.filename.width() > 200
    assert dialog.content_type.width() > 200
    assert dialog.part_headers.table.viewport().height() > 80
    assert dialog.part_headers.add_button.width() >= dialog.part_headers.add_button.sizeHint().width()
    assert dialog.part_headers.table.cellWidget(0, 2).echoMode().name == "Password"
    dialog.close()


def test_picker_cancel_and_apply_are_transactional(app, upload, monkeypatch):
    from api_tester import file_options_ui

    _, path = upload
    picker = FilePicker()
    picker.setText(str(path))
    picker.set_options("File", {"file:File:filename": "old.png"})
    changes = []
    picker.changed.connect(changes.append)

    def cancel(self):
        self.filename.setText("cancelled.pdf")
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(file_options_ui.FileOptionsDialog, "exec", cancel)
    picker.open_options()
    assert picker.option_values("File")["file:File:filename"] == "old.png"
    assert not changes

    def accept(self):
        self.filename.setText("new.pdf")
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(file_options_ui.FileOptionsDialog, "exec", accept)
    picker.open_options()
    assert picker.option_values("File")["file:File:filename"] == "new.pdf"
    assert changes == [str(path)]


def test_suite_roundtrip_keeps_file_options(upload, tmp_path):
    endpoint, path = upload
    suite_path = tmp_path / "suite.json"
    save_suite(Suite(cases=[Case(endpoint.id, values=upload_values(path))]), suite_path)
    assert load_suite(suite_path).cases[0].values == upload_values(path)


def test_data_runner_mapping_retains_file_options(upload):
    from api_tester.data_runner.mapping import RowMapper
    from api_tester.execution.models import RequestTemplate

    endpoint, path = upload
    mapper = RowMapper(endpoint, catalog_for(endpoint), [], template_values=upload_values(path))
    resolved = mapper.resolve(1, {})
    template = mapper.build_request_template(
        RequestTemplate(endpoint.id, endpoint.service, endpoint.method, endpoint.path), resolved,
    )
    assert template.values["file:File:header:X-Part-Id"] == "part-1"
    assert template.values["file:File:content_type"] == "application/pdf"


def test_load_studio_handoff_and_worker_resolve_file_variables(app, upload, transport):
    from api_tester.execution.models import ExecutionEnvironmentSnapshot
    from api_tester.execution.transport import WorkerTransport
    from api_tester.load_testing.ui import LoadTestingTab

    endpoint, path = upload
    environment = ExecutionEnvironmentSnapshot(
        "test", "Test", {"Demo": "https://example.test"},
        variables={"part": "resolved-part"},
    )
    tab = LoadTestingTab(catalog_for(endpoint), lambda: environment)
    values = {**upload_values(path), "file:File:header:X-Part-Id": "{{part}}"}
    assert tab.load_request(endpoint, values)
    scenario = tab._build_scenario()
    assert scenario.template.values["file:File:filename"] == "renamed.pdf"
    with WorkerTransport(environment, 0, "test") as worker:
        result, sample, error = worker.execute(endpoint, scenario.template)
    assert error is None
    assert result.passed
    assert b"X-Part-Id: resolved-part" in transport[0][0].body


@pytest.mark.parametrize("content_type", [
    "application/pdf; version=1",
    'application/pdf; filename="media.pdf"',
])
def test_generated_curl_sends_real_multipart_with_quoted_metadata(upload, content_type):
    import shutil
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    curl = shutil.which("curl")
    if curl is None:
        pytest.skip("curl is not installed")
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    endpoint, path = upload
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        values = {
            **upload_values(path), "file:File:filename": "report;final.pdf",
            "file:File:content_type": content_type,
            "file:File:header:X-Other": 'another;value="quoted"',
        }
        request = prepare_endpoint_request(
            endpoint, f"http://127.0.0.1:{server.server_port}", "", "", values, None,
        )
        args = shlex.split(generate_curl(request))
        result = subprocess.run(
            [curl, "--silent", "--show-error", "--max-time", "10", *args[1:]],
            capture_output=True, timeout=15,
        )
        assert result.returncode == 0, result.stderr.decode()
        assert b'filename="report;final.pdf"' in received[0]
        assert ("Content-Type: " + content_type).encode() in received[0], received[0].decode()
        assert b"X-Part-Id: part-1" in received[0]
        assert b'X-Other: another;value="quoted"' in received[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_explorer_saved_and_suite_handoff_preserve_options(app, upload, monkeypatch, tmp_path):
    import api_tester.main as module

    endpoint, path = upload
    monkeypatch.setattr(module, "SETTINGS_PATH", tmp_path / "settings.json")
    monkeypatch.setattr(module, "AI_SETTINGS_PATH", tmp_path / "ai.json")
    monkeypatch.setattr(module, "WORKSPACE_DB_PATH", tmp_path / "workspace.db")
    monkeypatch.setattr(module, "SUITES_DIR", tmp_path / "suites")
    monkeypatch.setattr(module, "load_catalog", lambda *args: catalog_for(endpoint))
    monkeypatch.setattr(module.MainWindow, "_check_ai_connection", lambda self: None)
    window = module.MainWindow()
    try:
        values = upload_values(path)
        window._request_drafts[endpoint.id] = (values, "")
        window._activate_endpoint(endpoint.id)
        assert window._prepared_request().file_options["File"].filename == "renamed.pdf"
        request = SavedRequest(endpoint.id, "Upload", values=window._saved_request_values())
        window.saved_request_store.upsert_request(request)
        window._open_saved_request(request.id)
        assert window.request_editor.values()["file:File:filename"] == "renamed.pdf"
        assert window._collection_cases([request.id])[0].values["file:File:content_type"] == "application/pdf"
        monkeypatch.setattr(window.suite_tab, "prompt_case_name", lambda *args: "Upload")
        window._add_to_suite()
        case = window.suite_tab.current_case
        assert case.values["file:File:header:X-Part-Id"] == "part-1"
        picker = window.suite_tab.request_editor.form_builder._editors[0][1]
        picker.set_options("File", {})
        picker.changed.emit(picker.text())
        assert "file:File:filename" not in case.values
        assert "file:File:header:X-Part-Id" not in case.values
    finally:
        window.close()


@pytest.mark.parametrize("execution", ["application", "generated_python", "worker"])
@pytest.mark.parametrize("custom", [False, True])
def test_real_http_upload_receives_exact_file_metadata(upload, execution, custom):
    import threading
    from email import policy
    from email.parser import BytesParser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from api_tester.execution.models import ExecutionEnvironmentSnapshot, RequestTemplate
    from api_tester.execution.transport import WorkerTransport

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers["Content-Type"], body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"uploaded":true}')

        def log_message(self, *args):
            pass

    endpoint, path = upload
    values = upload_values(path) if custom else {"form:File": str(path), "form:Label": "test"}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        if execution == "application":
            result = execute_endpoint(
                endpoint, base_url, "", "", values, None, "200",
                timeout=10, probe_connection=False,
            )
            assert result.passed
            assert result.status_code == 200
        elif execution == "generated_python":
            namespace = {}
            exec(generate_python(prepare_endpoint_request(
                endpoint, base_url, "", "", values, None,
            )), namespace)
            assert namespace["response"].status_code == 200
            assert all(file_tuple[1].closed for file_tuple in namespace["files"].values())
        else:
            environment = ExecutionEnvironmentSnapshot(
                "local-test", "Local test", {"Demo": base_url}, timeout=10,
            )
            template = RequestTemplate(
                endpoint.id, endpoint.service, endpoint.method, endpoint.path, values=values,
            )
            with WorkerTransport(environment, 0, "upload-test") as worker:
                result, sample, error = worker.execute(endpoint, template)
            assert error is None
            assert result is not None and result.passed
            assert sample.status_code == 200
        assert len(received) == 1
        content_type, body = received[0]
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + body
        )
        assert message.is_multipart()
        assert not message.defects
        parts = {part.get_param("name", header="content-disposition"): part
                 for part in message.iter_parts()}
        assert set(parts) == {"File", "Label"}
        file_part = parts["File"]
        assert file_part.get_filename() == ("renamed.pdf" if custom else "sample.png")
        assert file_part.get_content_type() == ("application/pdf" if custom else "image/png")
        assert file_part["X-Part-Id"] == ("part-1" if custom else None)
        assert file_part.get_payload(decode=True) == path.read_bytes()
        assert parts["Label"].get_payload(decode=True) == b"test"
        boundary = message.get_boundary()
        assert boundary and ("--" + boundary + "--").encode() in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
