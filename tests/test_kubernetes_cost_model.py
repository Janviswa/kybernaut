import json

import pytest

from app.services.kubernetes_cost_model import (
    KubernetesPricingError,
    estimate_request_cost,
    load_kubernetes_pricing,
    savings_between,
)


def pricing_data(**updates):
    value = {
        "model": "kubernetes_requests",
        "currency": "USD",
        "source": "operator-approved example rate card",
        "effective_date": "2026-01-01",
        "provider": "example-provider",
        "region": "example-region",
        "cpu_per_core_hour": 2.0,
        "memory_per_gib_hour": 1.0,
        "workload_adjustments_per_hour": {"demo-web": 0.5},
        "example": False,
    }
    value.update(updates)
    return value


def write_pricing(tmp_path, **updates):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(pricing_data(**updates)), encoding="utf-8")
    return path


def test_cpu_memory_cost_and_explicit_hourly_adjustment(tmp_path):
    pricing = load_kubernetes_pricing(write_pricing(tmp_path))

    estimate = estimate_request_cost(
        cpu_cores=0.5,
        memory_bytes=1024 ** 3,
        hours=24,
        pricing=pricing,
        workload="demo-web",
    )

    assert estimate["cpu_cost"] == 24.0
    assert estimate["memory_cost"] == 24.0
    assert estimate["adjustment_cost"] == 12.0
    assert estimate["total"] == 60.0
    assert estimate["currency"] == "USD"
    assert estimate["billing_basis"].endswith("not invoice cost")
    assert pricing.report_metadata()["provider"] == "example-provider"


def test_savings_percentage_requires_positive_current_cost(tmp_path):
    pricing = load_kubernetes_pricing(write_pricing(
        tmp_path,
        cpu_per_core_hour=0,
        memory_per_gib_hour=0,
        workload_adjustments_per_hour={},
    ))
    current = estimate_request_cost(
        cpu_cores=1,
        memory_bytes=1024 ** 3,
        hours=24,
        pricing=pricing,
        workload="demo-web",
    )
    proposed = estimate_request_cost(
        cpu_cores=0.5,
        memory_bytes=512 * 1024 ** 2,
        hours=24,
        pricing=pricing,
        workload="demo-web",
    )

    assert savings_between(current, proposed)["potential_savings"] == 0
    assert savings_between(current, proposed)["potential_savings_percent"] is None
    assert savings_between(current, proposed)["realized_savings"] == 0


def test_vm_catalog_is_not_accepted_as_kubernetes_pricing(tmp_path):
    path = tmp_path / "vm-pricing.json"
    path.write_text(json.dumps({
        "example": True,
        "instance_types": [{
            "name": "example.large",
            "vcpu": 2,
            "memory_gb": 8,
            "monthly_cost": 95,
        }],
    }), encoding="utf-8")

    with pytest.raises(KubernetesPricingError, match="VM catalogs are incompatible"):
        load_kubernetes_pricing(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"cpu_per_core_hour": None},
        {"memory_per_gib_hour": -1},
        {"currency": "US"},
        {"effective_date": "not-a-date"},
        {"workload_adjustments_per_hour": {"bad workload": 1}},
    ],
)
def test_invalid_or_missing_pricing_inputs_fail_closed(tmp_path, changes):
    with pytest.raises(KubernetesPricingError):
        load_kubernetes_pricing(write_pricing(tmp_path, **changes))


def test_invalid_period_quantities_do_not_produce_estimates(tmp_path):
    pricing = load_kubernetes_pricing(write_pricing(tmp_path))
    with pytest.raises(KubernetesPricingError, match="finite and non-negative"):
        estimate_request_cost(
            cpu_cores=float("nan"),
            memory_bytes=1024,
            hours=1,
            pricing=pricing,
            workload="demo-web",
        )
