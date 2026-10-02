"""The detailed Load Studio *Final report*.

A scrollable, sectioned view rendered once a run finishes or a saved run is
reopened: run header, findings & recommendations (see
:mod:`api_tester.load_testing.diagnostics`), request totals, per-metric
Min/Avg/Max/Overall statistics, the latency distribution, a per-stage
breakdown, threshold results, top errors, warnings and the scenario
configuration.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from api_tester import theme
from api_tester.load_testing.diagnostics import Finding
from api_tester.load_testing.engine import LoadRunSummary
from api_tester.load_testing.run_stats import RunStatistics, Stat, StageStats, overall_throughput

_OUTCOME_COLORS = {
    "PASS": theme.PASS,
    "FAIL": theme.FAIL,
    "STOPPED": theme.WARN,
    "ABORTED": theme.FAIL,
    "INCONCLUSIVE": theme.WARN,
}

_EMPTY = "—"


def _fmt(value: float | None, unit: str = "", digits: int = 0) -> str:
    if value is None:
        return _EMPTY
    return f"{value:,.{digits}f}{unit}"


def _pct(value: float | None) -> str:
    return _EMPTY if value is None else f"{value * 100:.1f}%"


class _ReportTable(QTableWidget):
    """A read-only table that grows to fit its rows, so only the page scrolls."""

    def __init__(self, headers: list[str], stretch_column: int) -> None:
        super().__init__(0, len(headers))
        self.setObjectName("loadTestingReportTable")
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
        self._fit_height()

    def set_rows(self, rows: list[tuple[Any, ...]]) -> None:
        self.setRowCount(len(rows))
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                if isinstance(value, QTableWidgetItem):
                    item = value
                else:
                    item = QTableWidgetItem(str(value))
                if column > 0 and not isinstance(value, QTableWidgetItem):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.setItem(row, column, item)
        self._fit_height()

    def _fit_height(self) -> None:
        rows_height = sum(self.rowHeight(row) for row in range(self.rowCount()))
        header_height = self.horizontalHeader().sizeHint().height()
        empty_row = 0 if self.rowCount() else self.verticalHeader().defaultSectionSize()
        self.setFixedHeight(header_height + rows_height + empty_row + 2 * self.frameWidth() + 2)


class LoadRunReportView(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("accordionScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setProperty("transparentPane", True)
        self._layout = QVBoxLayout(body)
        self._layout.setContentsMargins(0, 8, 8, 8)
        self._layout.setSpacing(10)

        overview = self._section("Run overview")
        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(4)
        self.outcome_label = QLabel(_EMPTY)
        form.addRow("Outcome", self.outcome_label)
        self.stop_reason_label = QLabel("")
        self.stop_reason_label.setWordWrap(True)
        form.addRow("Stop reason", self.stop_reason_label)
        self.totals_label = QLabel(_EMPTY)
        self.totals_label.setWordWrap(True)
        form.addRow("Totals", self.totals_label)
        self.timing_label = QLabel(_EMPTY)
        self.timing_label.setWordWrap(True)
        form.addRow("Timing", self.timing_label)
        self.run_id_label = QLabel(_EMPTY)
        self.run_id_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Run ID", self.run_id_label)
        self.persisted_label = QLabel(_EMPTY)
        self.persisted_label.setWordWrap(True)
        form.addRow("Persisted to", self.persisted_label)
        overview.addLayout(form)

        findings = self._section(
            "Findings & recommendations",
            "Rule-based diagnostics from this run's metrics, samples and errors. Causes are hypotheses to confirm "
            "with server-side telemetry.",
        )
        self.findings_container = QWidget()
        self.findings_container.setProperty("transparentPane", True)
        self._findings_layout = QVBoxLayout(self.findings_container)
        self._findings_layout.setContentsMargins(0, 0, 0, 0)
        self._findings_layout.setSpacing(8)
        findings.addWidget(self.findings_container)
        self.findings_empty_label = QLabel("Findings appear once a run completes.")
        self.findings_empty_label.setObjectName("loadTestingRunSubtitle")
        findings.addWidget(self.findings_empty_label)

        metrics = self._section(
            "Metric statistics",
            "Min, Avg and Max are taken over each metric snapshot interval; Overall covers the whole run.",
        )
        self.metric_stats_table = _ReportTable(["Metric", "Min", "Avg", "Max", "Overall"], 0)
        metrics.addWidget(self.metric_stats_table)
        self.latency_distribution_label = QLabel(_EMPTY)
        self.latency_distribution_label.setObjectName("loadTestingRunSubtitle")
        self.latency_distribution_label.setWordWrap(True)
        metrics.addWidget(self.latency_distribution_label)

        stages = self._section("Per-stage breakdown")
        self.stage_table = _ReportTable(
            ["Stage", "Kind", "Duration", "Users", "Requests", "Avg req/s", "Avg P95", "Avg P99", "Errors"], 0
        )
        stages.addWidget(self.stage_table)

        thresholds = self._section("Thresholds")
        self.thresholds_table = _ReportTable(["Metric", "Operator", "Target", "Observed", "Result"], 0)
        thresholds.addWidget(self.thresholds_table)
        self.thresholds_empty_label = QLabel("No thresholds were configured for this run.")
        self.thresholds_empty_label.setObjectName("loadTestingRunSubtitle")
        thresholds.addWidget(self.thresholds_empty_label)

        errors = self._section("Top errors")
        self.errors_table = _ReportTable(["Message", "Count", "Category", "Endpoint"], 0)
        errors.addWidget(self.errors_table)
        self.errors_empty_label = QLabel("No errors recorded.")
        self.errors_empty_label.setObjectName("loadTestingRunSubtitle")
        errors.addWidget(self.errors_empty_label)

        warnings = self._section("Warnings")
        self.warnings_label = QLabel("None.")
        self.warnings_label.setWordWrap(True)
        warnings.addWidget(self.warnings_label)

        configuration = self._section("Configuration")
        self.configuration_label = QLabel(_EMPTY)
        self.configuration_label.setWordWrap(True)
        self.configuration_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        configuration.addWidget(self.configuration_label)

        self._layout.addStretch()
        self.setWidget(body)
        self.clear()

    def _section(self, title: str, caption: str = "") -> QVBoxLayout:
        card = QFrame()
        card.setObjectName("loadTestingSectionCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(6)
        title_label = QLabel(title)
        title_label.setObjectName("loadTestingSectionTitle")
        layout.addWidget(title_label)
        if caption:
            caption_label = QLabel(caption)
            caption_label.setObjectName("loadTestingRunSubtitle")
            caption_label.setWordWrap(True)
            layout.addWidget(caption_label)
        self._layout.addWidget(card)
        return layout

    # ------------------------------------------------------------------ rendering

    def clear(self) -> None:
        self.outcome_label.setText(_EMPTY)
        self.outcome_label.setStyleSheet("")
        self.stop_reason_label.setText("")
        self.totals_label.setText(_EMPTY)
        self.timing_label.setText(_EMPTY)
        self.run_id_label.setText(_EMPTY)
        self.persisted_label.setText(_EMPTY)
        self.metric_stats_table.set_rows([])
        self.latency_distribution_label.setText(_EMPTY)
        self.stage_table.set_rows([])
        self.thresholds_table.set_rows([])
        self.thresholds_empty_label.setVisible(True)
        self.errors_table.set_rows([])
        self.errors_empty_label.setVisible(True)
        self.warnings_label.setText("None.")
        self.configuration_label.setText(_EMPTY)
        self.set_findings([])

    def set_findings(self, findings: list[Finding]) -> None:
        while self._findings_layout.count():
            item = self._findings_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.finding_cards: list[QFrame] = []
        for finding in findings:
            card = self._finding_card(finding)
            self.finding_cards.append(card)
            self._findings_layout.addWidget(card)
        self.findings_empty_label.setVisible(not findings)

    @staticmethod
    def _finding_card(finding: Finding) -> QFrame:
        card = QFrame()
        card.setObjectName("loadTestingFindingCard")
        card.setProperty("severity", finding.severity.lower())
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(8)
        pill = QLabel(finding.severity)
        pill.setObjectName("loadTestingSeverityPill")
        pill.setProperty("severity", finding.severity.lower())
        header.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
        title = QLabel(f"{finding.ref}  {finding.title}" if finding.ref else finding.title)
        title.setObjectName("loadTestingFindingTitle")
        title.setWordWrap(True)
        header.addWidget(title, 1)
        confidence = QLabel(f"Confidence: {finding.confidence}")
        confidence.setObjectName("loadTestingRunSubtitle")
        header.addWidget(confidence, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(header)
        columns = QHBoxLayout()
        columns.setSpacing(12)
        for heading, lines in (("Evidence", finding.evidence), ("Likely causes", finding.causes), ("Recommended fixes", finding.fixes)):
            column = QVBoxLayout()
            column.setSpacing(2)
            label = QLabel(heading.upper())
            label.setObjectName("loadTestingFindingHeading")
            column.addWidget(label)
            body = QLabel("\n".join(f"• {line}" for line in lines))
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            column.addWidget(body, 1)
            columns.addLayout(column, 1)
        layout.addLayout(columns)
        return card

    def render(
        self,
        *,
        summary: LoadRunSummary | None,
        outcome: str,
        stats: RunStatistics,
        snapshots: list[dict[str, Any]],
        stages: list[StageStats],
        errors: list[dict[str, Any]],
        warnings: list[dict[str, Any]],
        definition: dict[str, Any],
        environment_name: str,
        started_at: str,
        finished_at: str,
        duration_seconds: float | None,
        run_id: str,
        persisted_to: str,
        note: str = "",
        findings: list[Finding] | None = None,
    ) -> None:
        self.set_findings(list(findings or []))
        final = stats.final
        color = _OUTCOME_COLORS.get(outcome, theme.TEXT)
        self.outcome_label.setText(outcome)
        self.outcome_label.setStyleSheet(f"color: {color}; font-weight: 600;")
        if summary is not None:
            self.stop_reason_label.setText(summary.stop_reason or "(scenario completed without a stop condition)")
            total, passed, failed, errored = summary.total_requests, summary.passed, summary.failed, summary.errored
            peak = summary.peak_users
        else:
            self.stop_reason_label.setText(note or "(no final summary was saved for this run)")
            total = int(final.get("completed", 0) or 0)
            passed = int(final.get("passed", 0) or 0)
            failed = int(final.get("failed", 0) or 0)
            errored = int(final.get("errored", 0) or 0)
            peak = int(definition.get("peak_users", 0) or 0)
        cancelled = int(final.get("cancelled", 0) or 0)
        totals = (
            f"{total:,} requests · {passed:,} passed · {failed:,} failed · {errored:,} errored"
            + (f" · {cancelled:,} cancelled" if cancelled else "")
            + f" · peak {peak} users"
        )
        self.totals_label.setText(totals)
        timing = f"Started {started_at} · finished {finished_at}"
        if duration_seconds is not None:
            timing += f" · {duration_seconds:.0f}s"
        self.timing_label.setText(timing)
        self.run_id_label.setText(run_id or _EMPTY)
        self.persisted_label.setText(persisted_to or "Not persisted (results are in memory only)")
        if persisted_to:
            self.persisted_label.setToolTip(persisted_to)

        self.metric_stats_table.set_rows(self._metric_rows(stats, snapshots))
        if final.get("completed"):
            self.latency_distribution_label.setText(
                "Request latency distribution — "
                f"min {_fmt(final.get('min_ms'), ' ms')} · mean {_fmt(final.get('mean_ms'), ' ms')} · "
                f"p50 {_fmt(final.get('p50_ms'), ' ms')} · p95 {_fmt(final.get('p95_ms'), ' ms')} · "
                f"p99 {_fmt(final.get('p99_ms'), ' ms')} · max {_fmt(final.get('max_ms'), ' ms')}"
            )
        else:
            self.latency_distribution_label.setText("No requests completed.")

        self.stage_table.set_rows(
            [
                (
                    f"{stage.index}. {stage.label or stage.kind.replace('_', ' ').title()}",
                    stage.kind,
                    f"{stage.duration_seconds:g}s",
                    f"{stage.start_users} → {stage.end_users}",
                    f"{stage.requests:,}",
                    _fmt(stage.average_throughput, digits=1),
                    _fmt(stage.p95_ms, " ms"),
                    _fmt(stage.p99_ms, " ms"),
                    f"{stage.errors:,}",
                )
                for stage in stages
            ]
        )

        threshold_rows = []
        for result in summary.threshold_results if summary is not None else ():
            definition_item = result.definition
            verdict = QTableWidgetItem("PASS" if result.passed else "FAIL")
            verdict.setForeground(QColor(theme.PASS if result.passed else theme.FAIL))
            threshold_rows.append(
                (
                    definition_item.label or definition_item.metric,
                    definition_item.operator,
                    f"{definition_item.target:g}",
                    f"{result.observed_value:g}",
                    verdict,
                )
            )
        self.thresholds_table.set_rows(threshold_rows)
        self.thresholds_table.setVisible(bool(threshold_rows))
        self.thresholds_empty_label.setVisible(not threshold_rows)

        top_errors = errors[:10]
        self.errors_table.set_rows(
            [
                (str(entry.get("message", "")), f"{int(entry.get('count', 0)):,}", entry.get("category", ""), entry.get("endpoint_id", ""))
                for entry in top_errors
            ]
        )
        self.errors_table.setVisible(bool(top_errors))
        self.errors_empty_label.setVisible(not top_errors)
        if len(errors) > len(top_errors):
            self.errors_empty_label.setText(f"{len(errors) - len(top_errors)} more distinct errors on the Errors tab.")
            self.errors_empty_label.setVisible(True)
        else:
            self.errors_empty_label.setText("No errors recorded.")

        if warnings:
            self.warnings_label.setText(
                "\n".join(f"[{str(w.get('severity', 'warning')).upper()}] {w.get('message', '')}" for w in warnings)
            )
        else:
            self.warnings_label.setText("None.")

        self.configuration_label.setText(self._configuration_text(definition, environment_name))

    @staticmethod
    def _metric_rows(stats: RunStatistics, snapshots: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        final = stats.final

        def row(name: str, stat: Stat, overall: str, *, unit: str = "", digits: int = 0, percent: bool = False):
            if percent:
                return (name, _pct(stat.minimum), _pct(stat.average), _pct(stat.maximum), overall)
            return (
                name,
                _fmt(stat.minimum, unit, digits),
                _fmt(stat.average, unit, digits),
                _fmt(stat.maximum, unit, digits),
                overall,
            )

        has_requests = bool(final.get("completed"))
        return [
            row(
                "Throughput (req/s)",
                stats.throughput,
                _fmt(overall_throughput(snapshots, final), digits=1),
                digits=1,
            ),
            row("Request latency", stats.latency_mean, _fmt(final.get("mean_ms") if has_requests else None, " ms"), unit=" ms"),
            row("P50 latency", stats.p50, _fmt(final.get("p50_ms") if has_requests else None, " ms"), unit=" ms"),
            row("P95 latency", stats.p95, _fmt(final.get("p95_ms") if has_requests else None, " ms"), unit=" ms"),
            row("P99 latency", stats.p99, _fmt(final.get("p99_ms") if has_requests else None, " ms"), unit=" ms"),
            row(
                "Error rate",
                stats.error_rate,
                _pct(float(final.get("error_rate", 0.0)) if has_requests else None),
                percent=True,
            ),
            (
                "Virtual users",
                _fmt(stats.active_users.minimum),
                _fmt(stats.active_users.average, digits=1),
                _fmt(stats.active_users.maximum),
                _fmt(final.get("active_users")),
            ),
        ]

    @staticmethod
    def _configuration_text(definition: dict[str, Any], environment_name: str) -> str:
        if not definition:
            return _EMPTY
        lines = [
            f"Endpoint: {definition.get('method', '')} {definition.get('path', '')} "
            f"({definition.get('service', '')})".strip(),
            f"Environment: {environment_name or _EMPTY}",
            f"Workload: {definition.get('workload_model', 'closed')} · "
            f"{definition.get('peak_users', _EMPTY)} peak users · "
            f"{float(definition.get('total_duration_seconds', 0) or 0):g}s planned",
        ]
        stages = definition.get("stages") or []
        if stages:
            lines.append(
                "Stages: "
                + " → ".join(
                    f"{stage.get('label') or stage.get('kind')} ({float(stage.get('duration_seconds', 0)):g}s, "
                    f"{stage.get('start_users')}→{stage.get('end_users')} users, "
                    f"{stage.get('think_time_ms', 0)} ms think)"
                    for stage in stages
                )
            )
        limits = definition.get("limits") or {}
        if limits:
            parts = [
                f"max concurrency {limits.get('max_concurrency')}",
                f"max duration {limits.get('max_duration_seconds')}s",
                f"timeout {limits.get('request_timeout_seconds')}s",
            ]
            if limits.get("max_total_requests"):
                parts.append(f"max requests {limits.get('max_total_requests')}")
            if limits.get("error_rate_stop_threshold") is not None:
                parts.append(f"stop on error rate > {float(limits['error_rate_stop_threshold']) * 100:g}%")
            if limits.get("latency_stop_ms") is not None:
                parts.append(f"stop on P95 > {limits['latency_stop_ms']} ms")
            if limits.get("auth_failure_stop"):
                parts.append("stop on auth failure")
            lines.append("Safety limits: " + ", ".join(parts))
        if definition.get("expected_status") is not None:
            lines.append(f"Expected status: {definition.get('expected_status')}")
        return "\n".join(lines)
