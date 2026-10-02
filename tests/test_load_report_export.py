"""Diagnostics, sample aggregates and PDF export of the Load Studio report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication

from api_tester.execution.models import RequestSample
from api_tester.execution.persistence import RunStore
from api_tester.load_testing.diagnostics import diagnose, executive_summary, threshold_rows
from api_tester.load_testing.engine import LoadRunSummary, ThresholdResult
from api_tester.load_testing.pdf_export import SECTION_KEYS, PdfExportOptions, export_pdf
from api_tester.load_testing.report_data import (
    LoadReportData,
    build_report_data,
    load_sample_aggregates,
    mask_base_url,
)
from api_tester.load_testing.scenario import ThresholdDefinition

STARTED = datetime(2026, 10, 2, 12, 50, tzinfo=timezone.utc)
STAGES = [
    {"kind": "ramp_up", "label": "", "duration_seconds": 60, "start_users": 0, "end_users": 20, "think_time_ms": 0},
    {"kind": "steady", "label": "", "duration_seconds": 120, "start_users": 20, "end_users": 20, "think_time_ms": 0},
    {"kind": "ramp_up", "label": "", "duration_seconds": 60, "start_users": 20, "end_users": 60, "think_time_ms": 0},
    {"kind": "steady", "label": "", "duration_seconds": 120, "start_users": 60, "end_users": 60, "think_time_ms": 0},
]


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _users(t: float) -> float:
    if t < 60:
        return 20 * t / 60
    if t < 180:
        return 20.0
    if t < 240:
        return 20 + 40 * (t - 180) / 60
    return 60.0


def saturating_snapshots(*, error_onset: float | None = 250, healthy: bool = False) -> list[dict]:
    """A 360 s run whose throughput stops scaling at ~23 users and fails with 5xx after ``error_onset``."""
    snapshots, completed, failed = [], 0, 0
    for second in range(1, 361):
        users = max(1, round(_users(second - 0.5)))
        if healthy:
            throughput, p50, p95, p99 = users * 9.0, 80.0, 160.0, 240.0
        else:
            throughput = min(users * 9.2, 212.0)
            p50 = 90 + max(0, users - 32) * 18
            p95 = 210 + max(0, users - 30) * 62
            p99 = 380 + max(0, users - 28) * 95
        interval_completed = max(1, int(throughput))
        interval_errors = 0
        if error_onset is not None and second > error_onset:
            interval_errors = int(interval_completed * min(0.09, (second - error_onset) * 0.0009))
        completed += interval_completed
        failed += interval_errors
        snapshots.append(
            {
                "elapsed_seconds": float(second),
                "active_users": users,
                "completed": completed,
                "passed": completed - failed,
                "failed": failed,
                "errored": 0,
                "cancelled": 0,
                "p50_ms": p50,
                "p95_ms": p95,
                "p99_ms": p99,
                "min_ms": 40.0,
                "max_ms": p99 * 1.3,
                "mean_ms": (p50 + p95) / 2,
                "error_rate": failed / completed,
                "throughput_per_second": float(interval_completed),
                "interval_seconds": 1.0,
                "interval_completed": interval_completed,
                "interval_errors": interval_errors,
                "interval_throughput": float(interval_completed),
                "interval_mean_ms": (p50 + p95) / 2,
                "interval_p50_ms": p50,
                "interval_p95_ms": p95,
                "interval_p99_ms": p99,
            }
        )
    return snapshots


def _definition(**overrides) -> dict:
    definition = {
        "endpoint_id": "brands.get",
        "service": "MasterData",
        "method": "GET",
        "path": "/Brand",
        "base_url": "https://qa-apim.trialwyze.example/api",
        "scenario_name": "brands.get",
        "peak_users": 60,
        "total_duration_seconds": 360,
        "values": {"query:PageSize": "50"},
        "payload": None,
        "expected_status": "200",
        "workload_model": "closed_virtual_users",
        "stages": STAGES,
        "limits": {"max_concurrency": 100, "max_duration_seconds": 600, "request_timeout_seconds": 30},
        "thresholds": [{"metric": "p95_ms", "operator": "<=", "target": 800, "label": ""}],
    }
    definition.update(overrides)
    return definition


def _summary(final: dict, *, outcome: str = "FAIL", thresholds=None) -> LoadRunSummary:
    return LoadRunSummary(
        run_id="LT-TEST-1",
        outcome=outcome,
        started_at=STARTED.isoformat(),
        finished_at=(STARTED + timedelta(seconds=360)).isoformat(),
        total_requests=int(final["completed"]),
        passed=int(final["passed"]),
        failed=int(final["failed"]),
        errored=int(final["errored"]),
        peak_users=60,
        stop_reason=None,
        threshold_results=thresholds
        if thresholds is not None
        else [ThresholdResult(ThresholdDefinition("p95_ms", "<=", 800), float(final["p95_ms"]), False)],
    )


def _error_entries(snapshots: list[dict]) -> list[dict]:
    failed = snapshots[-1]["failed"]
    if not failed:
        return []
    first = (STARTED + timedelta(seconds=251)).isoformat()
    last = (STARTED + timedelta(seconds=360)).isoformat()
    return [
        {"signature": "s1", "category": "http_5xx", "endpoint_id": "brands.get", "message": "HTTP 503",
         "first_seen": first, "last_seen": last, "count": failed},
    ]


def store_samples(path: Path, run_id: str, snapshots: list[dict], *, queue_delay_ms: float = 3.0, per_second: int | None = None) -> None:
    """Persist samples like the engine does — one per request (or ``per_second`` per interval to keep it small)."""
    store = RunStore(path)
    store.start_run(run_id, "load_test", "brands.get", "qa", "QA", STARTED.isoformat(), _definition())
    batch = []
    for snapshot in snapshots:
        second = snapshot["elapsed_seconds"] - 1
        count = per_second or snapshot["interval_completed"]
        failures = round(count * snapshot["interval_errors"] / snapshot["interval_completed"])
        percentiles = (snapshot["interval_p50_ms"],) * 10 + (snapshot["interval_p95_ms"],) * 9 + (snapshot["interval_p99_ms"],)
        for index in range(count):
            failing = index < failures
            batch.append(
                RequestSample(
                    run_id=run_id, endpoint_id="brands.get", service="MasterData", method="GET",
                    outcome="failed" if failing else "passed", started_at=STARTED.isoformat(),
                    offset_ms=(second + index / count) * 1000, queue_delay_ms=queue_delay_ms,
                    http_ms=percentiles[index % len(percentiles)],
                    status_code=503 if failing else 200, worker_id=index,
                    error_category="http_5xx" if failing else None, error_message="HTTP 503" if failing else "",
                    response_bytes=4900,
                )
            )
    store.record_samples_batch(batch)
    store.close()


def saturating_report(tmp_path: Path | None = None, *, persisted: bool = True, **definition_overrides) -> LoadReportData:
    snapshots = saturating_snapshots()
    final = dict(snapshots[-1])
    samples, persisted_to = None, ""
    if persisted and tmp_path is not None:
        path = tmp_path / "run.db"
        store_samples(path, "LT-TEST-1", snapshots)
        samples = load_sample_aggregates(path, "LT-TEST-1")
        persisted_to = str(path)
    summary = _summary(final)
    return build_report_data(
        outcome=summary.outcome,
        summary=summary,
        definition=_definition(**definition_overrides),
        snapshots=snapshots,
        final_snapshot=final,
        errors=_error_entries(snapshots) if persisted else [],
        environment_name="QA",
        run_id="LT-TEST-1",
        started_at=summary.started_at,
        finished_at=summary.finished_at,
        duration_seconds=360,
        persisted_to=persisted_to,
        samples=samples,
    )


def _pdf_page_count(path: Path) -> int:
    from PyQt6.QtPdf import QPdfDocument

    document = QPdfDocument(None)
    document.load(str(path))
    count = document.pageCount()
    document.close()
    return count


# ---------------------------------------------------------------------- aggregates


def test_sample_aggregates_are_computed_in_sql(tmp_path):
    path = tmp_path / "agg.db"
    store = RunStore(path)
    samples = []
    for index, (latency, status, category) in enumerate(
        [(50, 200, None), (120, 200, None), (700, 200, None), (6000, None, "timeout"), (300, 503, "http_5xx"), (90, 429, "http_4xx")]
    ):
        samples.append(
            RequestSample(
                run_id="R1", endpoint_id="e", service="s", method="GET",
                outcome="passed" if category is None else ("error" if status is None else "failed"),
                started_at="", offset_ms=index * 1000.0, queue_delay_ms=float(index), http_ms=float(latency),
                status_code=status, error_category=category, error_message=f"HTTP {status}" if status else "timed out",
                response_bytes=100 * (index + 1),
            )
        )
    store.record_samples_batch(samples)
    raw = store.sample_aggregates("R1")
    assert store.sample_aggregates("missing") is None
    store.close()

    assert raw["total"] == 6
    assert raw["latency_buckets"] == [2, 1, 1, 1, 0, 0, 1]
    statuses = {item["status_code"]: item["count"] for item in raw["status_counts"]}
    assert statuses == {200: 3, None: 1, 503: 1, 429: 1}
    categories = {item["category"]: item for item in raw["categories"]}
    assert categories["timeout"]["first_seconds"] == pytest.approx(3.0)
    assert raw["queue_delay_ms"] == {"min": 0.0, "avg": 2.5, "max": 5.0}
    assert raw["response_bytes"]["total"] == 2100
    assert len(raw["error_samples"]) == 3

    aggregates = load_sample_aggregates(path, "R1")
    assert aggregates.total == 6
    assert aggregates.queue_delay_ms.average == pytest.approx(2.5)
    assert load_sample_aggregates(tmp_path / "nope.db", "R1") is None


def test_mask_base_url_strips_credentials_and_query():
    url = "https://user:secret@qa-apim.example.com:8443/api/v1?code=abc"
    assert mask_base_url(url, "full") == "https://qa-apim.example.com:8443/api/v1"
    assert mask_base_url(url, "host") == "https://qa-apim.example.com:8443"
    assert mask_base_url(url, "hidden") == "(hidden)"
    assert mask_base_url("", "full") == "—"


# ---------------------------------------------------------------------- diagnostics


def test_diagnose_saturating_run_ranks_findings(tmp_path):
    data = saturating_report(tmp_path)
    findings = diagnose(data)
    rules = [finding.rule for finding in findings]
    assert rules[0] == "saturation"
    assert findings[0].severity == "CRITICAL"
    assert 18 <= findings[0].annotations["knee_users"] <= 30  # throughput caps at 212/9.2 ≈ 23 users
    assert {"server_errors", "threshold_p95_ms", "long_tail", "load_generator_ok"} <= set(rules)
    assert "errors" not in rules  # classified by the 5xx rule instead
    assert [finding.ref for finding in findings] == [f"F{i}" for i in range(1, len(findings) + 1)]
    severities = [finding.severity for finding in findings]
    order = ["CRITICAL", "HIGH", "MEDIUM", "INFO"]
    assert severities == sorted(severities, key=order.index)
    server = next(finding for finding in findings if finding.rule == "server_errors")
    assert "HTTP 503" in server.evidence[0]
    assert any("first seen" in line for line in server.evidence)
    assert all(finding.evidence and finding.causes and finding.fixes for finding in findings)


def test_diagnose_unsaved_run_uses_snapshots_only():
    data = saturating_report(persisted=False)
    findings = {finding.rule: finding for finding in diagnose(data)}
    rules = set(findings)
    assert "saturation" in rules
    assert "load_generator_ok" not in rules  # needs samples
    assert findings["errors"].severity == "HIGH"  # counts only: errors cannot be classified
    assert "4m 1" in findings["errors"].evidence[1]


def test_diagnose_healthy_run_reports_no_problems():
    snapshots = saturating_snapshots(error_onset=None, healthy=True)
    final = dict(snapshots[-1])
    data = build_report_data(
        outcome="PASS", summary=_summary(final, outcome="PASS", thresholds=[]), definition=_definition(),
        snapshots=snapshots, final_snapshot=final,
    )
    findings = diagnose(data)
    assert [finding.rule for finding in findings] == ["healthy"]
    bullets = executive_summary(data, findings)
    assert bullets[0][0] == "pass"
    assert "GET /Brand" in bullets[0][1]
    assert len(bullets) <= 5


def test_diagnose_failing_from_start_and_auth():
    snapshots = saturating_snapshots(error_onset=None, healthy=True)
    for snapshot in snapshots:
        snapshot["interval_errors"] = snapshot["interval_completed"]
    final = dict(snapshots[-1])
    final.update(failed=final["completed"], passed=0, error_rate=1.0)
    errors = [{"category": "http_4xx", "message": "HTTP 401", "count": final["completed"],
               "first_seen": STARTED.isoformat(), "last_seen": STARTED.isoformat()}]
    data = build_report_data(
        outcome="FAIL", summary=_summary(final, thresholds=[]), definition=_definition(),
        snapshots=snapshots, final_snapshot=final, errors=errors, started_at=STARTED.isoformat(),
    )
    findings = diagnose(data)
    assert findings[0].rule == "failing_from_start"
    assert any("token" in cause for cause in findings[0].causes)
    auth = next(finding for finding in findings if finding.rule == "authentication")
    assert "expired" not in auth.causes[0]


def test_diagnose_rate_limits_timeouts_and_conflicts_from_error_entries():
    snapshots = saturating_snapshots(error_onset=None, healthy=True)
    final = dict(snapshots[-1])
    total = final["completed"]
    final.update(failed=int(total * 0.05), passed=total - int(total * 0.05))
    seen = (STARTED + timedelta(seconds=120)).isoformat()
    errors = [
        {"category": "http_4xx", "message": "HTTP 429", "count": int(total * 0.03), "first_seen": seen, "last_seen": seen},
        {"category": "timeout", "message": "Request timed out", "count": int(total * 0.01), "first_seen": seen, "last_seen": seen},
        {"category": "http_4xx", "message": "HTTP 409", "count": 25, "first_seen": seen, "last_seen": seen},
    ]
    data = build_report_data(
        outcome="FAIL", summary=_summary(final, thresholds=[]), definition=_definition(),
        snapshots=snapshots, final_snapshot=final, errors=errors, started_at=STARTED.isoformat(),
    )
    findings = {finding.rule: finding for finding in diagnose(data)}
    assert findings["rate_limited"].severity == "HIGH"
    assert "2m 00s" in findings["rate_limited"].evidence[1]
    assert "30 s" in findings["timeouts"].evidence[0]
    assert any("409" in cause for cause in findings["client_errors"].causes)


def test_diagnose_flags_load_generator_bottleneck(tmp_path):
    snapshots = saturating_snapshots(error_onset=None, healthy=True)
    path = tmp_path / "slow.db"
    store_samples(path, "LT-SLOW", snapshots, queue_delay_ms=250.0, per_second=1)
    final = dict(snapshots[-1])
    data = build_report_data(
        outcome="PASS", summary=_summary(final, outcome="PASS", thresholds=[]), definition=_definition(),
        snapshots=snapshots, final_snapshot=final, samples=load_sample_aggregates(path, "LT-SLOW"),
    )
    finding = next(finding for finding in diagnose(data) if finding.rule == "load_generator")
    assert finding.severity == "HIGH"
    assert "250 ms" in finding.evidence[0]


def test_diagnose_without_requests():
    data = build_report_data(outcome="ABORTED", summary=None, definition=_definition(), snapshots=[])
    findings = diagnose(data)
    assert [finding.rule for finding in findings] == ["no_requests"]


def test_threshold_rows_show_margin():
    data = saturating_report(persisted=False)
    name, target, observed, margin, passed = threshold_rows(data)[0]
    assert name == "P95 latency"
    assert target == "<= 800 ms"
    assert margin.startswith("+") and margin.endswith("%")
    assert passed is False


# ---------------------------------------------------------------------- PDF


def test_export_full_report(app, tmp_path):
    data = saturating_report(tmp_path)
    findings = diagnose(data)
    result = export_pdf(data, findings, tmp_path / "out" / "report.pdf", PdfExportOptions(author="QA team"))
    content = result.path.read_bytes()
    assert content.startswith(b"%PDF")
    assert result.page_count >= 5
    assert _pdf_page_count(result.path) == result.page_count
    text = "\n".join(result.texts)
    for expected in ("Key metrics", "Executive summary", "Throughput (completed requests per second)", "Metric statistics",
                     "Per-stage breakdown", "HTTP status distribution ¹", "Findings & recommendations", "Methodology & glossary",
                     "Scheduling delay ¹", "knee ≈", "Prepared by QA team", f"Page 1 of {result.page_count}"):
        assert expected in text, expected
    for finding in findings:
        assert f"{finding.ref}  {finding.title}" in text
    assert "This run was not persisted" not in text


def test_export_reduced_report_for_unsaved_run(app, tmp_path):
    data = saturating_report(persisted=False)
    result = export_pdf(data, diagnose(data), tmp_path / "reduced.pdf")
    text = "\n".join(result.texts)
    assert "This run was not persisted" in text
    assert "Per-request status codes are only available for persisted runs." in text
    assert "Bucketed distribution needs request samples" in text
    assert "Scheduling delay ¹" not in text
    assert _pdf_page_count(result.path) == result.page_count


def test_export_selected_sections_and_validation(app, tmp_path):
    data = saturating_report(persisted=False)
    findings = diagnose(data)
    result = export_pdf(data, findings, tmp_path / "findings.pdf", PdfExportOptions(sections=frozenset({"findings"})))
    text = "\n".join(result.texts)
    assert "Findings & recommendations" in text
    assert "Key metrics" not in text and "Methodology & glossary" not in text
    with pytest.raises(ValueError):
        export_pdf(data, findings, tmp_path / "none.pdf", PdfExportOptions(sections=frozenset()))


def test_export_never_includes_secrets(app, tmp_path):
    data = saturating_report(
        tmp_path,
        base_url="https://svc:TOPSECRET@qa.example.com/api?code=TOPSECRET",
        values={"header:Authorization": "Bearer TOPSECRET", "path:Id": "7"},
        payload={"note": "Bearer TOPSECRET"},
    )
    data.errors.append({"category": "authentication", "message": "Bearer TOPSECRET rejected", "count": 1})
    options = PdfExportOptions(sections=frozenset(SECTION_KEYS), include_error_samples=True)
    result = export_pdf(data, diagnose(data), tmp_path / "secret.pdf", options)
    text = "\n".join(result.texts)
    assert "TOPSECRET" not in text
    assert b"TOPSECRET" not in result.path.read_bytes()
    assert "https://qa.example.com/api" in text
    assert "path:Id" in text

    host_only = export_pdf(data, [], tmp_path / "host.pdf", PdfExportOptions(base_url_mode="host"))
    assert "https://qa.example.com" in "\n".join(host_only.texts)
    assert "https://qa.example.com/api" not in "\n".join(host_only.texts)
    hidden = export_pdf(data, [], tmp_path / "hidden.pdf", PdfExportOptions(base_url_mode="hidden"))
    assert "qa.example.com" not in "\n".join(hidden.texts)


def test_export_letter_page_size(app, tmp_path):
    from PyQt6.QtPdf import QPdfDocument

    data = saturating_report(persisted=False)
    result = export_pdf(data, diagnose(data), tmp_path / "letter.pdf", PdfExportOptions(page_size="Letter"))
    document = QPdfDocument(None)
    document.load(str(result.path))
    size = document.pagePointSize(0)
    document.close()
    assert size.width() == pytest.approx(612, abs=2)
    assert size.height() == pytest.approx(792, abs=2)


# ---------------------------------------------------------------------- options dialog


def test_export_dialog_defaults_and_options() -> None:
    from PyQt6.QtWidgets import QApplication

    from api_tester.load_testing.export_dialog import PdfExportDialog
    from api_tester.load_testing.pdf_export import SECTION_KEYS

    QApplication.instance() or QApplication([])
    dialog = PdfExportDialog(samples_available=True, run_label="brands.get — QA", author="Priya")
    assert dialog.samples_banner.objectName() == "pdfExportInfo"
    assert all(check.isChecked() for check in dialog.section_checks.values())
    assert dialog.section_checks["summary"].text() == "Cover && executive summary"
    assert dialog.error_samples_check.isEnabled()
    assert dialog.open_after is True

    dialog.error_samples_check.setChecked(True)
    dialog.page_size_combo.setCurrentText("Letter")
    dialog.base_url_combo.setCurrentIndex(1)
    dialog.notes_edit.setPlainText("Run on staging")
    options = dialog.options()
    assert options.sections == frozenset(SECTION_KEYS)
    assert options.page_size == "Letter"
    assert options.base_url_mode == "host"
    assert options.author == "Priya"
    assert options.notes == "Run on staging"
    assert options.include_error_samples is True

    dialog.section_checks["errors"].setChecked(False)
    assert not dialog.error_samples_check.isEnabled()
    assert dialog.options().include_error_samples is False
    for check in dialog.section_checks.values():
        check.setChecked(False)
    assert not dialog.export_button.isEnabled()


def test_export_dialog_without_samples_warns_and_disables_error_samples() -> None:
    from PyQt6.QtWidgets import QApplication

    from api_tester.load_testing.export_dialog import PdfExportDialog

    QApplication.instance() or QApplication([])
    dialog = PdfExportDialog(samples_available=False)
    assert dialog.samples_banner.objectName() == "pdfExportWarning"
    assert not dialog.error_samples_check.isEnabled()
    assert dialog.options().base_url_mode == "full"
    assert dialog.options().include_error_samples is False
