"""Detailed Load Studio report as a PDF.

:func:`export_pdf` draws the report with :class:`QPdfWriter` and plain
:class:`QPainter` vector drawing (no extra dependencies): a cover page with an
executive summary, performance charts, metric/stage/error tables, diagnostic
findings with likely causes and fixes, and an appendix with the configuration
and methodology. The document is rendered twice: the first pass, into memory,
counts pages so every footer can say "Page X of N".

Header values are never part of the run definition, so they cannot appear in
the PDF; base URLs always lose user info and query strings (see
:func:`~api_tester.load_testing.report_data.mask_base_url`).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PyQt6.QtCore import QBuffer, QIODevice, QMarginsF, QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QFont, QPageSize, QPainter, QPainterPath, QPdfWriter, QPen

from api_tester.load_testing.diagnostics import (
    Finding,
    executive_summary,
    fmt_ms,
    fmt_pct,
    fmt_seconds,
    threshold_rows,
)
from api_tester.load_testing.report_data import LATENCY_BUCKET_LABELS, LoadReportData, mask_base_url
from api_tester.load_testing.run_stats import downsample, overall_throughput

#: ``(key, label)`` of every optional section, in document order.
SECTIONS = (
    ("summary", "Cover & executive summary"),
    ("charts", "Performance charts"),
    ("metrics", "Metric statistics & stage breakdown"),
    ("errors", "Errors & HTTP status distribution"),
    ("findings", "Findings & recommendations"),
    ("appendix", "Appendix: configuration & methodology"),
)
SECTION_KEYS = tuple(key for key, _label in SECTIONS)
PAGE_SIZES = {"A4": QPageSize.PageSizeId.A4, "Letter": QPageSize.PageSizeId.Letter}
BASE_URL_MODES = (("full", "Full URL"), ("host", "Host only"), ("hidden", "Hidden"))
_RESOLUTION = 96
_CHART_POINTS = 300
_ERROR_SAMPLE_LIMIT = 50


@dataclass(frozen=True)
class PdfExportOptions:
    sections: frozenset[str] = frozenset(SECTION_KEYS)
    page_size: str = "A4"
    base_url_mode: str = "full"
    author: str = ""
    notes: str = ""
    include_error_samples: bool = False


@dataclass
class PdfExportResult:
    path: Path
    page_count: int
    #: Every string drawn into the document (for tests and auditing).
    texts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- palette

INK = QColor("#0f172a")
MUTED = QColor("#64748b")
LINE = QColor("#e2e8f0")
SOFT = QColor("#f8fafc")
HEAD = QColor("#f1f5f9")
WHITE = QColor("#ffffff")
ACCENT = QColor("#5b3cc4")
ACCENT_SOFT = QColor("#f5f3ff")
NAVY = QColor("#1e1b4b")
BLUE = QColor("#2563eb")
GREEN = QColor("#16a34a")
RED = QColor("#dc2626")
ORANGE = QColor("#ea580c")
AMBER = QColor("#d97706")
SEVERITY_COLORS = {"CRITICAL": RED, "HIGH": ORANGE, "MEDIUM": AMBER, "INFO": BLUE}
TONE_COLORS = {"pass": GREEN, "fail": RED, "warn": AMBER, "info": BLUE}
OUTCOME_COLORS = {"PASS": GREEN, "FAIL": RED, "ABORTED": RED, "STOPPED": AMBER, "INCONCLUSIVE": AMBER}
_AL = Qt.AlignmentFlag
_LEFT = _AL.AlignLeft | _AL.AlignVCenter
_RIGHT = _AL.AlignRight | _AL.AlignVCenter
_CENTER = _AL.AlignCenter
_TOP_LEFT = _AL.AlignLeft | _AL.AlignTop
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def _redact(text: str) -> str:
    return _BEARER.sub(r"\1 [redacted]", text)


def _font(size: float, bold: bool = False, family: str = "Segoe UI") -> QFont:
    font = QFont(family)
    font.setPointSizeF(size)
    font.setBold(bold)
    return font


def _flags(align, wrap: bool = False) -> int:
    return int(align.value) | (Qt.TextFlag.TextWordWrap.value if wrap else 0)


def _nice_ceiling(value: float) -> float:
    """Smallest 'round' ceiling >= value whose quarter steps are also round (axes use 4 intervals)."""
    if value <= 0:
        return 1.0
    exponent = 10 ** math.floor(math.log10(value))
    for multiplier in (1, 1.2, 1.6, 2, 2.4, 3, 4, 6, 8, 10):
        if value <= multiplier * exponent:
            return multiplier * exponent
    return 10 * exponent


def _axis_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{value:,.0f}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")


def _tick_step(span: float) -> float:
    for step in (1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200):
        if span / step <= 7:
            return float(step)
    return span / 6


def _bytes(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 1024:
        return f"{value:,.0f} B"
    if value < 1024 * 1024:
        return f"{value / 1024:,.1f} KB"
    return f"{value / (1024 * 1024):,.1f} MB"


def _fmt(value: float | None, digits: int = 0, unit: str = "") -> str:
    return "—" if value is None else f"{value:,.{digits}f}{unit}"


def _timestamp(value: str | None) -> str:
    from api_tester.load_testing.saved_runs import format_timestamp

    return format_timestamp(value)


def _stage_name(stage: dict[str, Any]) -> str:
    label = str(stage.get("label") or "")
    if label:
        return label
    kind = str(stage.get("kind", "")).replace("_", " ").title()
    start, end = stage.get("start_users", 0), stage.get("end_users", 0)
    return f"{kind} {start}" if start == end else f"{kind} {start}→{end}"


def _value(snapshot: dict[str, Any], key: str) -> float:
    """Interval value with a fallback to the cumulative field (older snapshots)."""
    value = snapshot.get(f"interval_{key}")
    if value is None:
        value = snapshot.get({"throughput": "throughput_per_second"}.get(key, key), 0.0)
    return float(value or 0.0)


# ---------------------------------------------------------------------- renderer


class _Renderer:
    MARGIN = 44.0

    def __init__(
        self,
        writer: QPdfWriter,
        data: LoadReportData,
        findings: list[Finding],
        options: PdfExportOptions,
        total_pages: int | None,
    ) -> None:
        self.writer = writer
        self.data = data
        self.findings = findings
        self.options = options
        self.total_pages = total_pages
        self.width = float(writer.width())
        self.height = float(writer.height())
        self.content_width = self.width - 2 * self.MARGIN
        self.bottom = self.height - 56
        self.page = 0
        self.y = 0.0
        self.section_name = ""
        self.texts: list[str] = []
        self.painter = QPainter()

    # -- primitives -----------------------------------------------------------

    def text(self, x, y, w, h, value, size=9.0, bold=False, color=INK, align=_LEFT, wrap=False, family="Segoe UI"):
        value = str(value)
        if not value:
            return
        self.texts.append(value)
        painter = self.painter
        painter.setFont(_font(size, bold, family))
        painter.setPen(color)
        if not wrap:
            value = painter.fontMetrics().elidedText(value, Qt.TextElideMode.ElideRight, max(1, int(w)))
        painter.drawText(QRectF(x, y, w, h), _flags(align, wrap), value)

    def measure(self, w, value, size=9.0, bold=False, family="Segoe UI") -> float:
        self.painter.setFont(_font(size, bold, family))
        rect = self.painter.boundingRect(QRectF(0, 0, max(1.0, w), 100000), _flags(_TOP_LEFT, True), str(value))
        return rect.height()

    def paragraph(self, x, w, value, size=8.6, color=INK, bold=False, gap=4.0) -> None:
        height = self.measure(w, value, size, bold) + 2
        self.ensure(height)
        self.text(x, self.y, w, height, value, size, bold, color, _TOP_LEFT, True)
        self.y += height + gap

    def box(self, x, y, w, h, fill=SOFT, border=LINE, radius=8.0, width=1.0):
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, w, h), radius, radius)
        self.painter.fillPath(path, fill)
        if border is not None:
            self.painter.setPen(QPen(border, width))
            self.painter.setBrush(Qt.BrushStyle.NoBrush)
            self.painter.drawPath(path)

    def pill(self, x, y, value, color, size=7.5) -> float:
        self.painter.setFont(_font(size, True))
        width = self.painter.fontMetrics().horizontalAdvance(value) + 14
        fill = QColor(color)
        fill.setAlpha(28)
        self.box(x, y, width, 17, fill, color, 8.5)
        self.text(x, y, width, 17, value, size, True, color, _CENTER)
        return width

    def dot(self, x, y, color, radius=3.4):
        self.painter.setPen(Qt.PenStyle.NoPen)
        self.painter.setBrush(color)
        self.painter.drawEllipse(QPointF(x, y), radius, radius)

    # -- pages ----------------------------------------------------------------

    def start_page(self, section: str, *, cover: bool = False) -> None:
        if self.page:
            self.footer()
            self.writer.newPage()
        self.page += 1
        self.section_name = section
        if not cover:
            self.header()
            self.y = 70.0

    def ensure(self, height: float) -> None:
        if self.y + height > self.bottom:
            self.start_page(self.section_name)

    def header(self) -> None:
        painter = self.painter
        painter.fillRect(QRectF(0, 0, self.width, 6), ACCENT)
        m = self.MARGIN
        self.box(m, 22, 22, 22, ACCENT, None, 6)
        self.text(m, 22, 22, 22, "⇄", 10, True, WHITE, _CENTER)
        self.text(m + 30, 22, 260, 22, "Rest Tester · Load Test Report", 9.5, True)
        self.text(m + 300, 22, self.width - 2 * m - 300, 22, f"{self.data.title}   ·   {self.section_name}", 8.5, False, MUTED, _RIGHT)
        painter.setPen(QPen(LINE, 1))
        painter.drawLine(QPointF(m, 54), QPointF(self.width - m, 54))

    def footer(self) -> None:
        m = self.MARGIN
        y = self.height - 40
        self.painter.setPen(QPen(LINE, 1))
        self.painter.drawLine(QPointF(m, y), QPointF(self.width - m, y))
        generated = self.data.generated_at.strftime("%Y-%m-%d %H:%M %Z").strip()
        parts = [f"Run {self.data.run_id}" if self.data.run_id else "Unsaved run", f"Generated {generated}"]
        if self.options.author:
            parts.append(f"Prepared by {self.options.author}")
        parts.append("Credentials and header values are never included")
        self.text(m, y + 4, self.content_width - 110, 20, "  ·  ".join(parts), 6.8, False, MUTED)
        total = self.total_pages if self.total_pages else "?"
        self.text(self.width - m - 100, y + 4, 100, 20, f"Page {self.page} of {total}", 7.5, True, MUTED, _RIGHT)

    def section(self, title: str, subtitle: str = "", min_body: float = 60.0) -> None:
        self.ensure(28 + (20 if subtitle else 0) + min_body)
        m = self.MARGIN
        self.painter.fillRect(QRectF(m, self.y + 3, 3, 16), ACCENT)
        self.text(m + 10, self.y, self.content_width - 10, 22, title, 12, True)
        self.y += 24
        if subtitle:
            height = self.measure(self.content_width - 10, subtitle, 7.8) + 2
            self.text(m + 10, self.y - 2, self.content_width - 10, height, subtitle, 7.8, False, MUTED, _TOP_LEFT, True)
            self.y += height + 2
        self.y += 4

    def note(self, value: str, color: QColor = MUTED) -> None:
        self.paragraph(self.MARGIN, self.content_width, value, 7.6, color)

    def banner(self, value: str, color: QColor = AMBER) -> None:
        height = self.measure(self.content_width - 30, value, 8) + 14
        self.ensure(height + 8)
        fill = QColor(color)
        fill.setAlpha(22)
        self.box(self.MARGIN, self.y, self.content_width, height, fill, color, 6)
        self.text(self.MARGIN + 10, self.y, 14, height, "!", 9, True, color, _CENTER)
        self.text(self.MARGIN + 26, self.y + 7, self.content_width - 34, height - 14, value, 8, False, INK, _TOP_LEFT, True)
        self.y += height + 10

    def table(
        self,
        widths: list[float],
        header: list[str],
        rows: list[tuple[Any, ...]],
        *,
        row_height: float = 20.0,
        size: float = 7.8,
        left: tuple[int, ...] = (0,),
        colors: dict[tuple[int, int], QColor] | None = None,
        family: str = "Segoe UI",
    ) -> None:
        scale = self.content_width / sum(widths)
        widths = [w * scale for w in widths]
        x = self.MARGIN

        def draw_header() -> None:
            self.box(x, self.y, self.content_width, row_height, HEAD, LINE, 4)
            cx = x
            for index, (width, title) in enumerate(zip(widths, header)):
                align = _LEFT if index in left else _RIGHT
                self.text(cx + 8, self.y, width - 16, row_height, title, size, True, MUTED, align)
                cx += width
            self.y += row_height

        self.ensure(row_height * 2)
        draw_header()
        for r, row in enumerate(rows):
            if self.y + row_height > self.bottom:
                self.start_page(self.section_name)
                draw_header()
            self.painter.setPen(QPen(LINE, 0.6))
            self.painter.drawLine(QPointF(x, self.y + row_height), QPointF(x + self.content_width, self.y + row_height))
            cx = x
            for index, (width, cell) in enumerate(zip(widths, row)):
                align = _LEFT if index in left else _RIGHT
                color, bold = INK, index == 0
                if colors and (r, index) in colors:
                    color, bold = colors[(r, index)], True
                self.text(cx + 8, self.y, width - 16, row_height, cell, size, bold, color, align, family=family)
                cx += width
            self.y += row_height
        self.y += 8

    def key_values(self, rows: list[tuple[str, str]]) -> None:
        self.table([200, 520], ["Setting", "Value"], rows, left=(0, 1))

    # -- charts ---------------------------------------------------------------

    def _stage_bands(self) -> list[tuple[float, float, str]]:
        bands, start = [], 0.0
        for stage in self.data.definition.get("stages") or []:
            end = start + float(stage.get("duration_seconds", 0.0) or 0.0)
            bands.append((start, end, _stage_name(stage)))
            start = end
        return bands

    def chart(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        title: str,
        series: list[tuple[list[float], list[float], QColor, bool]],
        *,
        y_unit: str = "",
        y_max: float | None = None,
        x_max: float | None = None,
        threshold: tuple[float, str] | None = None,
        bands: bool = True,
        legend: list[tuple[str, QColor]] | None = None,
        x_label: str = "",
        x_suffix: str = "s",
    ) -> tuple[float, float, float, float, float, float]:
        painter = self.painter
        self.box(x, y, w, h, WHITE, LINE, 8)
        self.text(x + 12, y + 8, w * 0.55, 18, title, 9, True)
        if legend:
            lx = x + w - 12
            for name, color in reversed(legend):
                painter.setFont(_font(7))
                tw = painter.fontMetrics().horizontalAdvance(name) + 2
                lx -= tw
                self.text(lx, y + 8, tw, 18, name, 7, False, MUTED)
                lx -= 10
                self.dot(lx + 4, y + 17, color, 3.2)
                lx -= 8
        px, py, pw, ph = x + 46, y + 34, w - 60, h - 58
        values = [value for _xs, ys, _c, _f in series for value in ys]
        if threshold is not None:
            values.append(threshold[0] * 1.1)
        top = y_max if y_max is not None else _nice_ceiling(max(values, default=0.0) * 1.08)
        right = x_max if x_max is not None else max((xs[-1] for xs, _ys, _c, _f in series if xs), default=1.0)
        right = max(right, 1e-6)
        if bands:
            for index, (start, end, label) in enumerate(self._stage_bands()):
                if start >= right:
                    break
                rx, rw = px + pw * start / right, pw * (min(end, right) - start) / right
                if index % 2 == 0:
                    painter.fillRect(QRectF(rx, py, rw, ph), ACCENT_SOFT)
                self.text(rx + 3, py + 1, rw - 4, 12, label, 6, False, QColor("#7c6bb5"))
        for i in range(5):
            gy = py + ph - ph * i / 4
            painter.setPen(QPen(LINE, 0.6))
            painter.drawLine(QPointF(px, gy), QPointF(px + pw, gy))
            value = top * i / 4
            label = f"{_axis_number(value)}{y_unit}"
            self.text(x + 2, gy - 6, 40, 12, label, 6.2, False, MUTED, _RIGHT)
        step = _tick_step(right)
        tick = 0.0
        while tick <= right + 1e-9:
            tx = px + pw * tick / right
            label = fmt_seconds(tick) if x_suffix == "s" else f"{tick:,.0f}"
            self.text(tx - 18, py + ph + 3, 36, 12, label, 6.2, False, MUTED, _CENTER)
            tick += step
        if x_label:
            self.text(px, py + ph + 13, pw, 10, x_label, 6, False, MUTED, _CENTER)
        if threshold is not None and top > 0:
            ty = py + ph - ph * min(threshold[0], top) / top
            painter.setPen(QPen(RED, 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(px, ty), QPointF(px + pw, ty))
            self.text(px + pw - 170, ty - 13, 168, 12, threshold[1], 6.4, True, RED, _RIGHT)
        for xs, ys, color, fill in series:
            if not xs:
                continue
            path = QPainterPath()
            for index, (vx, vy) in enumerate(zip(xs, ys)):
                point = QPointF(px + pw * min(vx, right) / right, py + ph - ph * min(max(vy, 0.0), top) / top)
                if index == 0:
                    path.moveTo(point)
                else:
                    path.lineTo(point)
            if fill:
                area = QPainterPath(path)
                area.lineTo(QPointF(px + pw * min(xs[-1], right) / right, py + ph))
                area.lineTo(QPointF(px + pw * min(xs[0], right) / right, py + ph))
                shade = QColor(color)
                shade.setAlpha(40)
                painter.fillPath(area, shade)
            painter.setPen(QPen(color, 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        return px, py, pw, ph, top, right

    # -- sections -------------------------------------------------------------

    def cover(self) -> None:
        data, painter, m = self.data, self.painter, self.MARGIN
        painter.fillRect(QRectF(0, 0, self.width, 210), NAVY)
        painter.fillRect(QRectF(0, 206, self.width, 4), ACCENT)
        self.box(m, 34, 30, 30, ACCENT, None, 7)
        self.text(m, 34, 30, 30, "⇄", 14, True, WHITE, _CENTER)
        self.text(m + 40, 34, 400, 30, "Rest Tester · Load Test Report", 11, True, QColor("#c7d2fe"))
        badge_w = 124.0
        title_w = self.content_width - badge_w - 16
        self.text(m, 80, title_w, 34, data.title, 21, True, WHITE)
        stages = data.definition.get("stages") or []
        workload = "Closed workload" if "closed" in str(data.definition.get("workload_model", "closed")) else "Open workload"
        line_one = "  ·  ".join(
            part
            for part in (
                data.service,
                data.endpoint_text,
                f"{workload}, {len(stages)} stage{'s' if len(stages) != 1 else ''}, {data.peak_users} peak virtual users",
                fmt_seconds(data.elapsed_seconds or data.duration_seconds),
            )
            if part
        )
        self.text(m, 116, title_w, 18, line_one, 9.5, False, QColor("#c7d2fe"))
        line_two = "  ·  ".join(
            part
            for part in (
                f"Environment {data.environment_name}" if data.environment_name else "",
                mask_base_url(data.base_url, self.options.base_url_mode) if data.base_url else "",
                f"Started {_timestamp(data.started_at)}",
                f"Run {data.run_id}" if data.run_id else "Unsaved run",
            )
            if part
        )
        self.text(m, 140, self.content_width, 18, line_two, 8, False, QColor("#a5b4fc"))
        self.text(m, 172, self.content_width, 18, f"Stop reason: {data.stop_reason}", 8.5, False, QColor("#e0e7ff"))
        color = OUTCOME_COLORS.get(data.outcome, AMBER)
        fill = QColor(color).darker(220)
        bx = self.width - m - badge_w
        self.box(bx, 34, badge_w, 48, fill, color, 10, 1.5)
        self.text(bx, 36, badge_w, 26, data.outcome, 16, True, WHITE, _CENTER)
        rows = threshold_rows(data)
        verdict = f"{sum(1 for row in rows if row[4])} of {len(rows)} thresholds passed" if rows else "No thresholds set"
        self.text(bx, 61, badge_w, 16, verdict, 7.2, False, QColor("#e2e8f0"), _CENTER)
        self.y = 232.0

    def key_metrics(self) -> None:
        stats, final = self.data.stats, self.data.final
        self.section("Key metrics", "Large value = headline statistic. Min / Avg / Max are taken over 1-second metric intervals.", 80)
        error_rate = float(final.get("error_rate", 0.0) or 0.0)
        failures = int(final.get("failed", 0) or 0) + int(final.get("errored", 0) or 0)
        p99_breached = any(
            not result.passed and result.definition.metric in ("p99_ms", "p95_ms") for result in self.data.thresholds
        )
        tiles = [
            ("THROUGHPUT · PEAK", _fmt(stats.throughput.maximum, 1, " req/s"),
             f"min {_fmt(stats.throughput.minimum, 1)} · avg {_fmt(stats.throughput.average, 1)}", INK),
            ("AVG LATENCY", fmt_ms(final.get("mean_ms") if final.get("completed") else None),
             "mean of all requests", INK),
            ("P99 · AVG", fmt_ms(stats.p99.average),
             f"min {_fmt(stats.p99.minimum)} · max {_fmt(stats.p99.maximum)} ms", RED if p99_breached else INK),
            ("ERROR RATE", fmt_pct(error_rate), f"{failures:,} failed / errored", RED if error_rate > 0 else INK),
            ("COMPLETED", f"{self.data.total_requests:,}", f"{int(final.get('passed', 0) or 0):,} passed", INK),
            ("USERS · PEAK", _fmt(stats.active_users.maximum),
             f"avg {_fmt(stats.active_users.average, 1)} · min {_fmt(stats.active_users.minimum)}", INK),
        ]
        gap = 8.0
        width = (self.content_width - 5 * gap) / 6
        for index, (title, value, sub, color) in enumerate(tiles):
            x = self.MARGIN + index * (width + gap)
            self.box(x, self.y, width, 70, WHITE, LINE, 8)
            self.text(x + 9, self.y + 7, width - 14, 14, title, 6.4, True, MUTED)
            self.text(x + 9, self.y + 22, width - 14, 26, value, 13, True, color)
            self.text(x + 9, self.y + 49, width - 14, 14, sub, 6.4, False, MUTED)
        self.y += 86

    def executive_summary(self) -> None:
        self.section("Executive summary", min_body=40)
        width = self.content_width - 30
        for tone, sentence in executive_summary(self.data, self.findings):
            height = self.measure(width, sentence, 8.6) + 4
            self.ensure(height)
            self.text(self.MARGIN + 10, self.y, 12, 16, "●", 8, True, TONE_COLORS.get(tone, BLUE))
            self.text(self.MARGIN + 24, self.y, width, height, sentence, 8.6, False, INK, _TOP_LEFT, True)
            self.y += height + 6
        self.y += 6

    def threshold_verdict(self) -> None:
        rows = threshold_rows(self.data)
        self.section("Threshold verdict", min_body=40)
        if not rows:
            self.note("No pass/fail thresholds were configured for this run.")
            self.y += 6
            return
        colors = {}
        for index, row in enumerate(rows):
            colors[(index, 4)] = GREEN if row[4] else RED
            if not row[4]:
                colors[(index, 2)] = RED
        self.table(
            [250, 120, 120, 105, 111],
            ["Threshold", "Target", "Observed", "Margin", "Result"],
            [(name, target, observed, margin, "PASS" if passed else "FAIL") for name, target, observed, margin, passed in rows],
            colors=colors,
        )
        self.y += 8

    def findings_glance(self) -> None:
        self.section("Findings at a glance", "Evidence, likely causes and fixes for each finding are in Findings & recommendations.", 40)
        for finding in self.findings:
            self.ensure(31)
            m = self.MARGIN
            self.box(m, self.y, self.content_width, 26, WHITE, LINE, 6)
            width = self.pill(m + 8, self.y + 4.5, finding.severity, SEVERITY_COLORS[finding.severity])
            self.text(m + 16 + max(width, 70), self.y, self.content_width - 160, 26, finding.title, 8.6, True)
            self.text(m + self.content_width - 70, self.y, 62, 26, finding.ref, 7.5, True, ACCENT, _RIGHT)
            self.y += 31

    def charts(self) -> None:
        data = self.data
        self.section("Performance over time", "Shaded bands mark load stages. Dashed red lines are pass/fail thresholds.", 200)
        points = downsample([s for s in data.snapshots if s.get("elapsed_seconds") is not None], _CHART_POINTS)
        if not points:
            self.note("No metric snapshots were recorded, so there is nothing to chart.")
            return
        xs = [float(s.get("elapsed_seconds", 0.0) or 0.0) for s in points]
        right = xs[-1] if xs[-1] > 0 else 1.0
        thresholds = {result.definition.metric: result.definition for result in data.thresholds}
        width = self.content_width
        m = self.MARGIN

        self.ensure(205)
        throughput_threshold = thresholds.get("throughput_per_second")
        self.chart(
            m, self.y, width, 200, "Throughput (completed requests per second)",
            [(xs, [_value(s, "throughput") for s in points], ACCENT, True)],
            x_max=right,
            threshold=(float(throughput_threshold.target), f"target {throughput_threshold.operator} {throughput_threshold.target:g} req/s")
            if throughput_threshold else None,
        )
        self.y += 210

        self.ensure(205)
        latency_threshold = next((thresholds[key] for key in ("p95_ms", "p99_ms", "p50_ms") if key in thresholds), None)
        self.chart(
            m, self.y, width, 200, "Latency percentiles (ms)",
            [
                (xs, [_value(s, "p50_ms") for s in points], ACCENT, False),
                (xs, [_value(s, "p95_ms") for s in points], AMBER, False),
                (xs, [_value(s, "p99_ms") for s in points], RED, False),
            ],
            x_max=right,
            threshold=(float(latency_threshold.target),
                       f"{latency_threshold.metric.split('_')[0].upper()} threshold {latency_threshold.target:g} ms")
            if latency_threshold else None,
            legend=[("P50", ACCENT), ("P95", AMBER), ("P99", RED)],
        )
        self.y += 210

        half = (width - 10) / 2
        self.ensure(195)
        error_threshold = thresholds.get("error_rate")
        error_values = [
            100.0 * int(s.get("interval_errors", 0) or 0) / int(s["interval_completed"]) if int(s.get("interval_completed", 0) or 0) else 0.0
            for s in points
        ]
        self.chart(
            m, self.y, half, 190, "Error rate (%)", [(xs, error_values, RED, True)], y_unit="%", x_max=right,
            y_max=_nice_ceiling(max(max(error_values, default=0.0), (float(error_threshold.target) * 100 if error_threshold else 0.0), 1.0) * 1.1),
            threshold=(float(error_threshold.target) * 100, f"threshold {float(error_threshold.target) * 100:g}%") if error_threshold else None,
        )
        self.chart(
            m + half + 10, self.y, half, 190, "Active virtual users",
            [(xs, [float(s.get("active_users", 0) or 0) for s in points], BLUE, True)], x_max=right,
        )
        self.y += 200

        self.ensure(195)
        self._scatter(m, self.y, half, 190)
        self._donut(m + half + 10, self.y, half, 190)
        self.y += 200

    def _scatter(self, x, y, w, h) -> None:
        intervals = [
            s for s in self.data.snapshots
            if int(s.get("interval_completed", 0) or 0) > 0 and int(s.get("active_users", 0) or 0) > 0
        ]
        users = [float(s.get("active_users", 0) or 0) for s in intervals]
        p95 = [_value(s, "p95_ms") for s in intervals]
        x_max = _nice_ceiling(max(users, default=1.0))
        px, py, pw, ph, top, right = self.chart(
            x, y, w, h, "P95 latency vs. virtual users", [],
            y_max=_nice_ceiling(max(p95, default=1.0) * 1.08), x_max=x_max, bands=False, x_label="virtual users", x_suffix="",
        )
        color = QColor(AMBER)
        color.setAlpha(150)
        for index in range(0, len(users), max(1, len(users) // 400)):
            self.dot(px + pw * users[index] / right, py + ph - ph * min(p95[index], top) / top, color, 1.8)
        knee = next((f.annotations.get("knee_users") for f in self.findings if f.rule == "saturation"), None)
        if knee:
            kx = px + pw * float(knee) / right
            self.painter.setPen(QPen(RED, 1, Qt.PenStyle.DashLine))
            self.painter.drawLine(QPointF(kx, py), QPointF(kx, py + ph))
            self.text(kx - 124, py + 4, 120, 12, f"knee ≈ {knee} users", 6.8, True, RED, _RIGHT)

    def _donut(self, x, y, w, h) -> None:
        final = self.data.final
        self.box(x, y, w, h, WHITE, LINE, 8)
        self.text(x + 12, y + 8, w - 24, 18, "Outcome mix", 9, True)
        segments = [
            ("Passed", int(final.get("passed", 0) or 0), GREEN),
            ("Failed (HTTP)", int(final.get("failed", 0) or 0), RED),
            ("Errored", int(final.get("errored", 0) or 0), AMBER),
            ("Cancelled", int(final.get("cancelled", 0) or 0), MUTED),
        ]
        total = sum(count for _label, count, _color in segments)
        cx, cy, radius = x + 72, y + 108, 50.0
        painter = self.painter
        if total:
            start = 90 * 16
            for _label, count, color in segments:
                if not count:
                    continue
                span = -max(1, int(round(360 * 16 * count / total)))
                painter.setPen(QPen(color, 15, Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawArc(QRectF(cx - radius, cy - radius, 2 * radius, 2 * radius), start, span)
                start += span
        else:
            painter.setPen(QPen(LINE, 15))
            painter.drawEllipse(QPointF(cx, cy), radius, radius)
        self.text(cx - 40, cy - 14, 80, 16, f"{total:,}", 11, True, INK, _CENTER)
        self.text(cx - 40, cy + 2, 80, 12, "requests", 6.8, False, MUTED, _CENTER)
        lx = cx + 76
        for index, (label, count, color) in enumerate(segments):
            ly = y + 64 + index * 20
            self.dot(lx, ly + 8, color, 3.5)
            self.text(lx + 9, ly, 80, 16, label, 7.6, True)
            share = f"{count:,} · {count / total * 100:.1f}%" if total else "0"
            self.text(lx + 9, ly, x + w - lx - 22, 16, share, 7.6, False, MUTED, _RIGHT)

    def metric_statistics(self) -> None:
        from api_tester.load_testing.report_view import LoadRunReportView

        data = self.data
        self.section("Metric statistics", "Min / Avg / Max over active 1-second intervals; Overall = whole-run value from the final snapshot.", 120)
        rows = [tuple(str(cell) for cell in row) for row in LoadRunReportView._metric_rows(data.stats, data.snapshots)]
        if data.samples_available:
            delay, size = data.samples.queue_delay_ms, data.samples.response_bytes
            rows.append(("Scheduling delay ¹", fmt_ms(delay.minimum), fmt_ms(delay.average), fmt_ms(delay.maximum), fmt_ms(delay.average)))
            rows.append(("Response size ¹", _bytes(size.minimum), _bytes(size.average), _bytes(size.maximum),
                         f"{_bytes(data.samples.response_bytes_total)} total"))
        self.table([230, 110, 110, 110, 146], ["Metric", "Min", "Avg", "Max", "Overall"], rows)
        if data.samples_available:
            self.note("¹ From request samples; only available when the run was persisted.")
        self.y += 6
        self._latency_distribution()

    def _latency_distribution(self) -> None:
        data = self.data
        buckets = data.samples.latency_buckets if data.samples_available else ()
        self.section("Latency distribution" + (" ¹" if buckets else ""), "Share of requests in each latency bucket.", 130)
        final = data.final
        if not buckets or not sum(buckets):
            self.note(
                f"Percentiles: min {fmt_ms(final.get('min_ms'))} · P50 {fmt_ms(final.get('p50_ms'))} · "
                f"P95 {fmt_ms(final.get('p95_ms'))} · P99 {fmt_ms(final.get('p99_ms'))} · max {fmt_ms(final.get('max_ms'))}. "
                "Bucketed distribution needs request samples from a persisted run."
            )
            self.y += 6
            return
        total = sum(buckets)
        peak = max(buckets)
        gap = 8.0
        width = (self.content_width - (len(buckets) - 1) * gap) / len(buckets)
        self.ensure(130)
        base = self.y + 104
        for index, (label, count) in enumerate(zip(LATENCY_BUCKET_LABELS, buckets)):
            bx = self.MARGIN + index * (width + gap)
            height = 88 * count / peak if peak else 0
            color = GREEN if index < 3 else (AMBER if index < 5 else RED)
            if height > 0:
                self.box(bx, base - height, width, height, color, None, 4)
            self.text(bx, base - height - 16, width, 14, f"{count / total * 100:.0f}%", 8, True, INK, _CENTER)
            self.text(bx, base + 4, width, 14, label, 7.4, False, MUTED, _CENTER)
        self.y = base + 28

    def stage_breakdown(self) -> None:
        data = self.data
        self.section("Per-stage breakdown", "Shows the stage where the endpoint stops scaling. SLA uses this run's latency and error-rate thresholds.", 60)
        if not data.stages:
            self.note("This run has no stage definitions.")
            return
        limits = {
            result.definition.metric: result.definition
            for result in data.thresholds
            if result.definition.metric in ("p95_ms", "p99_ms", "error_rate") and result.definition.operator in ("<=", "<")
        }
        rows, colors = [], {}
        for index, stage in enumerate(data.stages):
            error_rate = stage.errors / stage.requests if stage.requests else None
            checks = []
            for metric, observed in (("p95_ms", stage.p95_ms), ("p99_ms", stage.p99_ms), ("error_rate", error_rate)):
                definition = limits.get(metric)
                if definition is None or observed is None:
                    continue
                target = float(definition.target)
                checks.append(observed <= target if definition.operator == "<=" else observed < target)
            sla = "—" if not checks else ("PASS" if all(checks) else "FAIL")
            if checks:
                colors[(index, 8)] = GREEN if all(checks) else RED
            users = f"{stage.start_users}" if stage.start_users == stage.end_users else f"{stage.start_users}→{stage.end_users}"
            name = stage.label or stage.kind.replace("_", " ").title()
            rows.append((
                f"{stage.index} · {name}", f"{stage.duration_seconds:g}s", users, f"{stage.requests:,}",
                _fmt(stage.average_throughput, 1), fmt_ms(stage.p95_ms), fmt_ms(stage.p99_ms), fmt_pct(error_rate), sla,
            ))
        self.table(
            [130, 62, 62, 70, 72, 78, 78, 66, 64],
            ["Stage", "Duration", "Users", "Requests", "Avg req/s", "Avg P95", "Avg P99", "Errors", "SLA"],
            rows,
            colors=colors,
        )

    def errors(self) -> None:
        data = self.data
        self.section("Errors", "Grouped by category, then by normalized signature.", 60)
        failures = int(data.final.get("failed", 0) or 0) + int(data.final.get("errored", 0) or 0)
        if not data.errors:
            if failures:
                self.note(
                    f"{failures:,} requests failed. Error categories and signatures are only recorded for persisted runs; "
                    "enable “Persist this run” on the Pre-run summary to include them."
                )
            else:
                self.note("No errors were recorded.")
        else:
            by_category: dict[str, int] = {}
            for entry in data.errors:
                key = str(entry.get("category") or "other")
                by_category[key] = by_category.get(key, 0) + int(entry.get("count", 0) or 0)
            total = sum(by_category.values()) or 1
            peak = max(by_category.values()) or 1
            palette = {"http_5xx": RED, "timeout": AMBER, "http_4xx": ORANGE, "authentication": ORANGE}
            for name, count in sorted(by_category.items(), key=lambda item: -item[1]):
                self.ensure(21)
                self.text(self.MARGIN, self.y, 100, 18, name, 8, True)
                bar = (self.content_width - 200) * count / peak
                self.box(self.MARGIN + 104, self.y + 4, max(bar, 2), 11, palette.get(name, MUTED), None, 3)
                self.text(self.width - self.MARGIN - 90, self.y, 90, 18, f"{count:,} · {count / total:.0%}", 8, False, MUTED, _RIGHT)
                self.y += 21
            self.y += 6
            rows = []
            for entry in data.errors[:15]:
                first = data.error_offset_seconds(entry.get("first_seen"))
                last = data.error_offset_seconds(entry.get("last_seen"))
                rows.append((
                    str(entry.get("category", "")),
                    _redact(str(entry.get("message", ""))),
                    f"{int(entry.get('count', 0) or 0):,}",
                    f"+{fmt_seconds(first)}" if first is not None else "—",
                    f"+{fmt_seconds(last)}" if last is not None else "—",
                ))
            self.table([90, 330, 60, 80, 80], ["Category", "Signature", "Count", "First seen", "Last seen"], rows, size=7.5, left=(0, 1))
            if len(data.errors) > 15:
                self.note(f"{len(data.errors) - 15} more distinct errors are in the run file.")
        self._status_distribution()

    def _status_distribution(self) -> None:
        data = self.data
        self.section("HTTP status distribution" + (" ¹" if data.samples_available else ""), min_body=24)
        if not data.samples_available:
            self.note("Per-request status codes are only available for persisted runs.")
            return
        x = self.MARGIN
        self.ensure(24)
        for item in data.samples.status_counts:
            code = item.status_code
            if code is None or code == 0:
                label, color = "no response", MUTED
            else:
                label = str(code)
                color = GREEN if 200 <= code < 400 else (AMBER if code == 429 else (ORANGE if code < 500 else RED))
            text = f"{label}  ·  {item.count:,}"
            self.painter.setFont(_font(8, True))
            width = self.painter.fontMetrics().horizontalAdvance(text) + 14
            if x + width > self.width - self.MARGIN:
                x = self.MARGIN
                self.y += 24
                self.ensure(24)
            x += self.pill(x, self.y, text, color, 8) + 8
        self.y += 30
        if self.options.include_error_samples and data.samples.error_samples:
            self.section("Raw error samples", f"First {min(len(data.samples.error_samples), _ERROR_SAMPLE_LIMIT)} failed requests.", 60)
            rows = [
                (
                    f"+{fmt_seconds(sample.get('offset_seconds'))}",
                    str(sample.get("status_code") or "—"),
                    str(sample.get("category") or ""),
                    _redact(str(sample.get("message") or "")),
                    fmt_ms(sample.get("http_ms")),
                )
                for sample in data.samples.error_samples[:_ERROR_SAMPLE_LIMIT]
            ]
            self.table([70, 60, 100, 340, 70], ["Offset", "Status", "Category", "Message", "Latency"], rows, size=7.2, left=(0, 1, 2, 3))

    def findings_section(self) -> None:
        start, end = _timestamp(self.data.started_at), _timestamp(self.data.finished_at)
        self.section(
            "Findings & recommendations",
            "Generated by rule-based diagnostics from this run's metrics, samples and errors. Causes are ranked hypotheses: "
            f"confirm them with server-side telemetry (Application Insights, APIM analytics) for {start} – {end}.",
            120,
        )
        for finding in self.findings:
            self._finding_card(finding)

    def _finding_card(self, finding: Finding) -> None:
        m = self.MARGIN
        color = SEVERITY_COLORS[finding.severity]
        column_width = (self.content_width - 28 - 20) / 3
        columns = [("EVIDENCE", finding.evidence, color), ("LIKELY CAUSES", finding.causes, MUTED), ("RECOMMENDED FIXES", finding.fixes, GREEN)]
        heights = []
        for _label, items, _c in columns:
            heights.append(22 + sum(self.measure(column_width - 26, item, 7.3) + 5 for item in items) + 4)
        height = 34 + max(heights) + 12
        self.ensure(height + 10)
        y = self.y
        self.box(m, y, self.content_width, height, WHITE, LINE, 8)
        self.painter.fillRect(QRectF(m, y + 8, 3, height - 16), color)
        width = self.pill(m + 14, y + 10, finding.severity, color)
        self.text(m + 22 + max(width, 70), y + 9, self.content_width - 260, 20, f"{finding.ref}  {finding.title}", 9.6, True)
        self.text(m + self.content_width - 160, y + 9, 148, 20, f"Confidence: {finding.confidence}", 7.5, True, MUTED, _RIGHT)
        top = y + 34
        for index, (label, items, bullet) in enumerate(columns):
            cx = m + 14 + index * (column_width + 10)
            self.box(cx, top, column_width, height - 46, QColor("#f0fdf4") if index == 2 else SOFT, None, 6)
            self.text(cx + 8, top + 5, column_width - 16, 14, label, 6.8, True, GREEN if index == 2 else MUTED)
            iy = top + 22
            for item in items:
                item_height = self.measure(column_width - 26, item, 7.3) + 2
                self.text(cx + 8, iy, 8, 14, "•", 8, True, bullet)
                self.text(cx + 17, iy, column_width - 26, item_height, item, 7.3, False, INK, _TOP_LEFT, True)
                iy += item_height + 3
        self.y = y + height + 10

    def appendix(self) -> None:
        data, options = self.data, self.options
        definition = data.definition
        self.section("Run configuration", min_body=120)
        limits = definition.get("limits") or {}
        rows = [
            ("Scenario", str(definition.get("scenario_name") or definition.get("endpoint_id") or "—")),
            ("Run ID", data.run_id or "— (not persisted)"),
            ("Environment", data.environment_name or "—"),
            ("Base URL", mask_base_url(data.base_url, options.base_url_mode)),
            ("Service", data.service or "—"),
            ("Endpoint", data.endpoint_text),
            ("Workload model", str(definition.get("workload_model") or "closed_virtual_users")),
            ("Peak virtual users", str(data.peak_users)),
            ("Planned duration", fmt_seconds(float(definition.get("total_duration_seconds") or 0.0))),
            ("Actual duration", fmt_seconds(data.elapsed_seconds or data.duration_seconds)),
            ("Expected status", str(definition.get("expected_status") or "any 2xx")),
            ("Started / finished", f"{_timestamp(data.started_at)}  →  {_timestamp(data.finished_at)}"),
            ("Persisted to", data.persisted_to or "Not persisted (reduced report)"),
            ("Generated", data.generated_at.strftime("%Y-%m-%d %H:%M %Z").strip()),
        ]
        if options.author:
            rows.append(("Prepared by", options.author))
        self.key_values(rows)

        values = {key: value for key, value in (definition.get("values") or {}).items() if not str(key).startswith("header:")}
        if values:
            self.section("Request values", "Path and query values sent with every request. Header values are never stored or exported.", 40)
            self.table([200, 520], ["Parameter", "Value"], [(str(k), _redact(str(v))) for k, v in sorted(values.items())], left=(0, 1))
        payload = definition.get("payload")
        if payload not in (None, "", {}, []):
            self.section("Request payload", min_body=40)
            text = payload if isinstance(payload, str) else json.dumps(payload, indent=2, ensure_ascii=False)
            lines = _redact(text).splitlines()
            shown = lines[:40]
            if len(lines) > 40:
                shown.append(f"… {len(lines) - 40} more lines")
            for line in shown:
                self.ensure(12)
                self.text(self.MARGIN + 6, self.y, self.content_width - 12, 12, line, 7, False, INK, family="Consolas")
                self.y += 11
            self.y += 8

        stages = definition.get("stages") or []
        if stages:
            self.section("Load stages", min_body=40)
            self.table(
                [40, 110, 230, 90, 110, 110],
                ["#", "Kind", "Label", "Duration", "Users", "Think time"],
                [
                    (
                        str(index), str(stage.get("kind", "")), _stage_name(stage), f"{float(stage.get('duration_seconds', 0) or 0):g}s",
                        f"{stage.get('start_users', 0)}→{stage.get('end_users', 0)}", f"{int(stage.get('think_time_ms', 0) or 0):,} ms",
                    )
                    for index, stage in enumerate(stages, start=1)
                ],
                left=(0, 1, 2),
            )
        if limits:
            self.section("Safety limits", min_body=40)
            self.key_values([(str(key).replace("_", " ").capitalize(), "—" if value is None else str(value)) for key, value in limits.items()])
        thresholds = definition.get("thresholds") or []
        if thresholds:
            self.section("Configured thresholds", min_body=40)
            self.table(
                [260, 120, 160, 180], ["Metric", "Operator", "Target", "Label"],
                [(str(t.get("metric")), str(t.get("operator")), f"{float(t.get('target', 0)):g}", str(t.get("label") or "")) for t in thresholds],
                left=(0, 1, 3),
            )
        self.section("Warnings", min_body=20)
        if data.warnings:
            for warning in data.warnings:
                self.paragraph(self.MARGIN, self.content_width, f"[{str(warning.get('severity', 'warning')).upper()}] {warning.get('message', '')}", 8)
        else:
            self.note("None.")
        if options.notes.strip():
            self.section("Notes", min_body=20)
            self.paragraph(self.MARGIN, self.content_width, options.notes.strip(), 8.6)
        body = sum(self.measure(self.content_width, line, 7.8) + 5 for line in _METHODOLOGY)
        self.section("Methodology & glossary", min_body=body)
        for line in _METHODOLOGY:
            self.paragraph(self.MARGIN, self.content_width, line, 7.8, MUTED, gap=3)

    # -- document -------------------------------------------------------------

    def render(self) -> int:
        sections = self.options.sections
        if not self.painter.begin(self.writer):
            raise OSError("Could not start writing the PDF.")
        try:
            if "summary" in sections:
                self.start_page("Summary", cover=True)
                self.cover()
                if not self.data.samples_available:
                    self.banner(
                        "This run was not persisted, so this is a reduced report: latency buckets, HTTP status codes, "
                        "error signatures and scheduling delay are not included. Enable “Persist this run” for a full report."
                    )
                self.key_metrics()
                self.executive_summary()
                self.threshold_verdict()
                if "findings" in sections:
                    self.findings_glance()
            if "charts" in sections:
                self.start_page("Performance over time")
                self.charts()
            if "metrics" in sections:
                self.start_page("Metrics, stages & errors")
                self.metric_statistics()
                self.stage_breakdown()
            if "errors" in sections:
                if "metrics" not in sections:
                    self.start_page("Errors")
                else:
                    self.y += 8
                self.errors()
            if "findings" in sections:
                self.start_page("Findings & recommendations")
                self.findings_section()
            if "appendix" in sections:
                self.start_page("Appendix")
                self.appendix()
            self.footer()
        finally:
            self.painter.end()
        return self.page


_METHODOLOGY = (
    "Workload: closed model — each virtual user sends a request, waits for the response, pauses for the stage's think time, "
    "and repeats. Throughput therefore depends on latency: when latency rises, throughput falls for the same number of users.",
    "Intervals: metrics are captured once per second. Min / Avg / Max statistics are taken over intervals that had active "
    "users; Overall values come from the final cumulative snapshot.",
    "Percentiles: P50 is the median; P95 / P99 are the latency that 95% / 99% of requests completed within.",
    "Scheduling delay: time a request waited on the client before it could be sent. A high value means the load generator, "
    "not the server, limited the run.",
    "Error rate: failed (unexpected HTTP status) plus errored (no usable response) requests divided by completed requests.",
    "Findings: produced by deterministic rules over this run's data. Each lists the evidence it used; causes are hypotheses "
    "to confirm with server-side telemetry.",
    "Privacy: access tokens, x-api-key and other header values are never stored with a run and never exported.",
)


def _make_writer(target: str | QIODevice, options: PdfExportOptions, data: LoadReportData) -> QPdfWriter:
    writer = QPdfWriter(target)
    writer.setResolution(_RESOLUTION)
    writer.setPageSize(QPageSize(PAGE_SIZES.get(options.page_size, QPageSize.PageSizeId.A4)))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0))
    writer.setTitle(f"Load test report — {data.title}")
    writer.setCreator("Rest Tester")
    return writer


def export_pdf(
    data: LoadReportData,
    findings: list[Finding],
    path: str | Path,
    options: PdfExportOptions | None = None,
    *,
    progress: Callable[[str], None] | None = None,
) -> PdfExportResult:
    """Writes the report to ``path`` and returns the page count and drawn text."""
    options = options or PdfExportOptions()
    if not set(options.sections) & set(SECTION_KEYS):
        raise ValueError("Choose at least one section to export.")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if progress:
        progress("Laying out pages…")
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    layout = _Renderer(_make_writer(buffer, options, data), data, findings, options, None)
    total = layout.render()
    buffer.close()

    if progress:
        progress("Writing PDF…")
    writer = _make_writer(str(path), options, data)
    renderer = _Renderer(writer, data, findings, options, total)
    pages = renderer.render()
    del writer
    return PdfExportResult(path=path, page_count=pages, texts=renderer.texts)
