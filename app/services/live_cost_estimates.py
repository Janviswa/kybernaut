"""Illustrative USD estimates for current Prometheus service usage."""

from __future__ import annotations

import math
import os

from app.services.prometheus_features import EXPECTED_SERVICES

RATE_DEFAULTS_USD = {
    "KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD": 10.00,
    "KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD": 5.25,
}
ILLUSTRATIVE_REDUCTION_PERCENT = 10


class CostPricingConfigurationError(ValueError):
    def __init__(self, variable: str):
        self.variable = variable
        super().__init__(variable)


def _configured_rate(variable: str) -> float | None:
    raw = os.getenv(variable)
    if raw is None or not raw.strip():
        if os.getenv("KYBERNAUT_ENVIRONMENT", "").strip().lower() == "local-demo":
            return RATE_DEFAULTS_USD[variable]
        return None
    try:
        rate = float(raw)
    except ValueError as exc:
        raise CostPricingConfigurationError(variable) from exc
    if not math.isfinite(rate) or rate < 0:
        raise CostPricingConfigurationError(variable)
    return rate


def build_live_cost_estimate(features: dict) -> dict:
    cpu_rate = _configured_rate("KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD")
    memory_rate = _configured_rate("KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD")
    rates_configured = cpu_rate is not None and memory_rate is not None
    local_demo_defaults_used = (
        os.getenv("KYBERNAUT_ENVIRONMENT", "").strip().lower() == "local-demo"
        and any(not os.getenv(name, "").strip() for name in RATE_DEFAULTS_USD)
    )

    services: dict[str, dict | None] = {}
    for service in EXPECTED_SERVICES:
        usage = features.get(service)
        if not isinstance(usage, dict):
            services[service] = None
            continue
        item = dict(usage)
        hourly = monthly = None
        cpu_monthly = memory_monthly = None
        cpu_reduction = memory_reduction = None
        if rates_configured:
            cpu_hourly = item["cpu_cores_used"] * cpu_rate
            memory_hourly = item["memory_bytes"] / (1024 ** 3) * memory_rate
            hourly = cpu_hourly + memory_hourly
            monthly = hourly * 730
            cpu_monthly = round(cpu_hourly * 730, 2)
            memory_monthly = round(memory_hourly * 730, 2)
            cpu_reduction = round(
                cpu_monthly * ILLUSTRATIVE_REDUCTION_PERCENT / 100, 2
            )
            memory_reduction = round(
                memory_monthly * ILLUSTRATIVE_REDUCTION_PERCENT / 100, 2
            )
            hourly = round(hourly, 8)
            monthly = round(monthly, 2)
        item["estimated_cpu_monthly_cost_usd"] = cpu_monthly
        item["estimated_memory_monthly_cost_usd"] = memory_monthly
        item["illustrative_10_percent_cpu_reduction_monthly_savings_usd"] = cpu_reduction
        item["illustrative_10_percent_memory_reduction_monthly_savings_usd"] = memory_reduction
        item["estimated_hourly_cost_usd"] = hourly
        item["estimated_monthly_run_rate_usd"] = monthly
        services[service] = item

    all_services_available = all(
        services[service] is not None for service in EXPECTED_SERVICES
    )
    total_hourly = total_monthly = None
    if rates_configured and all_services_available:
        total_hourly = round(sum(
            float(services[service]["estimated_hourly_cost_usd"])
            for service in EXPECTED_SERVICES
        ), 8)
        total_monthly = round(total_hourly * 730, 2)

    status = (
        "rates_not_configured" if not rates_configured
        else "estimated" if all_services_available
        else "usage_incomplete"
    )
    return {
        "source": "prometheus",
        "currency": "USD",
        "pricing": {
            "configured": rates_configured,
            "source": (
                "illustrative owner-configured internal rates; "
                "not actual cloud-provider costs"
            ),
            "cpu_per_core_hour_usd": cpu_rate,
            "memory_per_gib_hour_usd": memory_rate,
            "configuration": list(RATE_DEFAULTS_USD),
            "local_demo_defaults_used": local_demo_defaults_used,
        },
        "status": status,
        "savings_scenario": {
            "reduction_percent": ILLUSTRATIVE_REDUCTION_PERCENT,
            "basis": (
                "hypothetical reduction in current measured process usage; "
                "not a recommendation or guaranteed saving"
            ),
        },
        "services": services,
        "summary": {
            "estimated_hourly_cost_usd": total_hourly,
            "estimated_monthly_run_rate_usd": total_monthly,
            "month_hours": 730,
            "all_services_available": all_services_available,
        },
        "unavailable_services": sorted(
            service for service in EXPECTED_SERVICES if services[service] is None
        ),
        "disclaimer": (
            "Illustrative estimates using owner-configured internal rates, "
            "not actual cloud-provider costs. Point-in-time process usage "
            "extrapolated to 730 hours; not a bill. Excludes host overhead "
            "and other workloads."
        ),
    }
