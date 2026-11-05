import csv
from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock

import pytest

from app.services.anomaly_detector import FEATURE_NAMES
from scripts import evaluate_anomaly_benchmark as benchmark


@pytest.fixture(scope="module")
def artifact():
    return benchmark.build_anomaly_benchmark()


@pytest.fixture(scope="module")
def comparative_artifact():
    return benchmark.build_comparative_anomaly_benchmark()


def test_benchmark_repeats_deterministically_and_uses_active_runtime_policy(
    artifact,
):
    assert benchmark.build_anomaly_benchmark() == artifact
    assert artifact["run"]["evaluation_mode"] == "synthetic"
    assert artifact["detector"]["identity"].endswith(
        "DirectionAwareAnomalyPolicy"
    )
    assert artifact["detector"]["policy_threshold"] == 5.0
    assert artifact["detector"]["isolation_forest_integrated_in_runtime"] is False
    assert artifact["metrics"]["point_anomaly"] == {
        "status": "measured",
        "true_positives": 40,
        "false_positives": 4,
        "true_negatives": 104,
        "false_negatives": 28,
        "precision": 0.909091,
        "recall": 0.588235,
        "f1": 0.714286,
        "false_positive_rate": 0.037037,
        "sample_count": 176,
        "excluded_count": 8,
    }
    drift = artifact["metrics"]["gradual_drift_detection_delay"]
    assert drift["episode_count"] == 4
    assert drift["detected_episode_count"] == 4
    assert drift["mean_seconds"] == 180.0


def test_confusion_metrics_handle_empty_and_single_class_data(artifact):
    assert benchmark._confusion([], "predicted_anomaly") == {
        "status": "not_measured",
        "true_positives": None,
        "false_positives": None,
        "true_negatives": None,
        "false_negatives": None,
        "precision": None,
        "recall": None,
        "f1": None,
        "false_positive_rate": None,
        "sample_count": 0,
        "excluded_count": 0,
    }
    normal = [
        sample for sample in artifact["samples"]
        if sample["status"] == "evaluated"
        and sample["ground_truth_anomaly"] is False
    ]
    normal_metrics = benchmark._confusion(normal, "predicted_anomaly")
    assert normal_metrics["recall"] is None
    assert normal_metrics["false_positive_rate"] is not None

    positive = [
        sample for sample in artifact["samples"]
        if sample["status"] == "evaluated"
        and sample["ground_truth_anomaly"] is True
    ]
    positive_metrics = benchmark._confusion(positive, "predicted_anomaly")
    assert positive_metrics["false_positive_rate"] is None
    assert positive_metrics["recall"] is not None


def test_missing_and_non_finite_observations_are_unavailable(artifact):
    unavailable = [
        sample for sample in artifact["samples"]
        if sample["status"] == "unavailable"
    ]
    assert len(unavailable) == 8
    assert all(sample["ground_truth_anomaly"] is None for sample in unavailable)
    assert all(sample["predicted_anomaly"] is None for sample in unavailable)
    assert all(sample["detector_output"] is None for sample in unavailable)
    assert all(
        sample["failure_reason"] in {
            "no_observation",
            "invalid_observation:ValueError",
        }
        for sample in unavailable
    )


def test_threshold_and_direction_sensitive_outputs_are_preserved(artifact):
    by_id = {sample["sample_id"]: sample for sample in artifact["samples"]}
    cpu_up = by_id["step-order-service-cpu_cores_used-up"]
    cpu_down = by_id["step-order-service-cpu_cores_used-down"]
    request_down = by_id["step-order-service-request_rate-down"]

    assert cpu_up["predicted_anomaly"] is True
    assert cpu_up["predicted_actionable"] is True
    assert cpu_up["detector_output"]["features"]["cpu_cores_used"][
        "deviation_score"
    ] > artifact["detector"]["policy_threshold"]
    assert cpu_down["predicted_anomaly"] is True
    assert cpu_down["predicted_actionable"] is False
    assert request_down["predicted_actionable"] is False
    assert artifact["unavailable_checks"][1]["check_id"] == (
        "available_disk_space"
    )
    assert "do not include" in artifact["unavailable_checks"][1]["reason"]


def test_artifact_validation_recomputes_and_rejects_tampering(artifact):
    assert benchmark.validate_anomaly_artifact(artifact) == artifact["metrics"]

    tampered = deepcopy(artifact)
    tampered["metrics"]["point_anomaly"]["true_positives"] += 1
    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match="Stored metrics do not match",
    ):
        benchmark.validate_anomaly_artifact(tampered)

    contradicting = deepcopy(artifact)
    contradicting["samples"][0]["predicted_anomaly"] = not (
        contradicting["samples"][0]["predicted_anomaly"]
    )
    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match="contradicts detector output",
    ):
        benchmark.validate_anomaly_artifact(contradicting)


def test_detector_minimum_baseline_rows_fail_closed(tmp_path):
    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match="at least 20",
    ):
        benchmark._fit_runtime_compatible_policy([[1, 2, 3, 4]] * 19)

    path = tmp_path / "short-baseline.csv"
    fields = ["timestamp", "service", *FEATURE_NAMES]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(10):
            for service in benchmark.EXPECTED_SERVICES:
                writer.writerow({
                    "timestamp": f"2026-01-01T00:{index:02d}:00+00:00",
                    "service": service,
                    "request_rate": 0.5,
                    "p95_latency_seconds": 0.1,
                    "memory_bytes": 50_000_000,
                    "cpu_cores_used": 0.01,
                })

    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match="baseline_service_coverage_insufficient",
    ):
        benchmark.build_anomaly_benchmark(
            baseline_path=path,
            exploratory=True,
        )


def test_benchmark_does_not_use_network_or_live_remediation(
    monkeypatch,
):
    from app.services import anomaly_runtime, k8s_remediation

    network = Mock(side_effect=AssertionError("network must not be used"))
    runner = Mock(side_effect=AssertionError("live runner must not be used"))
    monkeypatch.setattr(anomaly_runtime.httpx, "AsyncClient", network)
    monkeypatch.setattr(k8s_remediation, "default_runner", runner)

    result = benchmark.build_anomaly_benchmark()

    assert result["run"]["evaluation_mode"] == "synthetic"
    assert network.call_count == 0
    assert runner.call_count == 0


def test_three_way_benchmark_uses_shared_inputs_and_disjoint_split(
    comparative_artifact,
):
    artifact = comparative_artifact
    provenance = artifact["provenance"]
    split = provenance["split"]
    training = set(split["training_timestamps"])
    evaluation = {sample["timestamp"] for sample in artifact["samples"]}

    assert artifact["schema_version"] == benchmark.COMPARATIVE_SCHEMA_VERSION
    assert split["training_evaluation_overlap"] is False
    assert artifact["run"]["code_revision_scope"].startswith(
        "HEAD revision only"
    )
    assert training.isdisjoint(evaluation)
    assert set(provenance["features"]) == set(FEATURE_NAMES)
    assert provenance["label_assumptions"]["operational_labels_present"] is False
    assert all(
        sample["predictions"] is None
        or set(sample["predictions"]) == {
            "isolation_forest", "direction_aware_mad", "hybrid",
        }
        for sample in artifact["samples"]
    )
    assert all(
        sample["predictions"]["hybrid"]["anomaly"]
        == (
            sample["predictions"]["isolation_forest"]["anomaly"]
            or sample["predictions"]["direction_aware_mad"]["anomaly"]
        )
        for sample in artifact["samples"]
        if sample["predictions"] is not None
    )
    for service, service_split in split["per_service"].items():
        assert service_split["training_row_count"] >= 20
        assert service_split["evaluation_baseline_row_count"] > 0


def test_three_way_benchmark_is_deterministic_for_fixed_inputs(
    comparative_artifact,
):
    assert (
        benchmark.build_comparative_anomaly_benchmark()
        == comparative_artifact
    )


def test_three_way_metrics_recompute_and_include_per_service_counts(
    comparative_artifact,
):
    artifact = comparative_artifact
    metrics = benchmark.validate_anomaly_artifact(artifact)
    assert metrics == artifact["metrics"]
    assert metrics["eligible_observation_count"] == 176
    assert metrics["unavailable_observation_count"] == 8
    assert metrics["unavailable_by_reason"] == {
        "invalid_observation:ValueError": 4,
        "no_observation": 4,
    }
    assert all(
        sample["label_source"] == "unlabeled_unavailable"
        for sample in artifact["samples"]
        if sample["status"] == "unavailable"
    )

    for method in ("isolation_forest", "direction_aware_mad", "hybrid"):
        aggregate = metrics["point_anomaly"][method]
        assert aggregate["eligible_observation_count"] == 176
        service_totals = {
            key: sum(
                metrics["per_service"][service]["point_anomaly"][method][key]
                for service in benchmark.EXPECTED_SERVICES
            )
            for key in (
                "true_positives", "false_positives",
                "true_negatives", "false_negatives",
            )
        }
        assert service_totals == {
            key: aggregate[key] for key in service_totals
        }

    assert metrics["mad_actionable_candidate"]["eligible_observation_count"] == 176
    assert sum(
        service["unavailable_observation_count"]
        for service in metrics["per_service"].values()
    ) == metrics["unavailable_observation_count"]


def test_three_way_episode_delay_uses_labeled_onset_and_reports_misses(
    comparative_artifact,
):
    assert comparative_artifact["episode_delay_contract"][
        "not_operational_incident_latency"
    ] is True
    metrics = comparative_artifact["metrics"]
    for method in ("isolation_forest", "direction_aware_mad", "hybrid"):
        delay = metrics["episode_detection_delay"][method]
        assert delay["episode_count"] == 4
        assert delay["onset_rule"] == "first timestamped point labeled anomalous"
        assert delay["detection_rule"] == (
            "first predicted anomaly at or after the labeled onset"
        )
        assert delay["detected_episode_count"] + delay[
            "missed_episode_count"
        ] == delay["episode_count"]
        assert all(
            (
                episode["delay_seconds"] is None
                and not episode["detected"]
            )
            or (
                episode["delay_seconds"] >= 0
                and episode["detected"]
            )
            for episode in delay["episodes"]
        )


def test_three_way_artifact_detects_metric_and_prediction_tampering(
    comparative_artifact,
):
    if comparative_artifact["schema_version"] >= 4:
        altered_prediction = deepcopy(comparative_artifact)
        sample = next(
            item for item in altered_prediction["samples"]
            if item["status"] == "evaluated"
        )
        sample["predictions"]["isolation_forest"]["anomaly"] = not (
            sample["predictions"]["isolation_forest"]["anomaly"]
        )
        with pytest.raises(
            benchmark.AnomalyBenchmarkIncomplete,
            match="integrity digest",
        ):
            benchmark.validate_anomaly_artifact(altered_prediction)

        altered_config = deepcopy(comparative_artifact)
        altered_config["methods"]["isolation_forest"]["random_state"] += 1
        with pytest.raises(
            benchmark.AnomalyBenchmarkIncomplete,
            match="integrity digest",
        ):
            benchmark.validate_anomaly_artifact(altered_config)

        altered_label = deepcopy(comparative_artifact)
        sample = next(
            item for item in altered_label["samples"]
            if item["status"] == "evaluated"
        )
        sample["ground_truth_anomaly"] = not sample["ground_truth_anomaly"]
        with pytest.raises(
            benchmark.AnomalyBenchmarkIncomplete,
            match="integrity digest",
        ):
            benchmark.validate_anomaly_artifact(altered_label)

        altered_metadata = deepcopy(comparative_artifact)
        altered_metadata["provenance"]["split"]["training_fraction"] = 0.6
        with pytest.raises(
            benchmark.AnomalyBenchmarkIncomplete,
            match="integrity digest",
        ):
            benchmark.validate_anomaly_artifact(altered_metadata)

    altered_metrics = deepcopy(comparative_artifact)
    altered_metrics["metrics"]["point_anomaly"]["hybrid"][
        "true_positives"
    ] += 1
    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match=(
            "integrity digest"
            if comparative_artifact["schema_version"] >= 4
            else "Stored comparative metrics"
        ),
    ):
        benchmark.validate_anomaly_artifact(altered_metrics)

    altered_prediction = deepcopy(comparative_artifact)
    sample = next(
        item for item in altered_prediction["samples"]
        if item["status"] == "evaluated"
    )
    sample["predictions"]["hybrid"]["anomaly"] = not (
        sample["predictions"]["hybrid"]["anomaly"]
    )
    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match=(
            "integrity digest"
            if comparative_artifact["schema_version"] >= 4
            else "method outputs are inconsistent"
        ),
    ):
        benchmark.validate_anomaly_artifact(altered_prediction)


def test_three_way_integrity_digest_is_not_detector_recomputation(
    comparative_artifact,
):
    if comparative_artifact["schema_version"] < 4:
        pytest.skip("Artifact integrity digest requires schema 4.")

    edited = deepcopy(comparative_artifact)
    sample = next(
        item for item in edited["samples"]
        if item["status"] == "evaluated"
        and not item["predictions"]["direction_aware_mad"]["anomaly"]
    )
    sample["predictions"]["isolation_forest"]["anomaly"] = not (
        sample["predictions"]["isolation_forest"]["anomaly"]
    )
    edited["integrity"]["payload_sha256"] = (
        benchmark._comparative_integrity_digest(edited)
    )
    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match="method outputs are inconsistent",
    ):
        benchmark.validate_anomaly_artifact(edited)


def test_three_way_cli_reproduction_matches_in_separate_outputs(
    tmp_path, monkeypatch,
):
    first = tmp_path / "run-one.json"
    second = tmp_path / "run-two.json"

    for output in (first, second):
        monkeypatch.setattr(
            "sys.argv",
            [
                "evaluate_anomaly_benchmark.py",
                "--three-way",
                "--output",
                str(output),
            ],
        )
        assert benchmark.main() == 0

    first_artifact = json.loads(first.read_text(encoding="utf-8"))
    second_artifact = json.loads(second.read_text(encoding="utf-8"))
    assert first_artifact["samples"] == second_artifact["samples"]
    assert first_artifact["metrics"] == second_artifact["metrics"]
    assert first_artifact["integrity"] == second_artifact["integrity"]


def test_three_way_output_path_never_overwrites_existing_artifact(
    tmp_path, monkeypatch, capsys,
):
    output = tmp_path / "existing.json"
    output.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "evaluate_anomaly_benchmark.py",
            "--three-way",
            "--output",
            str(output),
        ],
    )

    assert benchmark.main() == 2
    assert output.read_text(encoding="utf-8") == "preserve"
    assert "File exists" in capsys.readouterr().err


def test_three_way_generation_preserves_historical_artifacts(
    comparative_artifact,
):
    del comparative_artifact
    historical = (
        benchmark.PROJECT_ROOT / "benchmark/results/"
        "direction-aware-anomaly-benchmark.json",
        benchmark.PROJECT_ROOT / "benchmark/results/"
        "f29-anomaly-false-negative-analysis.json",
    )
    digests = [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in historical
    ]

    benchmark.build_comparative_anomaly_benchmark()

    assert [
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in historical
    ] == digests
