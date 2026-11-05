from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from app.services.benchmark_metrics import BenchmarkIncomplete
from scripts import run_synthetic_benchmark as benchmark


@pytest.fixture
def artifact():
    return benchmark.build_synthetic_benchmark(
        run_id="synthetic-test-run",
        now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
    )


def test_synthetic_benchmark_reproduces_classifier_and_policy_results(artifact):
    repeated = benchmark.build_synthetic_benchmark(
        run_id="synthetic-test-run",
        now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert repeated == artifact
    assert artifact["summary"]["status"] == "complete"
    assert artifact["summary"]["classification"] == {
        "correct": 20,
        "denominator": 20,
        "accuracy": 1.0,
        "failed_scenario_ids": [],
    }
    assert artifact["summary"]["policy_contract_match"]["decision_counts"] == {
        "allow": 1,
        "deny": 1,
        "not_applicable": 4,
        "require_approval": 14,
    }
    assert artifact["summary"]["diagnosis_availability"]["status_counts"] == {
        "available": 18,
        "missing_evidence": 1,
        "unavailable": 1,
    }
    assert artifact["metrics"]["detection"] == {
        "true_positives": None,
        "false_negatives": None,
        "false_positives": None,
        "successful_injections": None,
        "failed_injections": None,
        "precision": None,
        "recall": None,
        "f1": None,
        "status": "not_measured",
        "sample_count": 0,
        "excluded_count": 20,
        "caveat": None,
    }
    assert artifact["summary"]["remediation"] == {
        "actions_executed": 0,
        "verified_recoveries": 0,
        "status": "not_exercised",
    }
    assert benchmark.validate_synthetic_artifact(artifact) == artifact["metrics"]


def test_missing_expected_label_is_rejected_instead_of_scored(artifact):
    incomplete = deepcopy(artifact)
    del incomplete["cases"][0]["expected_category"]

    with pytest.raises(BenchmarkIncomplete, match="incorrect expected_category"):
        benchmark.validate_synthetic_artifact(incomplete)


def test_incomplete_or_tampered_result_artifact_is_rejected(artifact):
    incomplete = deepcopy(artifact)
    incomplete["cases"].pop()
    with pytest.raises(BenchmarkIncomplete, match="20 cases"):
        benchmark.validate_synthetic_artifact(incomplete)

    tampered = deepcopy(artifact)
    tampered["metrics"]["classification"]["accuracy"] = 0.0
    with pytest.raises(BenchmarkIncomplete, match="Stored metrics"):
        benchmark.validate_synthetic_artifact(tampered)

    misdeclared = deepcopy(artifact)
    misdeclared["run"]["evaluation_mode"] = "live_lab"
    with pytest.raises(BenchmarkIncomplete, match="synthetic evaluation mode"):
        benchmark.validate_synthetic_artifact(misdeclared)

    fabricated_detection = deepcopy(artifact)
    fabricated_detection["cases"][0]["detection_result"] = "detected"
    with pytest.raises(BenchmarkIncomplete, match="cannot claim alert"):
        benchmark.validate_synthetic_artifact(fabricated_detection)


def test_policy_mismatch_cannot_be_hidden_by_the_summary(artifact):
    altered = deepcopy(artifact)
    case = next(
        item for item in altered["cases"]
        if item["scenario_id"] == "memory-leak-2"
    )
    case["policy_evaluation"]["actual_decision"] = "allow"
    case["policy_evaluation"]["matches_expected"] = False

    summary = benchmark._summary(altered["cases"], altered["metrics"])
    assert summary["all_expectations_met"] is False
    assert summary["policy_contract_match"]["failed_scenario_ids"] == [
        "memory-leak-2"
    ]
    with pytest.raises(BenchmarkIncomplete, match="policy result disagrees"):
        benchmark.validate_synthetic_artifact(altered)


def test_synthetic_benchmark_never_invokes_live_remediation(artifact, monkeypatch):
    from app.services import incident_diagnoser, k8s_remediation

    runner = Mock(side_effect=AssertionError("live runner must not run"))
    http_client = Mock(side_effect=AssertionError("network client must not run"))
    monkeypatch.setattr(k8s_remediation, "default_runner", runner)
    monkeypatch.setattr(
        incident_diagnoser.httpx, "AsyncClient", http_client
    )

    result = benchmark.build_synthetic_benchmark(
        run_id="safe-synthetic-run",
        now=datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert result["evaluation_provenance"]["live_remediation_invoked"] is False
    assert runner.call_count == 0
    assert http_client.call_count == 0
