"""Dependency-free PDF rendering of the weekly cost report (with charts)."""

from __future__ import annotations

from datetime import datetime, timezone

PAGE_W, PAGE_H = 612, 792
MARGIN = 48
TEAL = (0.055, 0.549, 0.561)
MINT = (0.486, 0.82, 0.796)
INK = (0.04, 0.165, 0.18)
MUTED = (0.42, 0.53, 0.54)
WARN = (0.85, 0.47, 0.02)
GRID = (0.89, 0.93, 0.93)


def _clean(value) -> str:
    text = "" if value is None else str(value)
    text = text.encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _num(value, default=0.0) -> float:
    return float(value) if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else default

# Helvetica / Helvetica-Bold advance widths (1/1000 em) for ASCII 32..126, so
# wrapping and right-alignment use real text widths instead of a guess.
_W1 = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_W2 = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]


def text_width(value, size=10, bold=False) -> float:
    table = _W2 if bold else _W1
    total = 0
    for char in str(value):
        code = ord(char)
        total += table[code - 32] if 32 <= code <= 126 else 556
    return total * size / 1000.0


def wrap_text(value, size=9.5, width=PAGE_W - 2 * MARGIN, bold=False) -> list:
    lines, current = [], ""
    for word in str(value).split():
        trial = f"{current} {word}".strip()
        if current and text_width(trial, size, bold) > width:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


class _Doc:
    def __init__(self):
        self.pages: list[list[str]] = []
        self.y = 0.0
        self.new_page()

    def new_page(self):
        self.pages.append([])
        self.y = PAGE_H - MARGIN

    @property
    def ops(self) -> list[str]:
        return self.pages[-1]

    def need(self, height: float):
        if self.y - height < MARGIN + 18:
            self.new_page()

    def color(self, rgb, stroke=False):
        self.ops.append("%.3f %.3f %.3f %s" % (*rgb, "RG" if stroke else "rg"))

    def text(self, x, y, value, size=10, bold=False, rgb=INK):
        self.color(rgb)
        font = "F2" if bold else "F1"
        self.ops.append(
            f"BT /{font} {size} Tf {x:.1f} {y:.1f} Td ({_clean(value)}) Tj ET"
        )

    def rect(self, x, y, w, h, rgb, fill=True):
        self.color(rgb, stroke=not fill)
        self.ops.append(
            f"{x:.1f} {y:.1f} {w:.1f} {h:.1f} re {'f' if fill else 'S'}"
        )

    def line(self, x1, y1, x2, y2, rgb=GRID, width=0.6):
        self.color(rgb, stroke=True)
        self.ops.append(f"{width} w {x1:.1f} {y1:.1f} m {x2:.1f} {y2:.1f} l S")

    def paragraph(self, value, size=9.5, rgb=INK, width=PAGE_W - 2 * MARGIN,
                  x=MARGIN, bold=False):
        for item in wrap_text(value, size, width, bold):
            self.need(size + 5)
            self.y -= size + 3
            self.text(x, self.y, item, size, bold=bold, rgb=rgb)

    def text_right(self, x_right, y, value, size=10, bold=False, rgb=INK):
        self.text(x_right - text_width(value, size, bold), y, value, size, bold, rgb)

    def gap(self, height=6):
        self.y -= height

    def heading(self, value, size=13):
        self.need(size + 18)
        self.y -= size + 10
        self.text(MARGIN, self.y, value, size, bold=True, rgb=TEAL)
        self.y -= 8

    def bar_chart(self, title, items, unit="", color=TEAL, height=120):
        """Horizontal-label vertical bar chart. items: [(label, value)]."""
        self.heading(title, 11)
        self.need(height + 40)
        left, bottom = MARGIN + 34, self.y - height - 18
        width = PAGE_W - 2 * MARGIN - 44
        top_value = max([_num(v) for _, v in items] + [1e-9])
        for step in range(5):
            gy = bottom + height * step / 4
            self.line(left, gy, left + width, gy)
            self.text(MARGIN - 4, gy - 3, "%.4g" % (top_value * step / 4), 7, rgb=MUTED)
        slot = width / max(len(items), 1)
        for index, (label, value) in enumerate(items):
            bar_h = height * _num(value) / top_value
            bx = left + index * slot + slot * 0.2
            self.rect(bx, bottom, slot * 0.6, bar_h, color)
            self.text(bx, bottom + bar_h + 3, "%.4g%s" % (_num(value), unit), 7, rgb=INK)
            self.text(bx - 2, bottom - 11, str(label)[:16], 7, rgb=MUTED)
        self.y = bottom - 22


def build_cost_report_pdf(report: dict, *, requested_by: str = "operator") -> bytes:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    period = report.get("reporting_period") if isinstance(
        report.get("reporting_period"), dict) else {}
    rows = [r for r in (report.get("recommendations") or []) if isinstance(r, dict)]
    pricing = report.get("pricing") if isinstance(report.get("pricing"), dict) else {}
    configured_request_pricing = (
        pricing.get("model") == "kubernetes_requests"
        and pricing.get("cost_model_status") == "configured"
        and pricing.get("is_example") is False
    )

    doc = _Doc()
    doc.rect(0, PAGE_H - 74, PAGE_W, 74, TEAL)
    doc.text(MARGIN, PAGE_H - 38, "Kybernaut Weekly Reliability & Cost Report", 17, True, (1, 1, 1))
    doc.text(MARGIN, PAGE_H - 58,
             "Period %s to %s (end exclusive)" % (
                 str(period.get("start", "n/a"))[:10],
                 str(period.get("end_exclusive", "n/a"))[:10]), 9.5, False, (1, 1, 1))
    doc.y = PAGE_H - 74 - 14
    doc.paragraph("Generated %s for %s. Report generated at %s." % (
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), requested_by,
        report.get("generated_at", "unknown")), 8.5, MUTED)
    doc.paragraph("Data provenance: %s" % report.get("provenance", "not stated"), 8.5, MUTED)
    doc.paragraph("Pricing source: %s" % pricing.get("source", "not stated"), 8.5, MUTED)
    if pricing.get("model") == "kubernetes_requests":
        doc.paragraph(
            "Kubernetes request rates: %s/core-hour CPU; %s/GiB-hour memory; "
            "effective %s. These are allocation estimates, not invoice costs." % (
                pricing.get("cpu_price_per_core_hour", "not provided"),
                pricing.get("memory_price_per_gib_hour", "not provided"),
                pricing.get("effective_date", "not provided"),
            ),
            8.5,
            MUTED,
        )

    doc.heading("Summary")
    for label, key in (
        ("Resources analyzed", "resources_analyzed"),
        ("Downsize candidates", "underutilized_candidates"),
        ("Capacity-pressure resources", "capacity_pressure_resources"),
        ("Incomplete / insufficient data", "incomplete_or_insufficient"),
        ("Potential monthly savings (estimate)", "potential_monthly_savings"),
        ("Estimated annual savings (estimate)", "estimated_annual_savings"),
        ("Potential request allocation savings (reporting period)",
         "potential_request_cost_savings"),
        ("Realized savings", "realized_savings"),
    ):
        doc.need(16)
        doc.y -= 14
        doc.text(MARGIN, doc.y, label, 10)
        value = summary.get(key)
        if value is None:
            display = "not estimated"
        elif key == "potential_request_cost_savings":
            display = "%s %.4f" % (
                summary.get("request_cost_savings_currency") or "",
                value,
            )
        else:
            display = value
        doc.text(PAGE_W - MARGIN - 150, doc.y, str(display), 10, True)
        doc.line(MARGIN, doc.y - 4, PAGE_W - MARGIN, doc.y - 4)

    savings = [
        (r.get("resource", "?"), _num(r.get("potential_monthly_savings")))
        for r in rows
        if r.get("potential_monthly_savings") is not None
    ]
    if savings:
        doc.bar_chart("Potential monthly savings per resource (estimate)", savings, color=TEAL)
    cpu = [(r.get("resource", "?"), _num(r.get("cpu_avg")) * 100) for r in rows]
    mem = [(r.get("resource", "?"), _num(r.get("memory_avg")) * 100) for r in rows]
    if cpu:
        doc.bar_chart("Average CPU utilization this week (%)", cpu, "%", MINT)
        doc.bar_chart("Average memory utilization this week (%)", mem, "%", WARN)

    doc.heading("Resource details")
    for r in rows:
        doc.need(70)
        doc.y -= 14
        doc.text(MARGIN, doc.y, "%s  [%s]" % (r.get("resource", "?"), r.get("status", "?")), 10.5, True)
        doc.paragraph("Type %s | samples %s/%s | CPU avg %.1f%% p95 %.1f%% | memory avg %.1f%% p95 %.1f%%" % (
            r.get("current_type", "n/a"), r.get("sample_count", "?"), r.get("expected_samples", "?"),
            _num(r.get("cpu_avg")) * 100, _num(r.get("cpu_p95")) * 100,
            _num(r.get("memory_avg")) * 100, _num(r.get("memory_p95")) * 100), 8.5, MUTED)
        if r.get("current_monthly_cost") is not None:
            doc.paragraph("Current monthly cost %s -> recommended %s | potential saving %s" % (
                r.get("current_monthly_cost"), r.get("recommended_monthly_cost"),
                r.get("potential_monthly_savings")), 8.5)
        if r.get("current_estimated_request_cost"):
            current_cost = r["current_estimated_request_cost"]
            proposed_cost = r.get("proposed_estimated_request_cost")
            doc.paragraph(
                "Requested-resource estimate (%s; not invoice cost): current %s "
                "| proposed %s" % (
                    current_cost["currency"],
                    current_cost["total"],
                    (
                        proposed_cost["total"]
                        if proposed_cost else "not estimated"
                    ),
                ),
                8.5,
            )
        if r.get("cpu_request_recommended_cores") is not None:
            request_savings = r.get("request_cost_savings")
            if configured_request_pricing and isinstance(request_savings, dict):
                savings_text = (
                    "Potential estimated request-allocation saving: %s %.4f "
                    "(not invoice cost; not realized savings)." % (
                        request_savings.get("currency", pricing.get("currency", "")),
                        request_savings.get("potential_savings", 0.0),
                    )
                )
            elif configured_request_pricing:
                savings_text = (
                    "Request-allocation savings are not estimated for this proposal."
                )
            else:
                savings_text = (
                    "Monetary savings are not estimated because compatible "
                    "non-example pricing is unavailable."
                )
            doc.paragraph(
                "Proposed aggregate requests: CPU %.3f -> %.3f cores; "
                "memory %.0f -> %.0f bytes. Per-container allocation is not "
                "estimated. %s" % (
                    r["cpu_request_current_cores"],
                    r["cpu_request_recommended_cores"],
                    r["memory_request_current_bytes"],
                    r["memory_request_recommended_bytes"],
                    savings_text,
                ),
                8.5,
            )
        doc.paragraph("Rationale: %s" % r.get("rationale", "none"), 8.5)
        if r.get("suggested_next_step"):
            doc.paragraph("Next step: %s" % r.get("suggested_next_step"), 8.5)

    doc.heading("Important")
    doc.paragraph(report.get("disclaimer", "Recommendations are read-only estimates."), 9)
    doc.paragraph("A human decision is required before any capacity change. Realized savings are "
                  "only counted after an approved change is applied and measured.", 9)

    return _serialize(doc)


def _f(value):
    return float(value) if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else None


def _usd(value, places=2):
    return "unavailable" if value is None else "$%s" % format(value, ",.%df" % places)


def _pct(part, whole):
    return "n/a" if not whole else "%.1f%%" % (100.0 * part / whole)


def _table(doc, columns, rows, *, total_row=None, size=8.5):
    """columns: [(header, width, 'l'|'r')]. Draws a header band and zebra rows."""
    row_h = 17
    doc.need(row_h * 3 + 6)
    x0 = MARGIN
    total_w = sum(width for _, width, _ in columns)

    def header():
        doc.need(row_h * 2)
        top = doc.y
        doc.rect(x0, top - row_h, total_w, row_h, (0.88, 0.95, 0.95))
        x = x0
        for title, width, align in columns:
            if align == "r":
                doc.text_right(x + width - 5, top - row_h + 5, title, size - 0.5, True, TEAL)
            else:
                doc.text(x + 5, top - row_h + 5, title, size - 0.5, True, TEAL)
            x += width
        doc.y = top - row_h

    header()
    for index, row in enumerate(rows + ([total_row] if total_row else [])):
        is_total = total_row is not None and index == len(rows)
        if doc.y - row_h < MARGIN + 18:
            doc.new_page()
            header()
        top = doc.y
        if is_total:
            doc.rect(x0, top - row_h, total_w, row_h, (0.93, 0.97, 0.97))
            doc.line(x0, top, x0 + total_w, top, TEAL, 0.9)
        elif index % 2 == 1:
            doc.rect(x0, top - row_h, total_w, row_h, (0.975, 0.985, 0.985))
        x = x0
        for (title, width, align), cell in zip(columns, row):
            if align == "r":
                doc.text_right(x + width - 5, top - row_h + 5, cell, size, is_total)
            else:
                doc.text(x + 5, top - row_h + 5, cell, size, is_total)
            x += width
        doc.y = top - row_h
    doc.line(x0, doc.y, x0 + total_w, doc.y, GRID, 0.8)
    doc.gap(8)


def _kpi_cards(doc, cards):
    gap, height = 10, 52
    width = (PAGE_W - 2 * MARGIN - gap * (len(cards) - 1)) / len(cards)
    doc.need(height + 10)
    top = doc.y
    for index, (label, value, note) in enumerate(cards):
        x = MARGIN + index * (width + gap)
        doc.rect(x, top - height, width, height, (0.95, 0.985, 0.985))
        doc.rect(x, top - height, 3, height, TEAL)
        doc.text(x + 11, top - 14, label.upper(), 7, True, MUTED)
        size = 15 if text_width(value, 15, True) < width - 18 else 11
        doc.text(x + 11, top - 33, value, size, True, INK)
        doc.text(x + 11, top - 46, note, 7, False, MUTED)
    doc.y = top - height - 6


def _split_chart(doc, title, items, rate_note):
    """Horizontal stacked bars: CPU (teal) + memory (amber) monthly cost per service."""
    _section(doc, title, keep=110)
    bar_h, slot = 13, 24
    label_w, value_w = 110, 78
    plot_w = PAGE_W - 2 * MARGIN - label_w - value_w
    doc.need(slot * len(items) + 36)
    peak = max([c + m for _, c, m in items] + [1e-9])
    top = doc.y - 6
    for index, (name, cpu, memory) in enumerate(items):
        y = top - (index + 1) * slot + 4
        doc.text(MARGIN, y + 3, name, 8.5, False, INK)
        x = MARGIN + label_w
        cpu_w, mem_w = plot_w * cpu / peak, plot_w * memory / peak
        doc.rect(x, y, plot_w, bar_h, (0.94, 0.97, 0.97))
        if cpu_w > 0:
            doc.rect(x, y, cpu_w, bar_h, TEAL)
        if mem_w > 0:
            doc.rect(x + cpu_w, y, mem_w, bar_h, WARN)
        doc.text_right(PAGE_W - MARGIN, y + 3, "$%s/mo" % format(cpu + memory, ",.2f"), 8.5, True)
    ly = top - len(items) * slot - 14
    doc.rect(MARGIN + label_w, ly, 8, 8, TEAL)
    doc.text(MARGIN + label_w + 12, ly, "CPU", 7.5, False, MUTED)
    doc.rect(MARGIN + label_w + 48, ly, 8, 8, WARN)
    doc.text(MARGIN + label_w + 60, ly, "Memory", 7.5, False, MUTED)
    doc.text(MARGIN + label_w + 112, ly, rate_note, 7.5, False, MUTED)
    doc.y = ly - 10


def _stamp(value) -> str:
    """'2026-11-04T06:57:38.874+00:00' -> '2026-11-04 06:57 UTC' (fallback: as given)."""
    if not isinstance(value, str) or not value:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _section(doc, title, *, keep=70):
    """Section heading with a rule; keeps the heading with at least `keep` pt of content."""
    doc.need(keep + 30)
    doc.gap(10)
    doc.y -= 13
    doc.text(MARGIN, doc.y, title, 13, bold=True, rgb=TEAL)
    doc.y -= 6
    doc.line(MARGIN, doc.y, PAGE_W - MARGIN, doc.y, TEAL, 0.9)
    doc.y -= 4


def _bullets(doc, items, *, size=9, rgb=INK, indent=12):
    width = PAGE_W - 2 * MARGIN - indent
    for item in items:
        lines = wrap_text(item, size, width)
        doc.need(len(lines) * (size + 3.5) + 6)
        doc.gap(3)
        for number, line in enumerate(lines):
            doc.y -= size + 3.5
            if number == 0:
                doc.rect(MARGIN + 2, doc.y + size / 2 - 1.5, 3, 3, TEAL)
            doc.text(MARGIN + indent, doc.y, line, size, False, rgb)


def _note_box(doc, text, rgb=WARN):
    lines = wrap_text(text, 8.5, PAGE_W - 2 * MARGIN - 20)
    height = len(lines) * 11.5 + 12
    doc.need(height + 8)
    top = doc.y - 4
    doc.rect(MARGIN, top - height, PAGE_W - 2 * MARGIN, height, (0.995, 0.965, 0.92))
    doc.rect(MARGIN, top - height, 3, height, rgb)
    for number, line in enumerate(lines):
        doc.text(MARGIN + 12, top - 14 - number * 11.5, line, 8.5, False, INK)
    doc.y = top - height - 6


_CHECKS = {
    "memory": (
        "Compare the container memory request/limit with the 7-14 day peak working set "
        "(container_memory_working_set_bytes). Memory is rarely reclaimed after a spike, "
        "so size for the peak plus headroom, not the current sample."),
    "CPU": (
        "Compare the CPU request with p95 usage over a representative period "
        "(rate of container_cpu_usage_seconds_total). Check throttling before lowering it."),
}


def _priority_card(doc, rank, name, resource, measured, cost, share, total_month):
    width = PAGE_W - 2 * MARGIN
    text_x = MARGIN + 38
    body_w = width - 38 - 10
    save = "If usage fell 10%% / 20%% / 30%%, the run rate would drop by about %s / %s / %s per month." % (
        _usd(cost * .1), _usd(cost * .2), _usd(cost * .3))
    head_lines = wrap_text("Measured %s. %s" % (measured, save), 8.5, body_w)
    check_lines = wrap_text("Check first: " + _CHECKS[resource], 8.5, body_w)
    height = 30 + (len(head_lines) + len(check_lines)) * 11.5 + 12
    doc.need(height + 8)
    top = doc.y - 6
    doc.rect(MARGIN, top - height, width, height, (0.965, 0.988, 0.988))
    doc.rect(MARGIN, top - height, 3, height, TEAL)
    doc.rect(MARGIN + 11, top - 26, 20, 20, TEAL)
    doc.text(MARGIN + 11 + (20 - text_width(str(rank), 11, True)) / 2, top - 20, str(rank), 11, True, (1, 1, 1))
    doc.text(text_x, top - 17, "%s - %s" % (name, resource), 10.5, True)
    doc.text_right(PAGE_W - MARGIN - 10, top - 17, "%s/mo  |  %s of run rate" % (_usd(cost), share), 9.5, True, TEAL)
    y = top - 32
    for line in head_lines:
        y -= 11.5
        doc.text(text_x, y, line, 8.5, False, INK)
    y -= 4
    for line in check_lines:
        y -= 11.5
        doc.text(text_x, y, line, 8.5, False, MUTED)
    doc.y = top - height - 2


def build_live_cost_report_pdf(
    report: dict, *, requested_by: str = "operator"
) -> bytes:
    pricing = report.get("pricing") if isinstance(report.get("pricing"), dict) else {}
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    services = report.get("services") if isinstance(report.get("services"), dict) else {}
    configured = pricing.get("configured") is True
    cpu_rate = _f(pricing.get("cpu_per_core_hour_usd"))
    mem_rate = _f(pricing.get("memory_per_gib_hour_usd"))

    doc = _Doc()
    doc.rect(0, PAGE_H - 78, PAGE_W, 78, TEAL)
    doc.text(MARGIN, PAGE_H - 38, "Kybernaut Live Usage Cost Report", 18, True, (1, 1, 1))
    doc.text(MARGIN, PAGE_H - 58, "Current Prometheus measurements  |  illustrative USD estimates", 9.5, False, (1, 1, 1))
    doc.text_right(PAGE_W - MARGIN, PAGE_H - 38, "Usage observed", 7.5, False, (0.82, 0.95, 0.94))
    doc.text_right(PAGE_W - MARGIN, PAGE_H - 52, _stamp(report.get("observed_at")), 10, True, (1, 1, 1))
    doc.y = PAGE_H - 78 - 10

    # ---- meta block: two aligned columns ----
    col_w = (PAGE_W - 2 * MARGIN - 20) / 2
    meta_left = [
        ("Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        ("Requested by", requested_by),
        ("Scope", "order, payment, user and inventory services"),
    ]
    meta_right = [
        ("CPU rate", "not configured" if cpu_rate is None else "$%.2f per core-hour" % cpu_rate),
        ("Memory rate", "not configured" if mem_rate is None else "$%.2f per GiB-hour" % mem_rate),
        ("Basis", "730-hour month; point-in-time sample"),
    ]
    top = doc.y - 6
    for index, (label, value) in enumerate(meta_left):
        doc.text(MARGIN, top - 11 - index * 13, label, 8, True, MUTED)
        doc.text(MARGIN + 62, top - 11 - index * 13, value, 8.5, False, INK)
    for index, (label, value) in enumerate(meta_right):
        doc.text(MARGIN + col_w + 20, top - 11 - index * 13, label, 8, True, MUTED)
        doc.text(MARGIN + col_w + 20 + 62, top - 11 - index * 13, value, 8.5, False, INK)
    doc.y = top - 3 * 13 - 6
    for item in wrap_text(
            "Illustrative owner-configured internal rates; not actual cloud-provider costs or an invoice.",
            8, PAGE_W - 2 * MARGIN):
        doc.y -= 11
        doc.text(MARGIN, doc.y, item, 8, False, MUTED)
    doc.gap(4)

    # ---- collect per-service numbers once ----
    usable = []
    for name, usage in services.items():
        if not isinstance(usage, dict):
            continue
        usable.append({
            "name": str(name), "usage": usage,
            "cores": _f(usage.get("cpu_cores_used")), "bytes": _f(usage.get("memory_bytes")),
            "cpu_m": _f(usage.get("estimated_cpu_monthly_cost_usd")),
            "mem_m": _f(usage.get("estimated_memory_monthly_cost_usd")),
            "hour": _f(usage.get("estimated_hourly_cost_usd")),
            "month": _f(usage.get("estimated_monthly_run_rate_usd")),
            "rps": _f(usage.get("request_rate")),
            "p95": _f(usage.get("p95_latency_seconds")),
        })
    missing = [str(n) for n, usage in services.items() if not isinstance(usage, dict)]
    priced = [u for u in usable if None not in (u["cpu_m"], u["mem_m"], u["month"])]
    total_month = sum(u["month"] for u in priced)
    total_cpu = sum(u["cpu_m"] for u in priced)
    total_mem = sum(u["mem_m"] for u in priced)

    if missing:
        _note_box(doc, "Prometheus usage was unavailable for: %s. The four-service total is "
                       "withheld and the figures below cover only the services that reported." % ", ".join(missing))

    # ---- headline numbers ----
    _section(doc, "Four-service estimate", keep=60)
    hourly, monthly = summary.get("estimated_hourly_cost_usd"), summary.get("estimated_monthly_run_rate_usd")
    biggest = max(priced, key=lambda u: u["month"]) if priced else None
    _kpi_cards(doc, [
        ("Hourly run rate", "n/a" if hourly is None else "$%.2f" % hourly, "all four services"),
        ("Monthly run rate", "n/a" if monthly is None else "$%.2f" % monthly, "730-hour month"),
        ("Highest-cost service", biggest["name"] if biggest else "n/a",
         "%s/month" % _usd(biggest["month"]) if biggest else "no priced services"),
        ("Memory share of cost", _pct(total_mem, total_month) if priced else "n/a",
         "CPU %s" % _pct(total_cpu, total_month) if priced else "pricing unavailable"),
    ])

    # ---- table: cost by service ----
    _section(doc, "Cost by service", keep=100)
    columns = [("Service", 112, "l"), ("CPU cores", 56, "r"), ("CPU $/mo", 60, "r"),
               ("Memory MiB", 64, "r"), ("Mem $/mo", 60, "r"), ("$/hour", 56, "r"),
               ("$/month", 64, "r"), ("Share", 44, "r")]
    rows = []
    for u in usable:
        rows.append([
            u["name"],
            "n/a" if u["cores"] is None else "%.3f" % u["cores"],
            _usd(u["cpu_m"]),
            "n/a" if u["bytes"] is None else "%.1f" % (u["bytes"] / 1024 ** 2),
            _usd(u["mem_m"]),
            "n/a" if u["hour"] is None else "$%.4f" % u["hour"],
            _usd(u["month"]),
            _pct(u["month"], total_month) if u["month"] is not None and total_month else "n/a",
        ])
    for name in missing:
        rows.append([name] + ["n/a"] * 7)
    total_row = ["Total", "", _usd(total_cpu), "", _usd(total_mem),
                 "n/a" if hourly is None else "$%.4f" % hourly, _usd(total_month), "100%"] if priced else None
    _table(doc, columns, rows, total_row=total_row)
    if priced:
        _split_chart(
            doc, "Monthly cost split: CPU vs memory",
            [(u["name"], u["cpu_m"], u["mem_m"]) for u in sorted(priced, key=lambda u: -u["month"])],
            "$%.2f/core-h CPU, $%.2f/GiB-h memory" % (cpu_rate or 0, mem_rate or 0))

    # ---- table: live usage signals ----
    _section(doc, "Live usage signals", keep=90)
    _table(doc, [("Service", 160, "l"), ("CPU cores", 90, "r"), ("Memory (MiB)", 96, "r"),
                 ("Requests/sec", 90, "r"), ("p95 latency", 80, "r")],
           [[u["name"],
             "n/a" if u["cores"] is None else "%.3f" % u["cores"],
             "n/a" if u["bytes"] is None else "%.1f" % (u["bytes"] / 1024 ** 2),
             "n/a" if u["rps"] is None else "%.2f" % u["rps"],
             "n/a" if u["p95"] is None else "%.1f ms" % (u["p95"] * 1000)]
            for u in usable] + [[name, "n/a", "n/a", "n/a", "n/a"] for name in missing])

    # ---- where we can cut ----
    if priced:
        doc.new_page()
        _section(doc, "Where we can cut", keep=60)
        for line in wrap_text(
                "Ranked from the measured usage above: the biggest line items are where a review pays off "
                "first. Savings are straight-line what-if scenarios (a % reduction in measured usage), not "
                "recommendations or guaranteed savings.", 8.5, PAGE_W - 2 * MARGIN):
            doc.y -= 11.5
            doc.text(MARGIN, doc.y, line, 8.5, False, MUTED)
        doc.gap(10)
        _kpi_cards(doc, [
            ("If usage -10%", "-" + _usd(total_month * .1), "per month"),
            ("If usage -20%", "-" + _usd(total_month * .2), "per month"),
            ("If usage -30%", "-" + _usd(total_month * .3), "per month"),
            ("-10% over a year", "-" + _usd(total_month * .1 * 12), "12 x monthly"),
        ])

        items = []
        for u in priced:
            items.append((u["name"], "memory", "%.1f MiB" % (u["bytes"] / 1024 ** 2), u["mem_m"]))
            items.append((u["name"], "CPU", "%.3f cores" % u["cores"], u["cpu_m"]))
        items.sort(key=lambda item: -item[3])

        _section(doc, "Review these first", keep=120)
        for rank, (name, resource, measured, cost) in enumerate(items[:3], 1):
            _priority_card(doc, rank, name, resource, measured, cost, _pct(cost, total_month), total_month)

        _section(doc, "All line items, ranked", keep=120)
        _table(doc, [("Line item", 150, "l"), ("Measured", 72, "r"), ("$/month", 62, "r"),
                     ("% of total", 58, "r"), ("-10%", 56, "r"), ("-20%", 56, "r"), ("-30%", 62, "r")],
               [["%s - %s" % (name, resource), measured, _usd(cost), _pct(cost, total_month),
                 _usd(cost * .1), _usd(cost * .2), _usd(cost * .3)]
                for name, resource, measured, cost in items],
               total_row=["All line items", "", _usd(total_month), "100%",
                          _usd(total_month * .1), _usd(total_month * .2), _usd(total_month * .3)])

        top_three = sum(item[3] for item in items[:3])
        cheapest, dearest = min(priced, key=lambda u: u["month"]), max(priced, key=lambda u: u["month"])
        dominant, other = ("Memory", "CPU") if total_mem >= total_cpu else ("CPU", "Memory")
        dom_cost, other_cost = (total_mem, total_cpu) if total_mem >= total_cpu else (total_cpu, total_mem)
        _section(doc, "What the numbers show", keep=80)
        notes = [
            "%s is the larger cost lever: %s of the run rate (%s of %s per month); %s is %s (%s)." % (
                dominant, _pct(dom_cost, total_month), _usd(dom_cost), _usd(total_month),
                other, _pct(other_cost, total_month), _usd(other_cost)),
            "The largest single line item is %s - %s at %s/month (%s of the total); the top three "
            "line items together are %s." % (
                items[0][0], items[0][1], _usd(items[0][3]), _pct(items[0][3], total_month),
                _pct(top_three, total_month)),
        ]
        if len(priced) > 1:
            notes.append("%s is the most expensive service (%s/month) and %s the least (%s/month), a "
                         "gap of %s/month." % (dearest["name"], _usd(dearest["month"]), cheapest["name"],
                                               _usd(cheapest["month"]), _usd(dearest["month"] - cheapest["month"])))
        spread = max(u["month"] for u in priced) - min(u["month"] for u in priced)
        if total_month and spread / total_month < 0.15:
            notes.append("Costs are spread almost evenly across the four services, so no single service is "
                         "an outlier; a platform-wide request/limit review will move the total more than "
                         "tuning one service.")
        _bullets(doc, notes)

        _section(doc, "Per-service detail", keep=90)
        for u in sorted(priced, key=lambda u: -u["month"]):
            total_line = ("Estimated total $%.6f/hour | $%.2f/month  (CPU $%.2f + memory $%.2f per month)" % (
                u["hour"], u["month"], u["cpu_m"], u["mem_m"]))
            scenario = wrap_text(
                "Illustrative 10%% usage-reduction scenario: CPU ~$%.2f/month; memory ~$%.2f/month "
                "(hypothetical only)." % (
                    u["cpu_m"] * 0.1, u["mem_m"] * 0.1), 8.5, PAGE_W - 2 * MARGIN - 20)
            height = 14 + (1 + len(scenario)) * 11.5 + 6
            doc.need(height + 8)
            top = doc.y - 6
            doc.rect(MARGIN, top - height, PAGE_W - 2 * MARGIN, height, (0.965, 0.988, 0.988))
            doc.rect(MARGIN, top - height, 3, height, TEAL)
            y = top - 16
            doc.text(MARGIN + 12, y, u["name"], 9.5, True)
            doc.text(MARGIN + 126, y, total_line, 8.5, False, INK)
            for line in scenario:
                y -= 11.5
                doc.text(MARGIN + 12, y, line, 8.5, False, MUTED)
            doc.y = top - height - 2

        _section(doc, "Before you cut anything", keep=80)
        _bullets(doc, [
            "This is one point-in-time sample. Confirm peak and p95 usage over a representative period "
            "(at least 7 days, including your busiest hours) before changing any allocation.",
            "Compare against the configured CPU/memory requests and limits: usage far below a request is "
            "the safest saving; usage near a limit means the service is already tight.",
            "Check service impact first: request rate, p95 latency and any open incident for the service.",
            "Any capacity change needs a human decision and the normal approval workflow. Savings count as "
            "realized only after an approved change is applied and measured.",
        ])
    elif not configured:
        _section(doc, "Where we can cut", keep=60)
        _note_box(doc, "Pricing is not configured in this environment, so cost lines cannot be ranked. "
                       "Set KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD and KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD "
                       "to illustrative owner-configured rates to enable this section.")

    # ---- method + important ----
    _section(doc, "How the estimate is calculated", keep=70)
    _bullets(doc, [
        "CPU monthly cost = measured CPU cores x CPU rate per core-hour x 730 hours.",
        "Memory monthly cost = measured memory GiB x memory rate per GiB-hour x 730 hours.",
        "Service total = CPU + memory. Run rate = sum of the four services. Share = service total / run rate.",
    ], size=8.5, rgb=MUTED)

    doc.gap(8)
    _note_box(doc, "Important: " + (report.get("disclaimer") or (
        "Point-in-time Prometheus process usage extrapolated to 730 hours; not a bill. "
        "Excludes host overhead and other workloads.")) + " This report is read-only and does not "
        "change infrastructure.", rgb=TEAL)
    return _serialize(doc)


def _serialize(doc: _Doc) -> bytes:
    objects: list[bytes] = []

    def add(body: str | bytes) -> int:
        objects.append(body if isinstance(body, bytes) else body.encode("latin-1"))
        return len(objects)

    add("<< /Type /Catalog /Pages 2 0 R >>")          # 1
    add("")                                            # 2 (filled below)
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")       # 3
    add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")  # 4
    page_ids = []
    total_pages = len(doc.pages)
    for number, ops in enumerate(doc.pages, 1):
        ops = ops + [
            "%.3f %.3f %.3f RG 0.6 w %d 34 m %d 34 l S" % (*GRID, MARGIN, PAGE_W - MARGIN),
            "%.3f %.3f %.3f rg" % MUTED,
            "BT /F1 7.5 Tf %d 22 Td (Kybernaut - read-only report) Tj ET" % MARGIN,
            "BT /F1 7.5 Tf %.1f 22 Td (Page %d of %d) Tj ET" % (
                PAGE_W - MARGIN - text_width("Page %d of %d" % (number, total_pages), 7.5),
                number, total_pages),
        ]
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(add(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
            "/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>"
            % (PAGE_W, PAGE_H, content)))
    objects[1] = ("<< /Type /Pages /Kids [%s] /Count %d >>" % (
        " ".join("%d 0 R" % i for i in page_ids), len(page_ids))).encode("latin-1")

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)