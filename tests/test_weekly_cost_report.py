import json
import sqlite3
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from app.services.cost_optimizer import InstanceType
from app.services.cost_report_pdf import build_cost_report_pdf
from app.services.kubernetes_cost_model import load_kubernetes_pricing
from app.services import k8s_remediation
from app.services.weekly_cost_report import (
    DEFAULT_RECIPIENT,
    WeeklyCostConfig,
    WeeklyCostReportError,
    WeeklyRunLedger,
    analyze_week,
    default_recipient,
    last_completed_week,
    render_weekly_markdown,
    send_weekly_report,
    synthetic_weekly_observations,
)

START = "2026-10-12T00:00:00+00:00"
END = "2026-10-19T00:00:00+00:00"
CATALOG = [
    InstanceType("example.xlarge", 4, 16, 180.0),
    InstanceType("example.large", 2, 8, 95.0),
    InstanceType("example.medium", 1, 4, 50.0),
]


def report(observations=None):
    if observations is None:
        observations = synthetic_weekly_observations(START, END)
    return analyze_week(
        observations,
        period_start=START,
        period_end=END,
        sample_interval_minutes=60,
        catalog=CATALOG,
        provenance="synthetic test fixture",
        pricing_meta={"source": "example test catalog", "example": True},
    )


def test_weekly_report_has_underuse_pressure_and_evidence():
    result = report()
    rows = {row["resource"]: row for row in result["recommendations"]}

    assert rows["worker-underused"]["status"] == "downsize_candidate"
    assert rows["worker-underused"]["recommended_type"] == "example.large"
    assert rows["worker-underused"]["potential_monthly_savings"] == 85.0
    assert rows["worker-pressure"]["status"] == "capacity_pressure"
    assert rows["worker-pressure"]["cpu_peak"] == 0.9
    assert rows["worker-pressure"]["pressure_sample_ratio"] > 0.1
    assert rows["worker-critical"]["status"] == "human_review_required"
    assert rows["worker-gapped"]["status"] == "incomplete_period"
    assert rows["worker-thin"]["status"] == "insufficient_data"
    assert result["summary"]["potential_monthly_savings"] == 85.0
    assert result["summary"]["realized_savings"] == 0.0
    assert result["pricing"]["is_example"] is True
    assert result["provenance"] == "synthetic test fixture"


def test_peak_guard_blocks_downsize_despite_low_average():
    observations = synthetic_weekly_observations(START, END)
    target = [
        row for row in observations
        if row["resource"] == "worker-underused"
    ]
    for row in target[-2:]:
        row["cpu_utilization"] = 0.95

    result = report(observations)
    candidate = next(
        row for row in result["recommendations"]
        if row["resource"] == "worker-underused"
    )
    assert candidate["cpu_avg"] < 0.3
    assert candidate["cpu_peak"] == 0.95
    assert candidate["status"] == "peak_capacity_risk"
    assert candidate["potential_monthly_savings"] == 0.0


def test_observed_pressure_is_reported_even_with_incomplete_coverage():
    observations = synthetic_weekly_observations(START, END)
    observations = [
        row for row in observations
        if row["resource"] == "worker-pressure"
        and int(row["timestamp"][11:13]) < 4
    ]
    for row in observations:
        row["cpu_utilization"] = 0.95

    result = report(observations)
    pressure = result["recommendations"][0]
    assert pressure["status"] == "capacity_pressure"
    assert pressure["coverage"] < 0.8
    assert pressure["coverage_warning"]


def test_missing_observations_do_not_fabricate_resources_or_usage():
    result = report([])
    assert result["recommendations"] == []
    assert result["summary"]["resources_analyzed"] == 0
    assert result["summary"]["potential_monthly_savings"] == 0.0


def test_invalid_period_values_and_duplicate_samples_fail_clearly():
    with pytest.raises(WeeklyCostReportError, match="timezone"):
        analyze_week(
            [], period_start="2026-10-12", period_end=END,
            sample_interval_minutes=60, catalog=CATALOG, provenance="test",
        )
    observations = synthetic_weekly_observations(START, END)
    observations.append(observations[0].copy())
    with pytest.raises(
        WeeklyCostReportError,
        match="multiple samples in a resource reporting interval",
    ):
        report(observations)


def test_resource_metadata_must_remain_stable_over_reporting_period():
    observations = synthetic_weekly_observations(START, END)
    observations[1]["instance_type"] = "example.large"
    with pytest.raises(WeeklyCostReportError, match="metadata changed"):
        report(observations)


def test_sensitive_provenance_is_rejected_before_report_generation():
    with pytest.raises(WeeklyCostReportError, match="non-secret label"):
        analyze_week(
            [], period_start=START, period_end=END,
            sample_interval_minutes=60, catalog=CATALOG,
            provenance="source=foo api_key=do-not-report",
        )


def test_thresholds_are_configurable_from_environment(monkeypatch):
    monkeypatch.setenv("KYBERNAUT_COST_PRESSURE_THRESHOLD", "0.9")
    monkeypatch.setenv("KYBERNAUT_COST_MINIMUM_COVERAGE", "0.95")

    config = WeeklyCostConfig.from_env()

    assert config.pressure_threshold == 0.9
    assert config.minimum_coverage == 0.95


def test_analysis_never_invokes_infrastructure_action_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(
        k8s_remediation,
        "default_runner",
        lambda command: calls.append(command),
    )

    result = report()

    assert result["summary"]["resources_analyzed"] == 5
    assert calls == []


def test_markdown_contains_provenance_period_and_safety_disclosures():
    result = report()
    content = render_weekly_markdown(result)

    assert "2026-10-12" in content
    assert "synthetic test fixture" in content
    assert "EXAMPLE pricing" in content
    assert "worker-pressure" in content
    assert "read-only analysis" in content
    assert "Realized savings remain $0.00" in content


def test_kubernetes_request_sizing_does_not_invent_cost_savings():
    observations = []
    for index in range(24):
        observations.append({
            "timestamp": f"2026-10-12T{index:02d}:00:00+00:00",
            "resource": "kybernaut-lab/demo-web",
            "service": "demo-web",
            "instance_type": "kubernetes-request",
            "cpu_utilization": 0.1,
            "memory_utilization": 0.2,
        })
    resources = {
        "kybernaut-lab/demo-web": {
            "namespace": "kybernaut-lab",
            "deployment": "demo-web",
            "replicas": 1,
            "cpu_request_cores": 1.0,
            "memory_request_bytes": 1024 ** 3,
            "cpu_limit_cores": 2.0,
            "memory_limit_bytes": 2 * 1024 ** 3,
        },
    }

    result = analyze_week(
        observations,
        period_start=START,
        period_end="2026-10-13T00:00:00+00:00",
        sample_interval_minutes=60,
        catalog=[],
        provenance="live mocked Kubernetes request data",
        kubernetes_resources=resources,
    )
    row = result["recommendations"][0]

    assert row["status"] == "request_downsize_candidate"
    assert row["cpu_request_recommended_cores"] == pytest.approx(0.167)
    assert row["memory_request_recommended_bytes"] < 1024 ** 3
    assert row["potential_monthly_savings"] is None
    assert result["summary"]["potential_monthly_savings"] is None
    assert result["summary"]["estimated_annual_savings"] is None
    assert result["pricing"]["cost_model_status"] == "unavailable"
    markdown = render_weekly_markdown(result)
    assert "not estimated" in markdown
    pdf = build_cost_report_pdf(result)
    assert b"Proposed aggregate requests" in pdf
    assert b"not estimated" in pdf
    assert b"Monetary savings are not estimated" in pdf


def test_kubernetes_costs_use_explicit_request_pricing_and_period_hours(tmp_path):
    pricing_path = tmp_path / "kubernetes-pricing.json"
    pricing_path.write_text(json.dumps({
        "model": "kubernetes_requests",
        "currency": "USD",
        "source": "approved test rate card",
        "effective_date": "2026-01-01",
        "provider": "test-provider",
        "region": "test-region",
        "cpu_per_core_hour": 2.0,
        "memory_per_gib_hour": 1.0,
        "workload_adjustments_per_hour": {"demo-web": 0.5},
        "example": False,
    }), encoding="utf-8")
    observations = [{
        "timestamp": f"2026-10-12T{index:02d}:00:00+00:00",
        "resource": "kybernaut-lab/demo-web",
        "service": "demo-web",
        "instance_type": "kubernetes-request",
        "cpu_utilization": 0.1,
        "memory_utilization": 0.2,
    } for index in range(24)]
    resources = {
        "kybernaut-lab/demo-web": {
            "namespace": "kybernaut-lab",
            "deployment": "demo-web",
            "replicas": 1,
            "cpu_request_cores": 1.0,
            "memory_request_bytes": 1024 ** 3,
            "cpu_limit_cores": 2.0,
            "memory_limit_bytes": 2 * 1024 ** 3,
            "containers": [{
                "pod": "demo-web-abc", "container": "web",
                "cpu_request_cores": 1.0,
                "memory_request_bytes": 1024 ** 3,
            }],
        },
    }

    result = analyze_week(
        observations,
        period_start=START,
        period_end="2026-10-13T00:00:00+00:00",
        sample_interval_minutes=60,
        catalog=[],
        provenance="live mocked Kubernetes request data",
        kubernetes_resources=resources,
        kubernetes_pricing=load_kubernetes_pricing(pricing_path),
    )
    row = result["recommendations"][0]

    assert row["current_estimated_request_cost"]["total"] == 84.0
    assert row["current_estimated_request_cost"]["currency"] == "USD"
    assert row["proposed_estimated_request_cost"]["total"] < 84.0
    assert row["request_cost_savings"]["potential_savings"] > 0
    assert row["request_cost_savings"]["potential_savings_percent"] > 0
    assert result["summary"]["potential_request_cost_savings"] > 0
    assert result["summary"]["request_cost_savings_currency"] == "USD"
    assert result["summary"]["potential_monthly_savings"] is None
    assert row["container_identity"][0]["container"] == "web"
    markdown = render_weekly_markdown(result)
    assert "Configured Kubernetes request rates" in markdown
    assert "not provider invoice costs" in markdown
    assert "pricing unavailable" not in markdown.lower()
    pdf = build_cost_report_pdf(result)
    assert b"Potential estimated request-allocation saving" in pdf
    assert b"not invoice cost; not realized savings" in pdf
    assert b"Monetary savings are not estimated" not in pdf


def test_future_effective_pricing_cannot_estimate_historical_period():
    from app.services.kubernetes_cost_model import KubernetesPricing
    from datetime import date

    pricing = KubernetesPricing(
        cpu_per_core_hour=1,
        memory_per_gib_hour=1,
        currency="USD",
        source="future rate card",
        effective_date=date(2026, 10, 13),
    )
    with pytest.raises(WeeklyCostReportError, match="not effective"):
        analyze_week(
            [],
            period_start=START,
            period_end=END,
            sample_interval_minutes=60,
            catalog=[],
            provenance="test",
            kubernetes_resources={},
            kubernetes_pricing=pricing,
        )


def test_kubernetes_utilization_above_request_is_preserved_as_pressure():
    observations = [{
        "timestamp": f"2026-10-12T{index:02d}:00:00+00:00",
        "resource": "kybernaut-lab/demo-web",
        "service": "demo-web",
        "instance_type": "kubernetes-request",
        "cpu_utilization": 1.2,
        "memory_utilization": 0.2,
    } for index in range(24)]
    resources = {
        "kybernaut-lab/demo-web": {
            "namespace": "kybernaut-lab",
            "deployment": "demo-web",
            "replicas": 1,
            "cpu_request_cores": 1.0,
            "memory_request_bytes": 1024 ** 3,
            "cpu_limit_cores": None,
            "memory_limit_bytes": None,
        },
    }

    result = analyze_week(
        observations,
        period_start=START,
        period_end="2026-10-13T00:00:00+00:00",
        sample_interval_minutes=60,
        catalog=[],
        provenance="live mocked Kubernetes request data",
        kubernetes_resources=resources,
    )

    assert result["recommendations"][0]["cpu_peak"] == 1.2
    assert result["recommendations"][0]["status"] == "capacity_pressure"
    assert result["summary"]["potential_monthly_savings"] is None


def test_kubernetes_workload_without_complete_samples_is_reported():
    resources = {
        "kybernaut-lab/demo-web": {
            "namespace": "kybernaut-lab",
            "deployment": "demo-web",
            "replicas": 1,
            "cpu_request_cores": 1.0,
            "memory_request_bytes": 1024 ** 3,
            "cpu_limit_cores": None,
            "memory_limit_bytes": None,
        },
    }

    result = analyze_week(
        [],
        period_start=START,
        period_end="2026-10-13T00:00:00+00:00",
        sample_interval_minutes=60,
        catalog=[],
        provenance="live mocked Kubernetes request data",
        kubernetes_resources=resources,
    )

    row = result["recommendations"][0]
    assert row["resource"] == "kybernaut-lab/demo-web"
    assert row["status"] == "incomplete_period"
    assert row["sample_count"] == 0
    assert row["coverage"] == 0.0
    assert row["potential_monthly_savings"] is None


def test_recipient_default_and_environment_override(monkeypatch):
    monkeypatch.delenv("KYBERNAUT_COST_REPORT_RECIPIENT", raising=False)
    assert default_recipient() == DEFAULT_RECIPIENT == "jananiviswa05@gmail.com"
    monkeypatch.setenv(
        "KYBERNAUT_COST_REPORT_RECIPIENT", "finops@example.test"
    )
    assert default_recipient() == "finops@example.test"


def test_invalid_recipient_does_not_attempt_delivery():
    calls = []
    result = send_weekly_report(
        "weekly summary",
        recipient="not an email",
        enabled=True,
        environ={
            "KYBERNAUT_COST_SMTP_HOST": "smtp.example.test",
            "KYBERNAUT_COST_SMTP_PORT": "587",
            "KYBERNAUT_COST_SMTP_FROM": "reports@example.test",
            "KYBERNAUT_COST_SMTP_USERNAME": "user",
            "KYBERNAUT_COST_SMTP_PASSWORD": "secret",
        },
        smtp_factory=lambda *_args, **_kwargs: calls.append(True),
    )
    assert result["status"] == "invalid_recipient"
    assert calls == []


def test_email_is_preview_only_by_default_and_does_not_open_smtp():
    calls = []
    result = send_weekly_report(
        "weekly summary",
        environ={},
        smtp_factory=lambda *_args, **_kwargs: calls.append(True),
    )

    assert result == {
        "status": "preview_only",
        "recipient": DEFAULT_RECIPIENT,
        "sent": False,
    }
    assert calls == []


def test_enabled_delivery_without_smtp_configuration_is_safe():
    result = send_weekly_report(
        "weekly summary", enabled=True, environ={}
    )

    assert result["status"] == "not_configured"
    assert result["sent"] is False
    assert result["reason"] == "smtp_configuration_missing"


def test_smtp_sender_failure_is_reported_without_leaking_secret(caplog):
    password = "very-private-password"

    class FailingSMTP:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            raise RuntimeError(password)

    config = {
        "KYBERNAUT_COST_SMTP_HOST": "smtp.example.test",
        "KYBERNAUT_COST_SMTP_PORT": "587",
        "KYBERNAUT_COST_SMTP_FROM": "reports@example.test",
        "KYBERNAUT_COST_SMTP_USERNAME": "user",
        "KYBERNAUT_COST_SMTP_PASSWORD": password,
    }
    result = send_weekly_report(
        "weekly summary",
        enabled=True,
        environ=config,
        smtp_factory=FailingSMTP,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "RuntimeError"
    assert password not in caplog.text


def test_successful_mocked_smtp_uses_configured_recipient_and_content():
    sent = []

    class MockSMTP:
        def __init__(self, host, port, timeout):
            assert host == "smtp.example.test"
            assert port == 587
            assert timeout == 15

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            pass

        def login(self, username, password):
            assert username == "report-user"
            assert password == "mock-password"

        def send_message(self, message: EmailMessage):
            sent.append(message)

    config = {
        "KYBERNAUT_COST_SMTP_HOST": "smtp.example.test",
        "KYBERNAUT_COST_SMTP_PORT": "587",
        "KYBERNAUT_COST_SMTP_FROM": "reports@example.test",
        "KYBERNAUT_COST_SMTP_USERNAME": "report-user",
        "KYBERNAUT_COST_SMTP_PASSWORD": "mock-password",
        "KYBERNAUT_COST_REPORT_RECIPIENT": "recipient@example.test",
    }
    result = send_weekly_report(
        "weekly body", enabled=True, environ=config, smtp_factory=MockSMTP
    )

    assert result["status"] == "sent"
    assert result["recipient"] == "recipient@example.test"
    assert len(sent) == 1
    assert sent[0]["To"] == "recipient@example.test"
    assert sent[0].get_content().strip() == "weekly body"


def test_run_ledger_suppresses_duplicate_delivery_and_allows_retry_after_failure(
    tmp_path,
):
    ledger = WeeklyRunLedger(tmp_path / "runs.sqlite3")
    period = "2026-10-12/2026-10-19"
    assert ledger.claim_delivery(period, "week", "run-1")
    ledger.record(period, "week", "delivered", "sent", "run-1")
    assert not ledger.claim_delivery(period, "week")
    with sqlite3.connect(ledger.path) as connection:
        stored = connection.execute(
            "SELECT run_id, reporting_period, status, delivery_outcome "
            "FROM weekly_cost_runs WHERE period_key=?",
            (period,),
        ).fetchone()
    assert stored == ("run-1", "week", "delivered", "sent")

    failed_period = "2026-10-19/2026-10-26"
    assert ledger.claim_delivery(failed_period, "next week")
    ledger.record(failed_period, "next week", "failed", "failed")
    assert ledger.claim_delivery(failed_period, "next week")


def test_last_completed_week_uses_previous_monday_boundaries_in_utc():
    start, end = last_completed_week(
        "UTC", now=datetime(2026, 10, 19, 9, tzinfo=timezone.utc)
    )

    assert start == "2026-10-12T00:00:00+00:00"
    assert end == "2026-10-19T00:00:00+00:00"


def test_last_completed_week_preserves_timezone_and_dst_boundary():
    start, end = last_completed_week(
        "America/New_York",
        now=datetime.fromisoformat("2026-03-10T09:00:00-04:00"),
    )

    assert start == "2026-03-02T00:00:00-05:00"
    assert end == "2026-03-09T00:00:00-04:00"


def test_last_completed_week_rejects_unknown_timezone_and_naive_now():
    with pytest.raises(WeeklyCostReportError, match="unknown reporting timezone"):
        last_completed_week("Not/A_Zone")
    with pytest.raises(WeeklyCostReportError, match="now must include a timezone"):
        last_completed_week("UTC", now=datetime(2026, 10, 19, 9))


def test_interrupted_sending_run_remains_suppressed_for_operator_inspection(
    tmp_path,
):
    ledger = WeeklyRunLedger(tmp_path / "runs.sqlite3")
    period = "2026-10-12/2026-10-19"

    assert ledger.claim_delivery(period, "week", "interrupted-run")
    assert not ledger.claim_delivery(period, "week", "retry-run")


def test_preview_run_does_not_block_later_explicit_send_but_sent_run_does(
    tmp_path,
):
    ledger = WeeklyRunLedger(tmp_path / "runs.sqlite3")
    period = "2026-10-12/2026-10-19"

    ledger.record(period, "week", "completed", "preview_only", "preview-run")
    assert ledger.claim_delivery(period, "week", "send-run")
    ledger.record(period, "week", "delivered", "sent", "send-run")
    assert not ledger.claim_delivery(period, "week", "duplicate-run")


def test_cron_fragment_uses_explicit_utc_and_keeps_email_opt_in():
    cron = (
        Path(__file__).parents[1]
        / "deploy"
        / "cron"
        / "kybernaut-weekly-cost.cron"
    ).read_text(encoding="utf-8")
    schedule = next(line for line in cron.splitlines() if line.startswith("0 9"))

    assert "CRON_TZ=UTC" in cron
    assert schedule.split()[:5] == ["0", "9", "*", "*", "1"]
    assert "--last-completed-week --timezone UTC" in schedule
    assert "--send-email" not in schedule
