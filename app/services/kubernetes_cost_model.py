"""Explicit hourly pricing for Kubernetes CPU and memory requests."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

GIB = 1024 ** 3
CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/()=-]{0,199}$")


class KubernetesPricingError(ValueError):
    pass


@dataclass(frozen=True)
class KubernetesPricing:
    cpu_per_core_hour: float
    memory_per_gib_hour: float
    currency: str
    source: str
    effective_date: date
    provider: str | None = None
    region: str | None = None
    workload_adjustments_per_hour: dict[str, float] | None = None
    example: bool = False

    def report_metadata(self) -> dict[str, Any]:
        return {
            "model": "kubernetes_requests",
            "currency": self.currency,
            "source": self.source,
            "effective_date": self.effective_date.isoformat(),
            "provider": self.provider,
            "region": self.region,
            "example": self.example,
            "cpu_price_per_core_hour": self.cpu_per_core_hour,
            "memory_price_per_gib_hour": self.memory_per_gib_hour,
            "adjustments_per_hour": self.workload_adjustments_per_hour or {},
            "invoice_cost": False,
        }


def _price(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KubernetesPricingError(f"{field} must be a non-negative number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise KubernetesPricingError(f"{field} must be a non-negative number")
    return result


def load_kubernetes_pricing(path: str | Path) -> KubernetesPricing:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KubernetesPricingError(
            "Kubernetes pricing file is unreadable"
        ) from exc
    if not isinstance(data, dict) or data.get("model") != "kubernetes_requests":
        raise KubernetesPricingError(
            "Pricing model must be 'kubernetes_requests'; VM catalogs are incompatible"
        )
    currency = data.get("currency")
    source = data.get("source")
    if not isinstance(currency, str) or not CURRENCY_CODE.fullmatch(currency):
        raise KubernetesPricingError("currency must be an uppercase ISO code")
    if not isinstance(source, str) or not SAFE_LABEL.fullmatch(source.strip()):
        raise KubernetesPricingError("pricing source must be a non-empty label")
    try:
        effective = date.fromisoformat(data["effective_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise KubernetesPricingError(
            "effective_date must be an ISO calendar date"
        ) from exc
    adjustments = data.get("workload_adjustments_per_hour", {})
    if not isinstance(adjustments, dict):
        raise KubernetesPricingError(
            "workload_adjustments_per_hour must be an object"
        )
    checked_adjustments = {}
    for workload, amount in adjustments.items():
        if (
            not isinstance(workload, str)
            or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?", workload)
        ):
            raise KubernetesPricingError("workload adjustment name is invalid")
        checked_adjustments[workload] = _price(
            amount, f"adjustment for {workload}"
        )
    for optional in ("provider", "region"):
        if data.get(optional) is not None and (
            not isinstance(data[optional], str)
            or not SAFE_LABEL.fullmatch(data[optional].strip())
        ):
            raise KubernetesPricingError(f"{optional} must be a non-empty label")
    example = data.get("example", False)
    if not isinstance(example, bool):
        raise KubernetesPricingError("example must be a boolean")
    return KubernetesPricing(
        cpu_per_core_hour=_price(data.get("cpu_per_core_hour"), "cpu_per_core_hour"),
        memory_per_gib_hour=_price(
            data.get("memory_per_gib_hour"), "memory_per_gib_hour"
        ),
        currency=currency,
        source=source.strip(),
        effective_date=effective,
        provider=data.get("provider"),
        region=data.get("region"),
        workload_adjustments_per_hour=checked_adjustments,
        example=example,
    )


def estimate_request_cost(
    *,
    cpu_cores: float,
    memory_bytes: float,
    hours: float,
    pricing: KubernetesPricing,
    workload: str,
) -> dict[str, Any]:
    values = (cpu_cores, memory_bytes, hours)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in values
    ):
        raise KubernetesPricingError(
            "request quantities and period hours must be finite and non-negative"
        )
    cpu_cost = cpu_cores * pricing.cpu_per_core_hour * hours
    memory_cost = memory_bytes / GIB * pricing.memory_per_gib_hour * hours
    adjustment_rate = (pricing.workload_adjustments_per_hour or {}).get(
        workload, 0.0
    )
    adjustment = adjustment_rate * hours
    total = cpu_cost + memory_cost + adjustment
    if not math.isfinite(total):
        raise KubernetesPricingError("estimated request cost is not finite")
    return {
        "currency": pricing.currency,
        "cpu_cost": round(cpu_cost, 8),
        "memory_cost": round(memory_cost, 8),
        "adjustment_cost": round(adjustment, 8),
        "total": round(total, 8),
        "period_hours": hours,
        "billing_basis": "requested resource allocation estimate; not invoice cost",
    }


def savings_between(
    current: dict[str, Any], proposed: dict[str, Any]
) -> dict[str, Any]:
    if current["currency"] != proposed["currency"]:
        raise KubernetesPricingError("cost estimates use incompatible currencies")
    baseline = current["total"]
    savings = baseline - proposed["total"]
    return {
        "currency": current["currency"],
        "potential_savings": round(savings, 8),
        "potential_savings_percent": (
            round(savings / baseline * 100, 4) if baseline > 0 else None
        ),
        "realized_savings": 0.0,
    }
