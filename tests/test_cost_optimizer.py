import json

import pytest

from app.services.cost_optimizer import (
    CostAnalysisError, InstanceType, analyze_resource, build_report,
    load_catalog, render_markdown, recommend_kubernetes_requests,
)

CATALOG = [InstanceType("big", 4, 16, 180.0), InstanceType("mid", 2, 8, 95.0),
           InstanceType("small", 1, 4, 50.0)]


def res(cpu, mem, n=48, itype="big"):
    return {"name": "worker-1", "instance_type": itype,
            "cpu_utilization": [cpu] * n, "memory_utilization": [mem] * n}


def test_underutilized_resource_gets_cheapest_fitting_type():
    r = analyze_resource(res(0.12, 0.24), CATALOG)
    assert r["status"] == "downsize_candidate"
    assert r["recommended_type"] == "mid"   # small cannot hold p95 demand
    assert r["potential_monthly_savings"] == 85.0
    assert r["potential_annual_savings"] == 1020.0
    assert r["realized_savings"] == 0.0


def test_busy_resource_is_left_alone():
    r = analyze_resource(res(0.7, 0.8), CATALOG)
    assert r["status"] == "right_sized"
    assert r["potential_monthly_savings"] == 0.0


def test_thin_data_is_not_recommended():
    r = analyze_resource(res(0.05, 0.05, n=5), CATALOG)
    assert r["status"] == "insufficient_data"


def test_bursty_p95_prevents_downsize():
    cpu = [0.05] * 40 + [0.95] * 8
    r = analyze_resource({"name": "w", "instance_type": "big",
                          "cpu_utilization": cpu,
                          "memory_utilization": [0.2] * 48}, CATALOG)
    assert r["status"] != "downsize_candidate" or \
        r["recommended_type"] != "small"


def test_invalid_inputs_raise():
    with pytest.raises(CostAnalysisError):
        analyze_resource(res(0.1, 0.1, itype="nope"), CATALOG)
    with pytest.raises(CostAnalysisError):
        analyze_resource(res(1.5, 0.1), CATALOG)


@pytest.mark.parametrize("metric", ["cpu_utilization", "memory_utilization"])
@pytest.mark.parametrize("sample", [False, True])
def test_boolean_utilization_samples_are_rejected(metric, sample):
    resource = res(0.12, 0.24)
    resource[metric][0] = sample

    with pytest.raises(CostAnalysisError, match="boolean utilization samples"):
        analyze_resource(resource, CATALOG)


def test_report_totals_and_markdown(tmp_path):
    report = build_report([res(0.12, 0.24), res(0.8, 0.8)], CATALOG,
                          {"example": True, "source": "test"})
    assert report["summary"]["potential_monthly_savings"] == 85.0
    assert report["summary"]["realized_savings"] == 0.0
    md = render_markdown(report)
    assert "EXAMPLE PRICES" in md and "Potential monthly savings" in md


def test_catalog_loader_rejects_bad_prices(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"instance_types": [
        {"name": "x", "vcpu": 0, "memory_gb": 1, "monthly_cost": 1}]}))
    with pytest.raises(CostAnalysisError):
        load_catalog(p)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vcpu", float("nan")),
        ("vcpu", float("inf")),
        ("memory_gb", float("-inf")),
        ("monthly_cost", float("nan")),
        ("monthly_cost", float("inf")),
    ],
)
def test_catalog_loader_rejects_non_finite_values(tmp_path, field, value):
    p = tmp_path / "p.json"
    instance_type = {
        "name": "x",
        "vcpu": 1,
        "memory_gb": 1,
        "monthly_cost": 1,
        field: value,
    }
    p.write_text(json.dumps({"instance_types": [instance_type]}))

    with pytest.raises(CostAnalysisError, match="Invalid values"):
        load_catalog(p)


def test_kubernetes_request_sizing_withholds_zero_usage_recommendations():
    result = recommend_kubernetes_requests(
        name="kybernaut-lab/demo-web",
        cpu_utilization=[0.0] * 24,
        memory_utilization=[0.2] * 24,
        current_cpu_request_cores=0.5,
        current_memory_request_bytes=128 * 1024 ** 2,
    )

    assert result["status"] == "request_adjustment_review"
    assert result["cpu_request_recommended_cores"] is None
    assert result["memory_request_recommended_bytes"] is None
    assert result["potential_monthly_savings"] is None
