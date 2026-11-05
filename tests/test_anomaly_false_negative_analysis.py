import json
from copy import deepcopy

import pytest

from scripts import analyze_anomaly_false_negatives as analysis


def test_confusion_f1_is_zero_when_both_classes_are_missed():
    samples = [
        {
            "status": "evaluated",
            "ground_truth_anomaly": True,
            "predicted_anomaly": False,
        },
        {
            "status": "evaluated",
            "ground_truth_anomaly": False,
            "predicted_anomaly": True,
        },
    ]
    metrics = analysis._confusion(samples)
    assert metrics["precision"] == 0.0
    assert metrics["recall"] == 0.0
    assert metrics["f1"] == 0.0


@pytest.fixture(scope="module")
def result():
    return analysis.build_analysis()


def test_f28_false_negatives_are_recomputed_and_grouped(result):
    assert result["f28_integrity"]["confusion_consistent"] is True
    assert result["f28_integrity"]["false_negative_count"] == 28
    assert result["false_negative_analysis"]["by_scenario"] == {
        "gradual_cpu_drift": 24,
        "sudden_down_request_rate": 4,
    }
    assert result["false_negative_analysis"][
        "by_highest_deviation_feature"
    ] == {
        "cpu_cores_used": 24,
        "request_rate": 4,
    }
    assert result["false_negative_analysis"]["by_direction"] == {
        "HIGH": 24,
        "LOW": 4,
    }
    drift = result["false_negative_analysis"]["missed_episode_durations"][0]
    assert drift["missed_sample_span_seconds"] == 150.0
    assert drift["positive_episode_duration_seconds"] == 300.0
    assert drift["onset_to_first_detection_seconds"] == 180.0


def test_holdout_is_deterministic_frozen_and_episode_disjoint(result):
    assert analysis.build_analysis() == result
    split = result["split"]
    assert split["episode_ids_disjoint"] is True
    assert split["scenario_families_disjoint"] is True
    assert split["threshold_frozen_before_holdout"] is True
    assert split["candidate_changes_evaluated"] == []
    assert result["detector"]["threshold"] == 5.0
    assert result["detector"]["threshold_changed"] is False
    assert result["holdout"]["confusion"]["sample_count"] == 88
    assert result["holdout"]["confusion"]["tp"] + (
        result["holdout"]["confusion"]["fp"]
        + result["holdout"]["confusion"]["tn"]
        + result["holdout"]["confusion"]["fn"]
    ) == result["holdout"]["confusion"]["sample_count"]
    assert analysis.validate_analysis_artifact(result) == (
        result["holdout"]["confusion"]
    )


def test_validator_rejects_split_leakage_and_tampered_metrics(result):
    split_leak = deepcopy(result)
    split_leak["split"]["holdout_episode_ids"].append(
        split_leak["split"]["development_episode_ids"][0]
    )
    with pytest.raises(ValueError, match="split integrity"):
        analysis.validate_analysis_artifact(split_leak)

    metric_tamper = deepcopy(result)
    metric_tamper["holdout"]["confusion"]["fn"] += 1
    with pytest.raises(ValueError, match="do not recompute"):
        analysis.validate_analysis_artifact(metric_tamper)


def test_f28_input_remains_unchanged_and_invalid_metrics_fail_closed(
    tmp_path,
):
    artifact_path = tmp_path / "f28.json"
    artifact = json.loads(analysis.F28_ARTIFACT.read_text(encoding="utf-8"))
    original = deepcopy(artifact)
    artifact["metrics"]["point_anomaly"]["false_negatives"] += 1
    artifact_path.write_text(json.dumps(artifact), encoding="utf-8")

    with pytest.raises(ValueError, match="Stored metrics do not match"):
        analysis.build_analysis(artifact_path)

    assert json.loads(
        analysis.F28_ARTIFACT.read_text(encoding="utf-8")
    ) == original


def test_holdout_policy_is_read_only_and_does_not_call_remediation(
    monkeypatch,
):
    from app.services import k8s_remediation
    from app.services import anomaly_runtime

    def fail_if_called(*args, **kwargs):
        raise AssertionError("live integration must not be used")

    monkeypatch.setattr(k8s_remediation, "default_runner", fail_if_called)
    monkeypatch.setattr(
        anomaly_runtime.httpx,
        "AsyncClient",
        fail_if_called,
    )
    outcome = analysis.build_analysis()
    assert outcome["decision"]["production_detector_changed"] is False


def test_cli_refuses_to_overwrite_an_existing_artifact(tmp_path, monkeypatch):
    output = tmp_path / "existing.json"
    output.write_text("preserve this\n", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        ["analyze_anomaly_false_negatives", "--output", str(output)],
    )

    assert analysis.main() == 2
    assert output.read_text(encoding="utf-8") == "preserve this\n"
