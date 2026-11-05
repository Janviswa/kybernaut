"""Read-only weekly utilization analysis and optional report delivery."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import smtplib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from statistics import mean, pstdev
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.cost_optimizer import (
    MIN_SAMPLES,
    TARGET_UTILIZATION,
    InstanceType,
    analyze_resource,
    recommend_kubernetes_requests,
)
from app.services.kubernetes_cost_model import (
    KubernetesPricing,
    estimate_request_cost,
    savings_between,
)

logger = logging.getLogger(__name__)
DEFAULT_RECIPIENT = "jananiviswa05@gmail.com"
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
SAFE_PROVENANCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/();'=-]{0,499}$")
SAFE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SECRET_MARKER = re.compile(
    r"(?i)(password|secret|token|api[_-]?key)\s*[:=]"
)


class WeeklyCostReportError(ValueError):
    pass


@dataclass(frozen=True)
class WeeklyCostConfig:
    min_samples: int = MIN_SAMPLES
    minimum_coverage: float = 0.8
    low_cpu_average: float = 0.3
    low_memory_average: float = 0.4
    pressure_threshold: float = 0.85
    pressure_sample_ratio: float = 0.1
    maximum_downsize_peak: float = 0.8
    minimum_peak_headroom: float = 1.25

    @classmethod
    def from_env(cls) -> "WeeklyCostConfig":
        def number(name: str, converter, default: str):
            try:
                return converter(os.getenv(name, default))
            except (TypeError, ValueError):
                raise ValueError(f"{name} must be numeric") from None

        return cls(
            min_samples=number("KYBERNAUT_COST_MIN_SAMPLES", int, "24"),
            minimum_coverage=number(
                "KYBERNAUT_COST_MINIMUM_COVERAGE", float, "0.8"
            ),
            low_cpu_average=number(
                "KYBERNAUT_COST_LOW_CPU_AVERAGE", float, "0.3"
            ),
            low_memory_average=number(
                "KYBERNAUT_COST_LOW_MEMORY_AVERAGE", float, "0.4"
            ),
            pressure_threshold=number(
                "KYBERNAUT_COST_PRESSURE_THRESHOLD", float, "0.85"
            ),
            pressure_sample_ratio=number(
                "KYBERNAUT_COST_PRESSURE_SAMPLE_RATIO", float, "0.1"
            ),
            maximum_downsize_peak=number(
                "KYBERNAUT_COST_MAXIMUM_DOWNSIZE_PEAK", float, "0.8"
            ),
            minimum_peak_headroom=number(
                "KYBERNAUT_COST_MINIMUM_PEAK_HEADROOM", float, "1.25"
            ),
        )

    def __post_init__(self):
        if self.min_samples < MIN_SAMPLES:
            raise ValueError(f"min_samples must be at least {MIN_SAMPLES}")
        for name in (
            "minimum_coverage", "low_cpu_average", "low_memory_average",
            "pressure_threshold", "pressure_sample_ratio",
            "maximum_downsize_peak",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if (
            not math.isfinite(self.minimum_peak_headroom)
            or self.minimum_peak_headroom < 1
        ):
            raise ValueError("minimum_peak_headroom must be at least 1")


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise WeeklyCostReportError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WeeklyCostReportError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WeeklyCostReportError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def last_completed_week(
    timezone_name: str = "UTC", *, now: datetime | None = None
) -> tuple[str, str]:
    """Return the previous full Monday-to-Monday period in an IANA timezone."""
    try:
        zone = ZoneInfo(timezone_name)
    except (TypeError, ZoneInfoNotFoundError) as exc:
        raise WeeklyCostReportError(
            f"unknown reporting timezone: {timezone_name}"
        ) from exc
    current = now or datetime.now(zone)
    if current.tzinfo is None or current.utcoffset() is None:
        raise WeeklyCostReportError("now must include a timezone")
    local_now = current.astimezone(zone)
    this_monday = local_now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=local_now.weekday())
    start = this_monday - timedelta(days=7)
    return start.isoformat(), this_monday.isoformat()


def analyze_week(
    observations: list[dict[str, Any]],
    *,
    period_start: str,
    period_end: str,
    sample_interval_minutes: int,
    catalog: list[InstanceType],
    provenance: str,
    pricing_meta: dict | None = None,
    config: WeeklyCostConfig | None = None,
    kubernetes_resources: dict[str, dict[str, Any]] | None = None,
    kubernetes_pricing: KubernetesPricing | None = None,
) -> dict:
    """Analyze observed samples in [period_start, period_end), without filling gaps."""
    config = config or WeeklyCostConfig()
    if not isinstance(observations, list):
        raise WeeklyCostReportError("observations must be an array")
    start = _timestamp(period_start, "period_start")
    end = _timestamp(period_end, "period_end")
    if start >= end:
        raise WeeklyCostReportError("period_start must precede period_end")
    if kubernetes_pricing is not None and kubernetes_resources is None:
        raise WeeklyCostReportError(
            "Kubernetes request pricing cannot be used with a VM catalog report"
        )
    if (
        kubernetes_pricing is not None
        and kubernetes_pricing.effective_date > start.date()
    ):
        raise WeeklyCostReportError(
            "Kubernetes pricing is not effective at the start of the report period"
        )
    if (
        isinstance(sample_interval_minutes, bool)
        or not isinstance(sample_interval_minutes, int)
        or sample_interval_minutes < 1
    ):
        raise WeeklyCostReportError("sample_interval_minutes must be positive")
    if (
        not isinstance(provenance, str)
        or not SAFE_PROVENANCE.fullmatch(provenance)
        or SECRET_MARKER.search(provenance)
    ):
        raise WeeklyCostReportError(
            "provenance must be a non-secret label of at most 500 characters"
        )

    expected = math.ceil(
        (end - start).total_seconds() / (sample_interval_minutes * 60)
    )
    grouped: dict[tuple[str, str, str, str], list[tuple[datetime, float, float]]] = {}
    catalog_names = {entry.name for entry in catalog}
    seen: set[tuple[str, int]] = set()
    resource_metadata: dict[str, tuple[str, str, str]] = {}
    for index, item in enumerate(observations):
        if not isinstance(item, dict):
            raise WeeklyCostReportError(f"observation {index} must be an object")
        timestamp = _timestamp(item.get("timestamp"), f"observation {index} timestamp")
        if not start <= timestamp < end:
            continue
        name, service, instance_type = (
            item.get("resource"), item.get("service"), item.get("instance_type")
        )
        if not all(isinstance(value, str) and value.strip() for value in (
            name, service, instance_type
        )):
            raise WeeklyCostReportError(
                f"observation {index} requires resource, service, and instance_type"
            )
        if not all(
            SAFE_IDENTIFIER.fullmatch(value)
            for value in (name, service, instance_type)
        ):
            raise WeeklyCostReportError(
                f"observation {index} contains an invalid resource identifier"
            )
        importance = item.get("importance", "standard")
        if not isinstance(importance, str) or importance not in {
            "standard", "critical", "tier-0"
        }:
            raise WeeklyCostReportError(
                f"observation {index} has unsupported importance"
            )
        metadata = (service.strip(), instance_type.strip(), importance)
        previous = resource_metadata.setdefault(name.strip(), metadata)
        if previous != metadata:
            raise WeeklyCostReportError(
                "resource metadata changed during the reporting period"
            )
        kubernetes_mode = kubernetes_resources is not None
        if kubernetes_mode:
            if (
                instance_type != "kubernetes-request"
                or name.strip() not in kubernetes_resources
            ):
                raise WeeklyCostReportError(
                    f"observation {index} has no matching Kubernetes resource metadata"
                )
        elif instance_type not in catalog_names:
            raise WeeklyCostReportError(
                f"observation {index} uses unknown instance_type"
            )
        bucket = int(
            (timestamp - start).total_seconds()
            // (sample_interval_minutes * 60)
        )
        identity = (name.strip(), bucket)
        if identity in seen:
            raise WeeklyCostReportError(
                "multiple samples in a resource reporting interval"
            )
        seen.add(identity)
        values = []
        for metric in ("cpu_utilization", "memory_utilization"):
            value = item.get(metric)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise WeeklyCostReportError(
                    f"observation {index} requires numeric {metric}"
                )
            value = float(value)
            if (
                not math.isfinite(value)
                or value < 0
                or (not kubernetes_mode and value > 1)
            ):
                raise WeeklyCostReportError(
                    f"observation {index} {metric} has an invalid value"
                )
            values.append(value)
        key = (name.strip(), service.strip(), instance_type.strip(), importance)
        grouped.setdefault(key, []).append((timestamp, *values))

    recommendations = []
    for (name, service, instance_type, importance), samples in sorted(
        grouped.items()
    ):
        samples.sort(key=lambda sample: sample[0])
        cpu = [sample[1] for sample in samples]
        memory = [sample[2] for sample in samples]
        count = len(samples)
        coverage = min(1.0, count / expected) if expected else 0.0
        row: dict[str, Any] = {
            "resource": name,
            "service": service,
            "importance": importance,
            "current_type": instance_type,
            "sample_count": count,
            "expected_samples": expected,
            "missing_samples": max(0, expected - count),
            "coverage": round(coverage, 4),
            "first_observed_at": samples[0][0].isoformat(),
            "last_observed_at": samples[-1][0].isoformat(),
        }
        if kubernetes_resources is not None:
            resource_info = kubernetes_resources[name]
            row.update({
                "current_type": "Kubernetes aggregate requests",
                "namespace": resource_info["namespace"],
                "deployment_uid": resource_info.get("deployment_uid"),
                "resource_version": resource_info.get("resource_version"),
                "replicas": resource_info["replicas"],
                "cpu_request_current_cores": resource_info[
                    "cpu_request_cores"
                ],
                "memory_request_current_bytes": resource_info[
                    "memory_request_bytes"
                ],
                "cpu_limit_cores": resource_info["cpu_limit_cores"],
                "memory_limit_bytes": resource_info["memory_limit_bytes"],
                "cpu_limits_complete": resource_info.get(
                    "cpu_limits_complete", False
                ),
                "stale_samples": resource_info.get("stale_samples", 0),
                "complete_samples": resource_info.get("complete_samples", 0),
                "container_identity": resource_info.get("containers", []),
            })
            if kubernetes_pricing is not None:
                hours = (end - start).total_seconds() / 3600
                workload_name = resource_info.get(
                    "deployment", name.rsplit("/", 1)[-1]
                )
                row["current_estimated_request_cost"] = estimate_request_cost(
                    cpu_cores=resource_info["cpu_request_cores"],
                    memory_bytes=resource_info["memory_request_bytes"],
                    hours=hours,
                    pricing=kubernetes_pricing,
                    workload=workload_name,
                )
        if count < config.min_samples:
            row.update({
                "status": "insufficient_data",
                "rationale": (
                    f"Only {count} observations; at least "
                    f"{config.min_samples} are required."
                ),
                "suggested_next_step": "Collect more timestamped observations.",
                "potential_monthly_savings": None,
                "realized_savings": 0.0,
            })
            recommendations.append(row)
            continue

        cpu_avg, memory_avg = mean(cpu), mean(memory)
        cpu_p95 = _percentile(cpu, 0.95)
        memory_p95 = _percentile(memory, 0.95)
        cpu_peak, memory_peak = max(cpu), max(memory)
        pressure_samples = sum(
            value >= config.pressure_threshold for value in cpu
        ) + sum(value >= config.pressure_threshold for value in memory)
        metric_samples = count * 2
        pressure_ratio = pressure_samples / metric_samples
        row.update({
            "cpu_avg": round(cpu_avg, 4),
            "cpu_p95": round(cpu_p95, 4),
            "cpu_peak": round(cpu_peak, 4),
            "cpu_variability": round(pstdev(cpu), 4),
            "memory_avg": round(memory_avg, 4),
            "memory_p95": round(memory_p95, 4),
            "memory_peak": round(memory_peak, 4),
            "memory_variability": round(pstdev(memory), 4),
            "pressure_threshold": config.pressure_threshold,
            "pressure_sample_ratio": round(pressure_ratio, 4),
            "potential_monthly_savings": (
                None if kubernetes_resources is not None else 0.0
            ),
            "realized_savings": 0.0,
        })

        if pressure_ratio >= config.pressure_sample_ratio and pressure_samples:
            row.update({
                "status": "capacity_pressure",
                "recommendation": (
                    "Investigate a capacity increase; no size is selected from "
                    "utilization alone."
                ),
                "rationale": (
                    f"{pressure_ratio:.0%} of observed CPU/memory values reached "
                    f"the {config.pressure_threshold:.0%} threshold."
                ),
                "suggested_next_step": (
                    "Review workload growth, limits, quotas, and capacity before "
                    "considering a capacity increase."
                ),
                "operational_risk": (
                    "Sustained high utilization can increase latency or cause "
                    "resource exhaustion."
                ),
                "coverage_warning": (
                    f"Only {coverage:.0%} of expected samples are present."
                    if coverage < config.minimum_coverage else None
                ),
            })
        elif coverage < config.minimum_coverage:
            row.update({
                "status": "incomplete_period",
                "rationale": (
                    f"Only {coverage:.0%} of expected samples are present; "
                    "recommendations are withheld."
                ),
                "suggested_next_step": "Review telemetry gaps before deciding.",
            })
        elif (
            cpu_avg < config.low_cpu_average
            and memory_avg < config.low_memory_average
        ):
            if importance.lower() in {"critical", "tier-0"}:
                row.update({
                    "status": "human_review_required",
                    "rationale": (
                        "Average utilization is low, but resource importance "
                        "prevents an automated downsize recommendation."
                    ),
                    "suggested_next_step": (
                        "Have the service owner review peak demand and resilience "
                        "requirements."
                    ),
                })
            elif max(cpu_peak, memory_peak) > config.maximum_downsize_peak:
                row.update({
                    "status": "peak_capacity_risk",
                    "rationale": (
                        "Low averages are offset by a peak above the configured "
                        f"{config.maximum_downsize_peak:.0%} downsize guard."
                    ),
                    "suggested_next_step": (
                        "Review peak-period traffic and burst capacity before sizing."
                    ),
                })
            else:
                if kubernetes_resources is not None:
                    resource_info = kubernetes_resources[name]
                    optimizer = recommend_kubernetes_requests(
                        name=name,
                        cpu_utilization=cpu,
                        memory_utilization=memory,
                        current_cpu_request_cores=resource_info[
                            "cpu_request_cores"
                        ],
                        current_memory_request_bytes=resource_info[
                            "memory_request_bytes"
                        ],
                        minimum_peak_headroom=config.minimum_peak_headroom,
                    )
                    row.update(optimizer)
                    if (
                        kubernetes_pricing is not None
                        and optimizer["cpu_request_recommended_cores"] is not None
                        and optimizer["memory_request_recommended_bytes"] is not None
                    ):
                        proposed_cost = estimate_request_cost(
                            cpu_cores=optimizer[
                                "cpu_request_recommended_cores"
                            ],
                            memory_bytes=optimizer[
                                "memory_request_recommended_bytes"
                            ],
                            hours=(end - start).total_seconds() / 3600,
                            pricing=kubernetes_pricing,
                            workload=resource_info.get(
                                "deployment", name.rsplit("/", 1)[-1]
                            ),
                        )
                        row["proposed_estimated_request_cost"] = proposed_cost
                        row["request_cost_savings"] = savings_between(
                            row["current_estimated_request_cost"],
                            proposed_cost,
                        )
                    row.update({
                        "service": service,
                        "importance": importance,
                        "sample_count": count,
                        "expected_samples": expected,
                        "coverage": round(coverage, 4),
                        "cpu_peak": round(cpu_peak, 4),
                        "memory_peak": round(memory_peak, 4),
                        "cpu_variability": round(pstdev(cpu), 4),
                        "memory_variability": round(pstdev(memory), 4),
                        "suggested_next_step": (
                            "Have the workload owner review per-container "
                            "allocation, rollout behavior, and headroom before "
                            "changing requests."
                        ),
                        "confidence": "moderate; based on observed period and requests",
                        "recommendation": (
                            "Review aggregate CPU request "
                            f"{optimizer['cpu_request_current_cores']:.3f} -> "
                            f"{optimizer['cpu_request_recommended_cores']:.3f} "
                            "cores and memory request "
                            f"{optimizer['memory_request_current_bytes']:.0f} -> "
                            f"{optimizer['memory_request_recommended_bytes']:.0f} "
                            "bytes; no cost estimate is available."
                            if optimizer["status"] == "request_downsize_candidate"
                            else "Review observed demand and aggregate request sizing; "
                            "no cost estimate is available."
                        ),
                        "operational_risk": (
                            "Aggregate sizing does not determine a safe "
                            "per-container split."
                        ),
                    })
                else:
                    optimizer = analyze_resource({
                        "name": name,
                        "instance_type": instance_type,
                        "cpu_utilization": cpu,
                        "memory_utilization": memory,
                    }, catalog)
                    row.update(optimizer)
                    row.update({
                        "service": service,
                        "importance": importance,
                        "sample_count": count,
                        "expected_samples": expected,
                        "coverage": round(coverage, 4),
                        "cpu_peak": round(cpu_peak, 4),
                        "memory_peak": round(memory_peak, 4),
                        "cpu_variability": round(pstdev(cpu), 4),
                        "memory_variability": round(pstdev(memory), 4),
                        "suggested_next_step": (
                            "Validate resilience, seasonal peaks, and service-owner "
                            "requirements before making any change."
                        ),
                        "confidence": "moderate; based on observed period and catalog",
                        "recommendation": (
                            (
                                f"Review resizing from {instance_type} to "
                                f"{optimizer['recommended_type']}."
                            )
                            if optimizer["status"] == "downsize_candidate"
                            else "No cheaper catalog fit satisfies p95 demand."
                        ),
                        "operational_risk": (
                            "Demand may change outside this reporting period."
                        ),
                    })
        else:
            row.update({
                "status": "no_downsize_signal",
                "rationale": (
                    "CPU and memory averages do not both meet the low-use criteria."
                ),
                "suggested_next_step": "Continue monitoring the next reporting period.",
            })
        recommendations.append(row)

    if kubernetes_resources is not None:
        reported = {row["resource"] for row in recommendations}
        for name, resource_info in sorted(kubernetes_resources.items()):
            if name in reported:
                continue
            recommendations.append({
                "resource": name,
                "service": resource_info.get("deployment", name),
                "importance": "standard",
                "current_type": "Kubernetes aggregate requests",
                "namespace": resource_info.get("namespace"),
                "deployment_uid": resource_info.get("deployment_uid"),
                "resource_version": resource_info.get("resource_version"),
                "replicas": resource_info.get("replicas"),
                "cpu_request_current_cores": resource_info.get(
                    "cpu_request_cores"
                ),
                "memory_request_current_bytes": resource_info.get(
                    "memory_request_bytes"
                ),
                "cpu_limit_cores": resource_info.get("cpu_limit_cores"),
                "memory_limit_bytes": resource_info.get("memory_limit_bytes"),
                "cpu_limits_complete": resource_info.get(
                    "cpu_limits_complete", False
                ),
                "stale_samples": resource_info.get("stale_samples", 0),
                "complete_samples": resource_info.get("complete_samples", 0),
                "sample_count": 0,
                "expected_samples": expected,
                "missing_samples": expected,
                "coverage": 0.0,
                "status": "incomplete_period",
                "potential_monthly_savings": None,
                "realized_savings": 0.0,
                "rationale": "No complete CPU and memory sample pairs were collected.",
                "suggested_next_step": (
                    "Check the configured PromQL queries, scrape coverage, and "
                    "pod/container identity labels."
                ),
            })
            if kubernetes_pricing is not None:
                recommendations[-1]["current_estimated_request_cost"] = (
                    estimate_request_cost(
                        cpu_cores=resource_info["cpu_request_cores"],
                        memory_bytes=resource_info["memory_request_bytes"],
                        hours=(end - start).total_seconds() / 3600,
                        pricing=kubernetes_pricing,
                        workload=resource_info.get(
                            "deployment", name.rsplit("/", 1)[-1]
                        ),
                    )
                )

    monthly = (
        None if kubernetes_resources is not None else round(sum(
            item.get("potential_monthly_savings") or 0
            for item in recommendations
        ), 2)
    )
    cost_savings = [
        row["request_cost_savings"]["potential_savings"]
        for row in recommendations
        if isinstance(row.get("request_cost_savings"), dict)
    ]
    resource_cost_savings = (
        round(sum(cost_savings), 8) if cost_savings else None
    )
    source = (
        (
            kubernetes_pricing.source
            if kubernetes_pricing is not None
            else "unavailable: Kubernetes request pricing is not configured"
        )
        if kubernetes_resources is not None
        else (pricing_meta or {}).get("source", "unspecified")
    )
    if not isinstance(source, str) or not SAFE_PROVENANCE.fullmatch(source) \
            or SECRET_MARKER.search(source):
        source = "unsafe pricing source label omitted"
    return {
        "schema_version": 1,
        "reporting_period": {
            "start": start.isoformat(),
            "end_exclusive": end.isoformat(),
            "sample_interval_minutes": sample_interval_minutes,
            "expected_samples_per_resource": expected,
            "minimum_coverage": config.minimum_coverage,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance,
        "pricing": {
            **(
                kubernetes_pricing.report_metadata()
                if kubernetes_pricing is not None else {}
            ),
            "source": source,
            "is_example": (
                kubernetes_pricing.example
                if kubernetes_pricing is not None
                else (
                    False if kubernetes_resources is not None
                    else bool((pricing_meta or {}).get("example", False))
                )
            ),
            "assumption": (
                (
                    "Requested-resource estimates use the configured rates and "
                    "are not provider invoice costs."
                    if kubernetes_pricing is not None
                    else "No Kubernetes request pricing model is configured; "
                    "monetary estimates are unavailable."
                )
                if kubernetes_resources is not None
                else "Catalog prices are estimates; no pricing reflects a bill."
            ),
            "cost_model_status": (
                (
                    "configured"
                    if kubernetes_pricing is not None
                    else "unavailable"
                )
                if kubernetes_resources is not None else "catalog"
            ),
        },
        "thresholds": {
            "low_cpu_average": config.low_cpu_average,
            "low_memory_average": config.low_memory_average,
            "pressure_threshold": config.pressure_threshold,
            "pressure_sample_ratio": config.pressure_sample_ratio,
            "maximum_downsize_peak": config.maximum_downsize_peak,
            "sizing_target_utilization": (
                TARGET_UTILIZATION if kubernetes_resources is not None else None
            ),
            "minimum_peak_headroom": (
                config.minimum_peak_headroom
                if kubernetes_resources is not None else None
            ),
        },
        "recommendations": recommendations,
        "summary": {
            "resources_analyzed": len(recommendations),
            "underutilized_candidates": sum(
                row["status"] in {
                    "downsize_candidate", "request_downsize_candidate"
                }
                for row in recommendations
            ),
            "capacity_pressure_resources": sum(
                row["status"] == "capacity_pressure"
                for row in recommendations
            ),
            "incomplete_or_insufficient": sum(
                row["status"] in {"incomplete_period", "insufficient_data"}
                for row in recommendations
            ),
            "potential_monthly_savings": monthly,
            "estimated_annual_savings": (
                None if monthly is None else round(monthly * 12, 2)
            ),
            "potential_request_cost_savings": resource_cost_savings,
            "request_cost_savings_currency": (
                kubernetes_pricing.currency
                if kubernetes_pricing is not None else None
            ),
            "realized_savings": 0.0,
        },
        "decision_required": any(
            row["status"] in {
                "downsize_candidate", "request_downsize_candidate",
                "capacity_pressure",
                "human_review_required", "peak_capacity_risk",
            }
            for row in recommendations
        ),
        "disclaimer": (
            "Recommendations are read-only analysis, not infrastructure "
            "changes. Savings are estimates, not realized savings. A human "
            "decision is required before any future capacity change."
        ),
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(quantile * (len(ordered) - 1))))
    return ordered[index]


def synthetic_weekly_observations(
    period_start: str,
    period_end: str,
    *,
    sample_interval_minutes: int = 60,
) -> list[dict]:
    """Generate a fixed local-only dataset; no monitoring clients are used."""
    start = _timestamp(period_start, "period_start")
    end = _timestamp(period_end, "period_end")
    if start >= end:
        raise WeeklyCostReportError("period_start must precede period_end")
    if (
        isinstance(sample_interval_minutes, bool)
        or not isinstance(sample_interval_minutes, int)
        or sample_interval_minutes < 1
    ):
        raise WeeklyCostReportError("sample_interval_minutes must be positive")
    count = math.ceil(
        (end - start).total_seconds() / (sample_interval_minutes * 60)
    )
    observations = []
    resources = (
        ("worker-underused", "orders", "example.xlarge", "standard", count),
        ("worker-pressure", "payments", "example.xlarge", "standard", count),
        ("worker-critical", "identity", "example.xlarge", "critical", count),
        ("worker-gapped", "inventory", "example.xlarge", "standard", 120),
        ("worker-thin", "batch", "example.xlarge", "standard", 8),
    )
    for name, service, instance_type, importance, samples in resources:
        for index in range(samples):
            if name == "worker-pressure" and index >= count - 40:
                cpu, memory = 0.9, 0.7
            elif name == "worker-pressure":
                cpu, memory = 0.45, 0.5
            else:
                cpu = 0.1 + (index % 5) * 0.01
                memory = 0.2 + (index % 4) * 0.01
            observations.append({
                "timestamp": (
                    start
                    + timedelta(minutes=index * sample_interval_minutes)
                ).isoformat(),
                "resource": name,
                "service": service,
                "instance_type": instance_type,
                "importance": importance,
                "cpu_utilization": cpu,
                "memory_utilization": memory,
            })
    return observations


def render_weekly_markdown(report: dict) -> str:
    period = report["reporting_period"]
    summary = report["summary"]
    lines = [
        "# Kybernaut Weekly Cost Optimization Report",
        "",
        f"Period: {period['start']} through {period['end_exclusive']} (end exclusive)",
        f"Generated: {report['generated_at']}",
        f"Data provenance: {report['provenance']}",
        (
            f"Pricing: {report['pricing']['source']}"
            + (" (EXAMPLE pricing)" if report["pricing"]["is_example"] else "")
        ),
        "",
        "## Summary",
        "",
        f"- Resources analyzed: {summary['resources_analyzed']}",
        f"- Potential downsize candidates: {summary['underutilized_candidates']}",
        f"- Capacity pressure resources: {summary['capacity_pressure_resources']}",
        f"- Incomplete/insufficient resources: {summary['incomplete_or_insufficient']}",
        (
            f"- Potential requested-resource allocation savings for this period: "
            f"{summary['request_cost_savings_currency']} "
            f"{summary['potential_request_cost_savings']:,.4f} "
            "(not an invoice reduction)"
            if summary["potential_request_cost_savings"] is not None
            else "- Potential requested-resource allocation savings: not estimated"
        ),
        (
            f"- Estimated potential monthly savings: "
            f"${summary['potential_monthly_savings']:,.2f}"
            if summary["potential_monthly_savings"] is not None
            else "- Estimated potential monthly savings: not estimated"
        ),
        (
            f"- Estimated potential annual savings: "
            f"${summary['estimated_annual_savings']:,.2f}"
            if summary["estimated_annual_savings"] is not None
            else "- Estimated potential annual savings: not estimated"
        ),
        f"- Realized savings: ${summary['realized_savings']:,.2f}",
        "",
        "## Resource findings",
        "",
    ]
    for row in report["recommendations"]:
        lines.extend([
            f"### {row['service']} / {row['resource']} - {row['status']}",
            "",
            (
                f"- Evidence: {row.get('sample_count', 0)} observed of "
                f"{row.get('expected_samples', 0)} expected samples "
                f"({row.get('coverage', 0):.0%} coverage; "
                f"{row.get('missing_samples', 0)} missing)."
            ),
        ])
        if "cpu_avg" in row:
            lines.extend([
                f"- CPU average / p95 / peak: {row['cpu_avg']:.0%} / "
                f"{row['cpu_p95']:.0%} / {row['cpu_peak']:.0%}.",
                f"- Memory average / p95 / peak: {row['memory_avg']:.0%} / "
                f"{row['memory_p95']:.0%} / {row['memory_peak']:.0%}.",
            ])
        lines.append(f"- Rationale: {row['rationale']}")
        if row.get("cpu_request_recommended_cores") is not None:
            lines.append(
                "- Aggregate request proposal: CPU "
                f"{row['cpu_request_current_cores']:.3f} -> "
                f"{row['cpu_request_recommended_cores']:.3f} cores; memory "
                f"{row['memory_request_current_bytes']:.0f} -> "
                f"{row['memory_request_recommended_bytes']:.0f} bytes."
            )
        if row.get("current_estimated_request_cost"):
            current_cost = row["current_estimated_request_cost"]
            lines.append(
                f"- Current estimated request cost: {current_cost['currency']} "
                f"{current_cost['total']:.4f} for the reporting period "
                f"(not an invoice amount)."
            )
        if row.get("proposed_estimated_request_cost"):
            proposed_cost = row["proposed_estimated_request_cost"]
            cost_savings = row["request_cost_savings"]
            lines.append(
                f"- Proposed estimated request cost: {proposed_cost['currency']} "
                f"{proposed_cost['total']:.4f}; potential allocation-cost "
                f"savings: {cost_savings['currency']} "
                f"{cost_savings['potential_savings']:.4f}"
                + (
                    f" ({cost_savings['potential_savings_percent']:.2f}%)."
                    if cost_savings["potential_savings_percent"] is not None
                    else "."
                )
            )
        if row.get("recommended_type"):
            lines.append(
                f"- Proposed capacity: {row['current_type']} -> "
                f"{row['recommended_type']}; estimated ${row['potential_monthly_savings']:.2f}/month."
            )
        elif report["pricing"].get("model") == "kubernetes_requests":
            if not row.get("request_cost_savings"):
                lines.append(
                    "- Request allocation savings: not estimated; no safe "
                    "proposed resize cost is available."
                )
        elif row.get("potential_monthly_savings") is None:
            lines.append("- Potential savings: not estimated; pricing unavailable.")
        if row.get("recommendation"):
            lines.append(f"- Recommendation: {row['recommendation']}")
        if row.get("operational_risk"):
            lines.append(f"- Risk: {row['operational_risk']}")
        if row.get("coverage_warning"):
            lines.append(f"- Data limitation: {row['coverage_warning']}")
        lines.extend([
            f"- Suggested next step: {row['suggested_next_step']}",
            "",
        ])
    if report["pricing"].get("model") == "kubernetes_requests":
        pricing_note = (
            "Configured Kubernetes request rates produce allocation estimates, "
            "not provider invoice costs."
        )
    elif report["pricing"].get("cost_model_status") == "unavailable":
        pricing_note = (
            "No Kubernetes request pricing model is configured, so request "
            "allocation savings are not estimated."
        )
    else:
        pricing_note = (
            "Catalog prices are estimates and do not represent provider invoices."
        )
    lines.extend([
        "## Delivery and safety",
        "",
        "This report is a read-only analysis and recommendation; it does not "
        "change infrastructure.",
        pricing_note + " Realized savings remain $0.00 until a human approves, "
        "implements, and measures a change.",
        "",
    ])
    return "\n".join(lines)


def default_recipient() -> str:
    return os.getenv("KYBERNAUT_COST_REPORT_RECIPIENT", DEFAULT_RECIPIENT).strip()


def send_weekly_report(
    report_markdown: str,
    *,
    recipient: str | None = None,
    enabled: bool = False,
    environ: dict[str, str] | None = None,
    smtp_factory=smtplib.SMTP,
) -> dict:
    """Send only when explicitly enabled and complete SMTP configuration exists."""
    env = os.environ if environ is None else environ
    target = (recipient or env.get(
        "KYBERNAUT_COST_REPORT_RECIPIENT", DEFAULT_RECIPIENT
    )).strip()
    if not SAFE_EMAIL.fullmatch(target) or parseaddr(target)[1] != target:
        return {
            "status": "invalid_recipient",
            "recipient": None,
            "sent": False,
            "reason": "recipient_is_not_a_valid_email_address",
        }
    if not enabled:
        return {"status": "preview_only", "recipient": target, "sent": False}
    required = (
        "KYBERNAUT_COST_SMTP_HOST",
        "KYBERNAUT_COST_SMTP_PORT",
        "KYBERNAUT_COST_SMTP_FROM",
        "KYBERNAUT_COST_SMTP_USERNAME",
        "KYBERNAUT_COST_SMTP_PASSWORD",
    )
    missing = [key for key in required if not env.get(key)]
    if missing:
        logger.warning("Weekly report email not sent; SMTP configuration missing")
        return {
            "status": "not_configured",
            "recipient": target,
            "sent": False,
            "reason": "smtp_configuration_missing",
        }
    try:
        port = int(env["KYBERNAUT_COST_SMTP_PORT"])
        message = EmailMessage()
        message["Subject"] = "Kybernaut weekly cost optimization report"
        message["From"] = env["KYBERNAUT_COST_SMTP_FROM"]
        message["To"] = target
        message.set_content(report_markdown)
        with smtp_factory(
            env["KYBERNAUT_COST_SMTP_HOST"], port, timeout=15
        ) as client:
            client.starttls()
            client.login(
                env["KYBERNAUT_COST_SMTP_USERNAME"],
                env["KYBERNAUT_COST_SMTP_PASSWORD"],
            )
            refused = client.send_message(message)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
    except Exception as exc:
        logger.error("Weekly report email delivery failed (%s)", type(exc).__name__)
        return {
            "status": "failed",
            "recipient": target,
            "sent": False,
            "reason": type(exc).__name__,
        }
    logger.info("Weekly report email delivered to configured recipient")
    return {"status": "sent", "recipient": target, "sent": True}


class WeeklyRunLedger:
    """SQLite idempotency ledger keyed by reporting-period boundaries."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS weekly_cost_runs (
                    period_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    reporting_period TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delivery_outcome TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def claim_delivery(
        self, period_key: str, reporting_period: str, run_id: str | None = None
    ) -> bool:
        with sqlite3.connect(self.path, timeout=10) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM weekly_cost_runs WHERE period_key = ?",
                (period_key,),
            ).fetchone()
            if row and row[0] in {"sending", "sent", "delivered"}:
                return False
            connection.execute(
                """INSERT INTO weekly_cost_runs
                   (period_key, run_id, reporting_period, status,
                    delivery_outcome, updated_at)
                   VALUES (?, ?, ?, 'sending', 'pending', ?)
                   ON CONFLICT(period_key) DO UPDATE SET
                     run_id=excluded.run_id, status='sending',
                     delivery_outcome='pending', updated_at=excluded.updated_at""",
                (
                         period_key, run_id or uuid.uuid4().hex, reporting_period,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return True

    def record(
        self, period_key: str, reporting_period: str,
        status: str, delivery_outcome: str, run_id: str | None = None,
    ) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                """INSERT INTO weekly_cost_runs
                   (period_key, run_id, reporting_period, status,
                    delivery_outcome, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(period_key) DO UPDATE SET
                     run_id=excluded.run_id, reporting_period=excluded.reporting_period,
                     status=excluded.status, delivery_outcome=excluded.delivery_outcome,
                     updated_at=excluded.updated_at""",
                (
                         period_key, run_id or uuid.uuid4().hex, reporting_period, status,
                    delivery_outcome, datetime.now(timezone.utc).isoformat(),
                ),
            )


def load_observation_file(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeeklyCostReportError(
            f"Could not read observation input: {type(exc).__name__}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(
        data.get("observations"), list
    ):
        raise WeeklyCostReportError(
            "Observation input must contain an observations array"
        )
    return data
