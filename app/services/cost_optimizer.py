"""FinOps analysis: utilization + pricing -> right-sizing recommendations.

Everything computed here is POTENTIAL savings. ``realized_savings`` is
always 0 unless an operator records that a recommendation was applied.
Prices are never hard-coded: they come from a pricing catalog supplied by
the operator (see config/pricing.example.json -- EXAMPLE VALUES ONLY).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

HOURS_PER_MONTH = 730.0

CPU_UNDERUSE_THRESHOLD = 0.30
MEM_UNDERUSE_THRESHOLD = 0.40
# Target headroom: size so average-driven peak demand fits at this ratio.
TARGET_UTILIZATION = 0.60
MIN_SAMPLES = 24  # refuse to recommend on thin data


class CostAnalysisError(ValueError):
    pass


@dataclass(frozen=True)
class InstanceType:
    name: str
    vcpu: float
    memory_gb: float
    monthly_cost: float


def load_catalog(path: str | Path) -> tuple[list[InstanceType], dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    types = [
        InstanceType(t["name"], float(t["vcpu"]), float(t["memory_gb"]),
                     float(t["monthly_cost"]))
        for t in data.get("instance_types", [])
    ]
    if not types:
        raise CostAnalysisError("Pricing catalog has no instance types")
    for t in types:
        values = (t.vcpu, t.memory_gb, t.monthly_cost)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise CostAnalysisError(f"Invalid values for {t.name}")
    return types, {k: v for k, v in data.items() if k != "instance_types"}


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def analyze_resource(
    resource: dict[str, Any], catalog: list[InstanceType]
) -> dict[str, Any]:
    """Analyze one resource.

    resource = {"name", "instance_type", "cpu_utilization": [0..1 samples],
                "memory_utilization": [0..1 samples]}
    """
    name = resource.get("name")
    current = next((t for t in catalog
                    if t.name == resource.get("instance_type")), None)
    if not name or current is None:
        raise CostAnalysisError(
            f"Resource {name!r}: unknown or missing instance_type")

    cpu_samples = resource.get("cpu_utilization", [])
    mem_samples = resource.get("memory_utilization", [])
    if any(
        isinstance(sample, bool)
        for samples in (cpu_samples, mem_samples)
        for sample in samples
    ):
        raise CostAnalysisError(
            f"Resource {name!r}: boolean utilization samples are invalid")
    cpu = [float(x) for x in cpu_samples]
    mem = [float(x) for x in mem_samples]
    if len(cpu) < MIN_SAMPLES or len(mem) < MIN_SAMPLES:
        return _skip(name, current, "insufficient_data",
                     f"need >= {MIN_SAMPLES} samples per metric")
    if any(not 0.0 <= x <= 1.0 for x in cpu + mem):
        raise CostAnalysisError(f"Resource {name!r}: utilization outside 0..1")

    cpu_avg, mem_avg = mean(cpu), mean(mem)
    cpu_p95, mem_p95 = _pct(cpu, 0.95), _pct(mem, 0.95)

    underused = (cpu_avg < CPU_UNDERUSE_THRESHOLD
                 and mem_avg < MEM_UNDERUSE_THRESHOLD)
    base = {
        "resource": name, "current_type": current.name,
        "current_monthly_cost": current.monthly_cost,
        "cpu_avg": round(cpu_avg, 4), "cpu_p95": round(cpu_p95, 4),
        "memory_avg": round(mem_avg, 4), "memory_p95": round(mem_p95, 4),
        "samples": min(len(cpu), len(mem)),
    }
    if not underused:
        return {**base, "status": "right_sized", "recommended_type": None,
                "recommended_monthly_cost": None,
                "potential_monthly_savings": 0.0,
                "potential_annual_savings": 0.0, "realized_savings": 0.0,
                "rationale": "Utilization does not indicate over-provisioning."}

    # Size on p95 (not average) so bursts still fit.
    need_vcpu = current.vcpu * cpu_p95 / TARGET_UTILIZATION
    need_mem = current.memory_gb * mem_p95 / TARGET_UTILIZATION
    fits = [t for t in catalog
            if t.vcpu >= need_vcpu and t.memory_gb >= need_mem
            and t.monthly_cost < current.monthly_cost]
    if not fits:
        return {**base, "status": "no_cheaper_fit",
                "recommended_type": None, "recommended_monthly_cost": None,
                "potential_monthly_savings": 0.0,
                "potential_annual_savings": 0.0, "realized_savings": 0.0,
                "rationale": "No cheaper catalog type satisfies p95 demand."}

    best = min(fits, key=lambda t: t.monthly_cost)
    monthly = round(current.monthly_cost - best.monthly_cost, 2)
    return {
        **base, "status": "downsize_candidate",
        "recommended_type": best.name,
        "recommended_monthly_cost": best.monthly_cost,
        "potential_monthly_savings": monthly,
        "potential_annual_savings": round(monthly * 12, 2),
        "realized_savings": 0.0,
        "rationale": (
            f"Average CPU {cpu_avg:.0%} / memory {mem_avg:.0%}; p95 demand "
            f"fits {best.name} at <= {TARGET_UTILIZATION:.0%} target."),
    }


def recommend_kubernetes_requests(
    *,
    name: str,
    cpu_utilization: list[float],
    memory_utilization: list[float],
    current_cpu_request_cores: float,
    current_memory_request_bytes: float,
    minimum_peak_headroom: float = 1.25,
) -> dict[str, Any]:
    """Suggest aggregate Kubernetes requests without assigning a monetary value."""
    for label, amount in (
        ("CPU request", current_cpu_request_cores),
        ("memory request", current_memory_request_bytes),
    ):
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount <= 0
        ):
            raise CostAnalysisError(f"Resource {name!r}: invalid {label}")
    if len(cpu_utilization) < MIN_SAMPLES or len(memory_utilization) < MIN_SAMPLES:
        raise CostAnalysisError(
            f"Resource {name!r}: insufficient samples for request sizing"
        )
    if (
        isinstance(minimum_peak_headroom, bool)
        or not isinstance(minimum_peak_headroom, (int, float))
        or not math.isfinite(minimum_peak_headroom)
        or minimum_peak_headroom < 1
    ):
        raise CostAnalysisError("minimum_peak_headroom must be at least 1")
    samples = cpu_utilization + memory_utilization
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in samples
    ):
        raise CostAnalysisError(
            f"Resource {name!r}: utilization samples must be finite and nonnegative"
        )

    cpu_p95 = _pct(cpu_utilization, 0.95)
    memory_p95 = _pct(memory_utilization, 0.95)
    if cpu_p95 == 0 or memory_p95 == 0:
        return {
            "status": "request_adjustment_review",
            "cpu_request_current_cores": current_cpu_request_cores,
            "cpu_request_recommended_cores": None,
            "memory_request_current_bytes": current_memory_request_bytes,
            "memory_request_recommended_bytes": None,
            "sizing_target_utilization": TARGET_UTILIZATION,
            "pricing_status": "unavailable",
            "potential_monthly_savings": None,
            "potential_annual_savings": None,
            "realized_savings": 0.0,
            "rationale": (
                "At least one p95 usage metric is zero; a positive request "
                "cannot be sized from this evidence."
            ),
        }
    cpu_needed = max(
        current_cpu_request_cores * cpu_p95 / TARGET_UTILIZATION,
        current_cpu_request_cores * max(cpu_utilization) * minimum_peak_headroom,
    )
    memory_needed = max(
        current_memory_request_bytes * memory_p95 / TARGET_UTILIZATION,
        current_memory_request_bytes
        * max(memory_utilization) * minimum_peak_headroom,
    )
    cpu_recommended = math.ceil(
        cpu_needed * 1000
        - 1e-9
    ) / 1000
    mib = 1024 ** 2
    memory_recommended = math.ceil(
        memory_needed / mib - 1e-9
    ) * mib
    cpu_down = cpu_recommended < current_cpu_request_cores
    memory_down = memory_recommended < current_memory_request_bytes
    cpu_up = cpu_recommended > current_cpu_request_cores
    memory_up = memory_recommended > current_memory_request_bytes
    if (cpu_down or memory_down) and not (cpu_up or memory_up):
        status = "request_downsize_candidate"
    elif cpu_up or memory_up:
        status = "request_adjustment_review"
    else:
        status = "requests_unchanged"
    return {
        "status": status,
        "cpu_request_current_cores": current_cpu_request_cores,
        "cpu_request_recommended_cores": cpu_recommended,
        "memory_request_current_bytes": current_memory_request_bytes,
        "memory_request_recommended_bytes": memory_recommended,
        "sizing_target_utilization": TARGET_UTILIZATION,
        "minimum_peak_headroom": minimum_peak_headroom,
        "pricing_status": "unavailable",
        "potential_monthly_savings": None,
        "potential_annual_savings": None,
        "realized_savings": 0.0,
        "rationale": (
            f"Aggregate requests are sized from p95 observed utilization at "
            f"{TARGET_UTILIZATION:.0%} target; no Kubernetes request pricing "
            "model is configured."
        ),
    }


def _skip(name, current, status, why):
    return {"resource": name, "current_type": current.name,
            "current_monthly_cost": current.monthly_cost, "status": status,
            "recommended_type": None, "recommended_monthly_cost": None,
            "potential_monthly_savings": 0.0,
            "potential_annual_savings": 0.0, "realized_savings": 0.0,
            "rationale": why}


def build_report(
    resources: list[dict], catalog: list[InstanceType], meta: dict | None = None
) -> dict[str, Any]:
    rows = [analyze_resource(r, catalog) for r in resources]
    monthly = round(sum(r["potential_monthly_savings"] for r in rows), 2)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pricing_source": (meta or {}).get("source", "unspecified"),
        "pricing_is_example": bool((meta or {}).get("example", False)),
        "recommendations": rows,
        "summary": {
            "resources_analyzed": len(rows),
            "downsize_candidates": sum(
                r["status"] == "downsize_candidate" for r in rows),
            "potential_monthly_savings": monthly,
            "potential_annual_savings": round(monthly * 12, 2),
            "realized_savings": 0.0,
        },
        "disclaimer": (
            "Savings are potential estimates from the supplied utilization "
            "and pricing data; none are realized until applied."),
    }


def render_markdown(report: dict[str, Any]) -> str:
    s = report["summary"]
    lines = [
        "# Kybernaut Weekly Infrastructure & Cost Report", "",
        f"Generated: {report['generated_at']}",
        f"Pricing source: {report['pricing_source']}"
        + (" **(EXAMPLE PRICES - not real)**"
           if report["pricing_is_example"] else ""), "",
        "## Summary", "",
        f"- Resources analyzed: {s['resources_analyzed']}",
        f"- Downsize candidates: {s['downsize_candidates']}",
        f"- Potential monthly savings: ${s['potential_monthly_savings']:,.2f}",
        f"- Potential annual savings: ${s['potential_annual_savings']:,.2f}",
        f"- Realized savings: ${s['realized_savings']:,.2f}", "",
        "## Recommendations", "",
        "| Resource | Status | Current | Recommended | CPU avg/p95 | "
        "Mem avg/p95 | Potential $/mo |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report["recommendations"]:
        cpu = (f"{r['cpu_avg']:.0%}/{r['cpu_p95']:.0%}"
               if "cpu_avg" in r else "n/a")
        mem = (f"{r['memory_avg']:.0%}/{r['memory_p95']:.0%}"
               if "memory_avg" in r else "n/a")
        lines.append(
            f"| {r['resource']} | {r['status']} | {r['current_type']} | "
            f"{r['recommended_type'] or '-'} | {cpu} | {mem} | "
            f"{r['potential_monthly_savings']:,.2f} |")
    lines += ["", report["disclaimer"], ""]
    return "\n".join(lines)
