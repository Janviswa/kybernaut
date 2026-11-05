import json
import hashlib
from pathlib import Path
import sys

import pytest

from scripts import run_benchmark
from app.services.benchmark_metrics import (
    BenchmarkIncomplete, TYPES, compute_metrics as _compute_metrics,
    load_scenario_matrix,
)

# SYNTHETIC fixtures to test the maths only -- not real benchmark results.


def synthetic(detect_all=True, wrong_rca=0):
    cases = []
    for t in TYPES:
        for i in range(1, 5):
            c = {"scenario_id": f"{t.replace('_', '-')}-{i}",
                 "expected_category": t,
                 "observed_category": t if detect_all else None,
                 "detection_result": "detected" if detect_all else "missed",
                 "injected_at": "2026-01-01T00:00:00+00:00",
                 "detected_at": "2026-01-01T00:00:30+00:00"
                 if detect_all else None,
                 "evidence": ["synthetic evidence"],
                 "failure_reason": None,
                 "remediation_outcome": "executed",
                 "remediation_result": "success",
                 "verification_outcome": "verified",
                 "recovered_at": "2026-01-01T00:02:00+00:00",
                 "llm_latency_ms": 1000, "llm_tokens": 500,
                 "rca_json_valid": True}
            cases.append(c)
    for c in cases[:wrong_rca]:
        c["observed_category"] = "unknown"
    return cases


def healthy_controls():
    return [
        {
            "control_id": f"healthy-control-{n}",
            "alert_raised": False,
            "observed_category": "unknown",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:15:00+00:00",
            "evidence": [],
            "failure_reason": None,
        }
        for n in range(1, 6)
    ]


def write_matrix(path, *, transform=None):
    matrix = load_scenario_matrix()
    if transform is not None:
        matrix = transform(matrix)
    path.write_text(json.dumps(matrix))
    return path


def run_envelope(matrix_path=None, **changes):
    path = matrix_path or Path(__file__).resolve().parents[1] / (
        "benchmark/scenarios.json"
    )
    envelope = {
        "run_id": "synthetic-run-001",
        "evaluation_mode": "synthetic",
        "scenario_matrix_sha256": hashlib.sha256(
            path.read_bytes()
        ).hexdigest(),
        "started_at": "2025-12-31T23:59:00+00:00",
        "completed_at": "2026-01-01T00:20:00+00:00",
    }
    envelope.update(changes)
    return envelope


def live_cases():
    cases = synthetic()
    for index, case in enumerate(cases, start=1):
        case.update({
            "trial_id": f"trial-{index:02d}",
            "injection_status": "succeeded",
            "injected_monotonic_ns": 1_000_000_000,
            "detected_monotonic_ns": (
                1_000_000_000 + index * 1_000_000_000
            ),
        })
    return cases


def compute_metrics(cases, controls=None, **kwargs):
    return _compute_metrics(
        cases,
        healthy_controls() if controls is None else controls,
        **kwargs,
    )


def test_result_schema_requires_detection_outcome():
    cases = synthetic()
    del cases[0]["detection_result"]
    with pytest.raises(BenchmarkIncomplete, match="detection_result"):
        compute_metrics(cases)


def test_result_schema_keeps_detection_timestamp_consistent():
    cases = synthetic()
    cases[0]["detected_at"] = None
    with pytest.raises(BenchmarkIncomplete, match="detected_at"):
        compute_metrics(cases)


def test_result_schema_rejects_timestamps_without_timezone():
    cases = synthetic()
    cases[0]["injected_at"] = "2026-01-01T00:00:00"
    with pytest.raises(BenchmarkIncomplete, match="timezone-aware"):
        compute_metrics(cases)


def test_successful_execution_with_verified_recovery_is_valid():
    cases = synthetic()
    assert compute_metrics(cases)["mttr_seconds"] == 120.0


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {
                "remediation_outcome": "not_executed",
                "remediation_result": None,
            },
            "without successful executed remediation",
        ),
        (
            {"remediation_result": "failed"},
            "without successful executed remediation",
        ),
        (
            {"remediation_result": None},
            "remediation_result exactly when",
        ),
        (
            {"verification_outcome": "not_run"},
            "recovered_at exactly when",
        ),
    ],
)
def test_recovery_claim_requires_successful_execution_and_timestamp(
    changes, message
):
    cases = synthetic()
    cases[0].update(changes)
    with pytest.raises(BenchmarkIncomplete, match=message):
        compute_metrics(cases)


@pytest.mark.parametrize(
    "verification_outcome",
    ["verified", "not_verified"],
)
def test_not_executed_action_cannot_have_recovery_verification(
    verification_outcome,
):
    cases = synthetic()
    cases[0].update({
        "remediation_outcome": "not_executed",
        "remediation_result": None,
        "verification_outcome": verification_outcome,
        "recovered_at": None,
    })

    with pytest.raises(BenchmarkIncomplete):
        compute_metrics(cases)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("remediation_outcome", "unknown"),
        ("remediation_result", "unknown"),
        ("verification_outcome", "recovered"),
        ("recovered_at", "not-a-timestamp"),
    ],
)
def test_malformed_or_unsupported_execution_verification_fields_fail(
    field, value
):
    cases = synthetic()
    cases[0][field] = value

    with pytest.raises(BenchmarkIncomplete):
        compute_metrics(cases)


def test_dry_run_not_run_is_excluded_from_auditable_mttr():
    cases = synthetic()
    for index, case in enumerate(cases):
        case.update({
            "remediation_outcome": "not_applicable",
            "remediation_result": None,
            "verification_outcome": "not_applicable",
            "recovered_at": None,
        })
        if index < 2:
            case.update({
                "remediation_outcome": "executed",
                "remediation_result": "success",
                "verification_outcome": "verified",
                "recovered_at": (
                    "2026-01-01T00:00:30+00:00"
                    if index == 0 else "2026-01-01T00:01:30+00:00"
                ),
            })
    cases[2].update({
        "remediation_outcome": "not_executed",
        "remediation_result": None,
        "verification_outcome": "not_run",
        "recovered_at": None,
    })

    metrics = compute_metrics(cases)

    assert metrics["mttr_seconds"] == 60.0
    assert metrics["timing"]["verified_mttr"] == {
        "mean": 60.0,
        "median": 60.0,
        "p95": 87.0,
        "minimum": 30.0,
        "maximum": 90.0,
        "numerator": 120.0,
        "denominator": 2,
        "sample_count": 2,
        "missing_count": 0,
        "excluded_count": 18,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("injected_at", 123),
        ("injected_at", {}),
        ("detected_at", 123),
        ("recovered_at", []),
    ],
)
def test_timestamp_fields_reject_non_string_values(field, value):
    cases = synthetic()
    cases[0][field] = value
    with pytest.raises(BenchmarkIncomplete, match=field):
        compute_metrics(cases)


def test_malformed_non_null_detected_at_on_missed_case_is_rejected():
    cases = synthetic(detect_all=False)
    cases[0]["detected_at"] = "not-a-timestamp"
    with pytest.raises(BenchmarkIncomplete, match="detected_at"):
        compute_metrics(cases)


@pytest.mark.parametrize(
    ("field", "end_time"),
    [
        ("detected_at", "2025-12-31T23:59:59+00:00"),
        ("recovered_at", "2025-12-31T23:59:59+00:00"),
    ],
)
def test_negative_timing_intervals_are_rejected(field, end_time):
    cases = synthetic()
    cases[0][field] = end_time
    with pytest.raises(BenchmarkIncomplete, match=f"{field} before injected_at"):
        compute_metrics(cases)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("llm_latency_ms", True),
        ("llm_latency_ms", -1),
        ("llm_latency_ms", float("nan")),
        ("llm_latency_ms", float("inf")),
        ("llm_tokens", False),
        ("llm_tokens", -1),
        ("llm_tokens", float("-inf")),
        ("llm_tokens", "500"),
    ],
)
def test_optional_llm_numeric_fields_reject_invalid_values(field, value):
    cases = synthetic()
    cases[0][field] = value
    with pytest.raises(BenchmarkIncomplete, match=field):
        compute_metrics(cases)


@pytest.mark.parametrize("value", ["true", 1, [], {}])
def test_rca_validity_flag_must_be_boolean_or_null(value):
    cases = synthetic()
    cases[0]["rca_json_valid"] = value
    with pytest.raises(BenchmarkIncomplete, match="rca_json_valid"):
        compute_metrics(cases)


def test_metric_denominators_report_populated_and_missing_samples():
    cases = synthetic()
    cases[0]["llm_latency_ms"] = None
    cases[0]["llm_tokens"] = None
    cases[1]["rca_json_valid"] = None
    cases[2]["remediation_result"] = "failed"
    cases[2]["verification_outcome"] = "not_verified"
    cases[2]["recovered_at"] = None
    metrics = compute_metrics(cases)

    assert metrics["timing"]["mttd"] == {
        "mean": 30.0,
        "median": 30.0,
        "p95": 30.0,
        "minimum": 30.0,
        "maximum": 30.0,
        "numerator": 600.0,
        "denominator": 20,
        "sample_count": 20,
        "missing_count": 0,
        "excluded_count": 0,
    }
    assert metrics["timing"]["verified_mttr"]["denominator"] == 19
    assert metrics["timing"]["verified_mttr"]["missing_count"] == 0
    assert metrics["timing"]["verified_mttr"]["excluded_count"] == 1
    assert metrics["timing"]["mttd_duration_source"] == "UTC_timestamp_delta"
    assert metrics["timing"]["mttd_by_scenario"]["cpu-spike-1"]["mean"] == 30.0
    assert metrics["rca"]["json_validity_counts"] == {
        "rate": 1.0,
        "numerator": 19,
        "denominator": 19,
        "sample_count": 19,
        "missing_count": 1,
        "excluded_count": 0,
    }
    assert metrics["llm"]["latency_counts"]["sample_count"] == 19
    assert metrics["llm"]["latency_counts"]["missing_count"] == 1
    assert metrics["remediation_success_counts"]["numerator"] == 19
    assert metrics["remediation_success_counts"]["denominator"] == 20
    assert metrics["verification_success_counts"]["numerator"] == 19
    assert metrics["verification_success_counts"]["denominator"] == 20


def test_empty_optional_metric_denominators_are_null_and_counted():
    cases = synthetic(detect_all=False)
    for case in cases:
        case.update({
            "remediation_outcome": "not_applicable",
            "remediation_result": None,
            "verification_outcome": "not_applicable",
            "recovered_at": None,
            "rca_json_valid": None,
            "llm_latency_ms": None,
            "llm_tokens": None,
        })
    metrics = compute_metrics(cases)

    assert metrics["mttd_seconds"] is None
    assert metrics["timing"]["mttd"]["denominator"] == 0
    assert metrics["timing"]["mttd"]["missing_count"] == 0
    assert metrics["timing"]["mttd"]["excluded_count"] == 20
    assert metrics["timing"]["verified_mttr"]["denominator"] == 0
    assert metrics["timing"]["verified_mttr"]["excluded_count"] == 20
    assert metrics["rca"]["json_validity"] is None
    assert metrics["rca"]["json_validity_counts"]["missing_count"] == 20
    assert metrics["remediation_success"] is None
    assert metrics["remediation_success_counts"]["denominator"] == 0
    assert metrics["remediation_success_counts"]["excluded_count"] == 20
    assert metrics["verification_success"] is None
    assert metrics["llm"]["mean_latency_ms"] is None
    assert metrics["llm"]["latency_counts"]["missing_count"] == 20


def test_denied_unavailable_unknown_and_failed_execution_remain_valid():
    cases = synthetic()
    cases[0].update({
        "observed_category": "unknown",
        "remediation_outcome": "not_applicable",
        "remediation_result": None,
        "verification_outcome": "not_applicable",
        "recovered_at": None,
    })
    cases[1].update({
        "remediation_outcome": "not_executed",
        "remediation_result": None,
        "verification_outcome": "not_applicable",
        "recovered_at": None,
    })
    cases[2].update({
        "remediation_result": "failed",
        "verification_outcome": "not_run",
        "recovered_at": None,
    })
    cases[3].update({
        "observed_category": None,
        "remediation_outcome": "not_executed",
        "remediation_result": None,
        "verification_outcome": "not_run",
        "recovered_at": None,
        "failure_reason": "evidence_unavailable",
    })

    assert compute_metrics(cases)["cases"] == 20


def test_perfect_run():
    m = compute_metrics(synthetic(), healthy_controls())
    assert m["detection"]["precision"] == 1.0 and m["detection"]["f1"] == 1.0
    assert m["mttd_seconds"] == 30.0 and m["mttr_seconds"] == 120.0
    assert m["classification"]["accuracy"] == 1.0


def test_false_positives_and_misses():
    controls = healthy_controls()
    controls[0]["alert_raised"] = True
    m = compute_metrics(synthetic(), controls)
    assert m["detection"]["false_positives"] == 1
    assert m["detection"]["precision"] == pytest.approx(20 / 21, abs=1e-4)
    m = compute_metrics(synthetic(detect_all=False))
    assert m["detection"]["recall"] == 0.0 and m["mttd_seconds"] is None


def test_classification_accuracy_drops_with_wrong_predictions():
    m = compute_metrics(synthetic(wrong_rca=5))
    assert m["classification"]["accuracy"] == 0.75


def test_incomplete_matrix_is_refused():
    with pytest.raises(BenchmarkIncomplete):
        compute_metrics(synthetic()[:19])
    skewed = synthetic()
    skewed[0]["expected_category"] = "pod_crash"
    with pytest.raises(BenchmarkIncomplete):
        compute_metrics(skewed)


def test_scenario_file_is_the_20_case_matrix():
    path = Path(__file__).resolve().parents[1] / "benchmark" / "scenarios.json"
    matrix = json.loads(path.read_text())
    cases = matrix["cases"]
    assert len(cases) == 20
    assert {case["scenario_id"] for case in cases} == {
        case["scenario_id"] for case in load_scenario_matrix()["cases"]
    }
    for t in TYPES:
        assert sum(c["expected_category"] == t for c in cases) == 4
    assert all(c["expected_outcome"] for c in cases)
    controls = matrix["controls"]
    assert len(controls) == matrix["controls_recommended"] == 5
    assert all(not c["expected_alert"] for c in controls)
    assert all(c["expected_category"] == "unknown" for c in controls)
    assert "delete" not in " ".join(
        c["variant"] for c in cases if c["scenario"] == "pod-crash"
    ).lower()


def test_canonical_scenario_mapping_is_valid():
    assert compute_metrics(synthetic(), healthy_controls())["cases"] == 20


def test_single_scenario_id_with_wrong_expected_category_is_rejected():
    cases = synthetic()
    cases[0]["expected_category"] = "pod_crash"
    with pytest.raises(BenchmarkIncomplete, match="incorrect expected_category"):
        compute_metrics(cases, healthy_controls())


def test_swapped_categories_are_rejected_even_when_totals_remain_four_each():
    cases = synthetic()
    cases[0]["expected_category"], cases[4]["expected_category"] = (
        cases[4]["expected_category"], cases[0]["expected_category"]
    )
    with pytest.raises(BenchmarkIncomplete, match="incorrect expected_category"):
        compute_metrics(cases, healthy_controls())


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "extra",
        "malformed",
    ],
)
def test_scenario_records_must_match_canonical_ids_and_shape(mutation):
    cases = synthetic()
    if mutation == "missing":
        cases.pop()
    elif mutation == "duplicate":
        cases[1]["scenario_id"] = cases[0]["scenario_id"]
    elif mutation == "extra":
        cases[-1]["scenario_id"] = "other-incident-1"
    else:
        cases[0]["scenario_id"] = None
    with pytest.raises(BenchmarkIncomplete):
        compute_metrics(cases, healthy_controls())


def test_missing_and_unreadable_scenario_matrices_fail_clearly(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(BenchmarkIncomplete, match="Unable to load scenario matrix"):
        compute_metrics(
            synthetic(), healthy_controls(), scenario_matrix_path=missing
        )

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json")
    with pytest.raises(BenchmarkIncomplete, match="Unable to load scenario matrix"):
        compute_metrics(
            synthetic(), healthy_controls(), scenario_matrix_path=malformed
        )
    with pytest.raises(BenchmarkIncomplete, match="Unable to load scenario matrix"):
        compute_metrics(
            synthetic(), healthy_controls(), scenario_matrix_path=tmp_path
        )


def test_malformed_canonical_scenario_matrix_fails_clearly(tmp_path):
    path = tmp_path / "bad-matrix.json"

    def malformed(matrix):
        matrix["cases"][0]["scenario_id"] = matrix["cases"][1]["scenario_id"]
        return matrix

    write_matrix(path, transform=malformed)
    with pytest.raises(BenchmarkIncomplete, match="invalid or duplicate scenario"):
        compute_metrics(
            synthetic(), healthy_controls(), scenario_matrix_path=path
        )


def test_invalid_expected_target_id_in_matrix_fails_clearly(tmp_path):
    path = tmp_path / "bad-target-matrix.json"

    def malformed(matrix):
        matrix["cases"][0]["expected_target_id"] = {"resource": "pod"}
        return matrix

    write_matrix(path, transform=malformed)
    with pytest.raises(BenchmarkIncomplete, match="invalid expected_target_id"):
        compute_metrics(
            synthetic(), healthy_controls(), scenario_matrix_path=path
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_control",
        "incorrect_expected_category",
        "missing_control",
        "invalid_duration",
    ],
)
def test_malformed_canonical_control_matrix_fails_clearly(tmp_path, mutation):
    path = tmp_path / "bad-controls.json"

    def malformed(matrix):
        if mutation == "duplicate_control":
            matrix["controls"][1]["control_id"] = (
                matrix["controls"][0]["control_id"]
            )
        elif mutation == "incorrect_expected_category":
            matrix["controls"][0]["expected_category"] = "cpu_spike"
        elif mutation == "missing_control":
            matrix["controls"].pop()
            matrix["controls_recommended"] = 4
        else:
            matrix["controls"][0]["duration_minutes"] = True
        return matrix

    write_matrix(path, transform=malformed)
    with pytest.raises(BenchmarkIncomplete):
        compute_metrics(
            synthetic(), healthy_controls(), scenario_matrix_path=path
        )


def test_all_five_declared_healthy_controls_are_required():
    assert compute_metrics(synthetic(), healthy_controls())["controls"] == 5
    with pytest.raises(BenchmarkIncomplete, match="all declared healthy controls"):
        _compute_metrics(synthetic(), None)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "extra",
        "incorrect_id",
        "incorrect_category",
        "malformed_timestamp",
        "end_before_start",
        "missing_evidence",
        "malformed_failure_reason",
    ],
)
def test_healthy_controls_must_match_canonical_schema(mutation):
    controls = healthy_controls()
    if mutation == "missing":
        controls.pop()
    elif mutation == "duplicate":
        controls[1]["control_id"] = controls[0]["control_id"]
    elif mutation == "extra":
        controls[-1]["control_id"] = "healthy-control-extra"
    elif mutation == "incorrect_id":
        controls[0]["control_id"] = "unknown-control"
    elif mutation == "incorrect_category":
        controls[0]["observed_category"] = "cpu_spike"
    elif mutation == "malformed_timestamp":
        controls[0]["started_at"] = "not-a-time"
    elif mutation == "end_before_start":
        controls[0]["ended_at"] = "2025-12-31T23:59:00+00:00"
    elif mutation == "missing_evidence":
        controls[0].pop("evidence")
    else:
        controls[0]["failure_reason"] = 7

    with pytest.raises(BenchmarkIncomplete):
        compute_metrics(synthetic(), controls)


def test_control_alert_still_counts_as_false_positive_with_canonical_controls():
    controls = healthy_controls()
    controls[0]["alert_raised"] = True
    metrics = compute_metrics(synthetic(), controls)
    assert metrics["detection"]["false_positives"] == 1


def test_category_metrics_cover_known_unknown_and_unclassified_predictions():
    cases = synthetic()
    cases[0]["observed_category"] = "unknown"
    cases[1]["observed_category"] = None
    cases[2]["detection_result"] = "missed"
    cases[2]["detected_at"] = None
    controls = healthy_controls()

    metrics = compute_metrics(cases, controls)["classification_metrics"]

    assert metrics["sample_count"] == 25
    assert metrics["missed_detection_count"] == 1
    assert metrics["correct_count"] == 23
    assert metrics["incorrect_count"] == 1
    assert metrics["unclassified_count"] == 1
    assert metrics["confusion_matrix"]["cpu_spike"]["unknown"] == 1
    assert metrics["confusion_matrix"]["cpu_spike"]["unclassified"] == 1
    assert metrics["per_category"]["unknown"]["support"] == 5
    assert metrics["per_category"]["unknown"]["true_positives"] == 5
    assert metrics["per_category"]["cpu_spike"]["false_negatives"] == 2


def test_category_metrics_report_undefined_precision_and_zero_denominators():
    cases = synthetic()
    for case in cases:
        case["observed_category"] = "cpu_spike"
    metrics = compute_metrics(cases, healthy_controls())[
        "classification_metrics"
    ]

    assert metrics["per_category"]["memory_leak"]["predicted_count"] == 0
    assert metrics["per_category"]["memory_leak"]["precision"] is None
    assert metrics["per_category"]["memory_leak"]["recall"] == 0.0
    assert metrics["per_category"]["memory_leak"]["f1"] == 0.0


def test_legacy_classification_scalars_remain_available():
    metrics = compute_metrics(synthetic(), healthy_controls())

    assert metrics["classification"]["accuracy"] == 1.0
    assert metrics["classification"]["macro_f1"] == 1.0
    assert metrics["classification"]["per_class_f1"]["cpu_spike"] == 1.0


def test_target_correctness_is_explicitly_unmeasured_without_truth_labels():
    cases = synthetic()
    for case in cases:
        case["observed_target_id"] = "some-target"

    metric = compute_metrics(cases, healthy_controls())["target_correctness"]

    assert metric == {
        "status": "not_measured",
        "numerator": 0,
        "denominator": 0,
        "sample_count": 0,
        "excluded_count": 20,
        "exclusions_by_reason": {
            "canonical_expected_target_not_defined": 20,
        },
        "accuracy": None,
    }


def test_target_correctness_uses_only_comparable_explicit_ids(tmp_path):
    matrix_path = tmp_path / "targeted-matrix.json"

    def add_one_expected_target(matrix):
        for case in matrix["cases"][:3]:
            case["expected_target_id"] = "ns-a/pod:uid-1"
        return matrix

    write_matrix(matrix_path, transform=add_one_expected_target)
    cases = synthetic()
    cases[0]["observed_target_id"] = "ns-a/pod:uid-1"
    cases[1]["observed_target_id"] = "ns-a/pod:uid-2"
    cases[2]["observed_target_id"] = None

    metric = compute_metrics(
        cases,
        healthy_controls(),
        scenario_matrix_path=matrix_path,
    )["target_correctness"]

    assert metric["status"] == "measured"
    assert metric["numerator"] == 1
    assert metric["denominator"] == 2
    assert metric["excluded_count"] == 18
    assert metric["exclusions_by_reason"] == {
        "canonical_expected_target_not_defined": 17,
        "observed_target_id_missing_or_invalid": 1,
    }
    assert metric["accuracy"] == 0.5


def test_malformed_supplied_observed_target_id_fails_validation():
    cases = synthetic()
    cases[0]["observed_target_id"] = ["not-an-id"]

    with pytest.raises(BenchmarkIncomplete, match="observed_target_id"):
        compute_metrics(cases)


def test_target_correctness_counts_missing_observed_target_when_truth_exists(
    tmp_path,
):
    matrix_path = tmp_path / "targeted-matrix.json"

    def add_one_expected_target(matrix):
        matrix["cases"][0]["expected_target_id"] = "order-service"
        return matrix

    write_matrix(matrix_path, transform=add_one_expected_target)
    metric = compute_metrics(
        synthetic(), healthy_controls(), scenario_matrix_path=matrix_path
    )["target_correctness"]

    assert metric["denominator"] == 0
    assert metric["excluded_count"] == 20
    assert metric["exclusions_by_reason"] == {
        "canonical_expected_target_not_defined": 19,
        "observed_target_id_missing_or_invalid": 1,
    }


def test_provenance_coverage_distinguishes_missing_and_incomplete():
    cases = synthetic()
    cases[0]["evidence_provenance"] = [{
        "source": "kubernetes",
        "status": "available",
        "source_observed_at": "2026-01-01T00:00:00+00:00",
        "timestamp_provenance": "pod_status_transition",
    }]
    cases[1]["evidence_provenance"] = [{
        "source": "prometheus",
        "status": "available",
        "timestamp_provenance": "instant_query",
    }]
    cases[2]["evidence_provenance"] = [{
        "source": "prometheus",
        "status": "available",
        "timestamp_provenance": "instant_query",
    }]
    cases[3]["evidence_provenance"] = [{
        "source": "prometheus",
        "status": "invalid",
        "source_observed_at": "2026-01-01T00:00:00+00:00",
    }]
    metrics = compute_metrics(cases, healthy_controls())[
        "evidence_provenance"
    ]

    assert metrics["record_count"] == 25
    assert metrics["records_with_valid_provenance"] == 1
    assert metrics["records_missing_provenance_field"] == 21
    assert metrics["invalid_entry_count"] == 0
    assert metrics["incomplete_entry_count"] == 3
    assert metrics["freshness_assessed_entry_count"] == 0
    assert metrics["freshness_unassessed_entry_count"] == 1


def test_malformed_explicit_provenance_values_fail_validation():
    cases = synthetic()
    cases[0]["evidence_provenance"] = [{
        "source_identity": "prometheus",
        "status": "available",
        "source_sample_timestamp": "bad-time",
        "query_evaluation_timestamp": None,
        "observation_time": "2026-01-01T00:00:03+00:00",
    }]

    with pytest.raises(BenchmarkIncomplete, match="source_sample_timestamp"):
        compute_metrics(cases)


def test_malformed_explicit_provenance_status_fails_validation():
    cases = synthetic()
    cases[0]["evidence_provenance"] = [{
        "source_identity": "prometheus",
        "status": ["available"],
        "source_sample_timestamp": None,
        "query_evaluation_timestamp": None,
        "observation_time": "2026-01-01T00:00:03+00:00",
    }]

    with pytest.raises(BenchmarkIncomplete, match="provenance field status"):
        compute_metrics(cases)


def test_null_evidence_reference_fails_when_explicitly_supplied():
    cases = synthetic()
    cases[0]["evidence_provenance"] = [{
        "source_identity": "prometheus",
        "status": "available",
        "source_sample_timestamp": None,
        "query_evaluation_timestamp": None,
        "observation_time": "2026-01-01T00:00:03+00:00",
        "evidence_ref": None,
    }]

    with pytest.raises(BenchmarkIncomplete, match="evidence_ref"):
        compute_metrics(cases)


def test_parseable_observation_time_does_not_claim_freshness():
    cases = synthetic()
    for case in cases:
        case["evidence_provenance"] = [{
            "source": "prometheus",
            "status": "available",
            "source_observed_at": "2026-01-01T00:00:00+00:00",
            "query_timestamp": "2026-01-01T00:00:01+00:00",
            "timestamp_provenance": "prometheus_instant_query",
        }]
    metrics = compute_metrics(cases, healthy_controls())[
        "evidence_provenance"
    ]

    assert metrics["valid_entry_count"] == 20
    assert metrics["freshness_assessed_entry_count"] == 0
    assert metrics["freshness_unassessed_entry_count"] == 20
    assert metrics["freshness_unassessed_record_count"] == 25
    assert "do not establish freshness" in metrics["freshness_limitation"]


def test_explicit_provenance_distinguishes_sample_query_and_observation_times():
    cases = synthetic()
    cases[0]["evidence_provenance"] = [{
        "evidence_ref": "evidence://local/1",
        "source_identity": "prometheus:cluster-a",
        "status": "available",
        "source_sample_timestamp": "2026-01-01T00:00:00+00:00",
        "query_evaluation_timestamp": "2026-01-01T00:00:02+00:00",
        "observation_time": "2026-01-01T00:00:03+00:00",
    }]
    metrics = compute_metrics(cases, healthy_controls())[
        "evidence_provenance"
    ]

    assert metrics["valid_entry_count"] == 1
    assert metrics["source_sample_timestamp_count"] == 1
    assert metrics["query_evaluation_timestamp_count"] == 1
    assert metrics["observation_time_count"] == 1
    assert metrics["evidence_reference_count"] == 1
    assert metrics["freshness_assessed_entry_count"] == 0


def test_policy_and_approval_safety_are_not_inferred_from_missing_fields():
    metrics = compute_metrics(synthetic(), healthy_controls())

    assert metrics["policy_approval_safety"]["status"] == "not_measured"
    assert metrics["policy_approval_safety"]["sample_count"] == 0
    assert metrics["policy_approval_safety"]["excluded_count"] == 20
    assert metrics["policy_approval_safety"]["safety_claim"] is None
    assert "does not independently prove" in (
        metrics["policy_approval_safety"]["limitation"]
    )


def _valid_policy_approval(**changes):
    evidence = {
        "policy_decision": "require_approval",
        "approval_required": True,
        "approval_outcome": "approved",
        "execution_status": "executed",
        "verification_result": "verified",
        "decision_at": "2026-01-01T00:00:10+00:00",
        "approval_decided_at": "2026-01-01T00:00:20+00:00",
        "execution_started_at": "2026-01-01T00:00:30+00:00",
        "audit_event_refs": ["audit://local/event-1"],
    }
    evidence.update(changes)
    return evidence


def test_policy_approval_states_are_separately_counted():
    cases = synthetic()
    cases[0]["policy_approval"] = _valid_policy_approval()
    cases[1]["policy_approval"] = _valid_policy_approval(
        policy_decision="deny",
        approval_required=False,
        approval_outcome="not_required",
        execution_status="not_executed",
        verification_result="not_run",
        approval_decided_at=None,
        execution_started_at=None,
        audit_event_refs=[],
    )
    cases[1].update({
        "remediation_outcome": "not_executed",
        "remediation_result": None,
        "verification_outcome": "not_run",
        "recovered_at": None,
    })
    metric = compute_metrics(cases, healthy_controls())[
        "policy_approval_safety"
    ]

    assert metric["status"] == "not_measured"
    assert metric["coverage_status"] == "schema_validated"
    assert metric["sample_count"] == 2
    assert metric["excluded_count"] == 18
    assert metric["policy_decision_counts"] == {
        "require_approval": 1,
        "deny": 1,
    }
    assert metric["approval_outcome_counts"] == {
        "approved": 1,
        "not_required": 1,
    }
    assert metric["execution_status_counts"] == {
        "executed": 1,
        "not_executed": 1,
    }
    assert metric["records_with_audit_event_refs"] == 1
    assert metric["records_without_audit_event_refs"] == 1
    assert metric["safety_claim"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"approval_required": "true"},
        {"policy_decision": "deny"},
        {"approval_outcome": "pending"},
        {"execution_status": "not_executed"},
        {"verification_result": "not_run"},
        {"execution_started_at": "2026-01-01T00:00:15+00:00"},
        {"audit_event_refs": "audit://not-a-list"},
    ],
)
def test_contradictory_policy_approval_evidence_fails_validation(changes):
    cases = synthetic()
    cases[0]["policy_approval"] = _valid_policy_approval(**changes)

    with pytest.raises(BenchmarkIncomplete):
        compute_metrics(cases)


def test_policy_approval_missing_fields_fail_closed():
    cases = synthetic()
    cases[0]["policy_approval"] = {"policy_decision": "allow"}

    with pytest.raises(BenchmarkIncomplete, match="missing"):
        compute_metrics(cases)


@pytest.mark.parametrize(
    ("approval_outcome", "approval_decided_at"),
    [
        ("pending", "2026-01-01T00:00:20+00:00"),
        ("not_required", "2026-01-01T00:00:20+00:00"),
    ],
)
def test_approval_timestamp_requires_terminal_approval_outcome(
    approval_outcome, approval_decided_at
):
    cases = synthetic()
    cases[0]["policy_approval"] = _valid_policy_approval(
        policy_decision="require_approval" if approval_outcome == "pending"
        else "allow",
        approval_required=approval_outcome == "pending",
        approval_outcome=approval_outcome,
        execution_status="not_executed",
        verification_result="not_run",
        approval_decided_at=approval_decided_at,
        execution_started_at=None,
    )
    cases[0].update({
        "remediation_outcome": "not_executed",
        "remediation_result": None,
        "verification_outcome": "not_run",
        "recovered_at": None,
    })

    with pytest.raises(BenchmarkIncomplete, match="terminal approval outcome"):
        compute_metrics(cases)


def test_legacy_and_versioned_result_schemas_are_compatible():
    cases = synthetic()
    controls = healthy_controls()

    legacy = compute_metrics(cases, controls)
    versioned = compute_metrics(cases, controls, schema_version=1)

    assert legacy == versioned


@pytest.mark.parametrize("version", [True, 3, "1", 1.0])
def test_unsupported_result_schema_versions_fail(version):
    with pytest.raises(BenchmarkIncomplete, match="schema_version"):
        compute_metrics(synthetic(), healthy_controls(), schema_version=version)


@pytest.mark.parametrize("mode", ["synthetic", "live_lab"])
def test_run_envelope_modes_are_declarations_not_execution_proof(mode):
    envelope = run_envelope(evaluation_mode=mode)
    metrics = compute_metrics(
        live_cases() if mode == "live_lab" else synthetic(),
        healthy_controls(),
        schema_version=2,
        run_envelope=envelope,
    )

    assert metrics["run_metadata"]["status"] == "enveloped"
    assert metrics["run_metadata"]["run_id"] == "synthetic-run-001"
    assert metrics["run_metadata"]["evaluation_mode"] == mode
    assert metrics["run_metadata"]["matrix_digest_matched"] is True
    assert metrics["run_metadata"]["execution_independently_verified"] is False
    assert metrics["detection"]["recall"] == 1.0


def test_live_mttd_uses_monotonic_time_and_reports_distribution():
    cases = live_cases()
    metrics = compute_metrics(
        cases,
        healthy_controls(),
        schema_version=2,
        run_envelope=run_envelope(evaluation_mode="live_lab"),
    )

    timing = metrics["timing"]["mttd"]
    assert metrics["timing"]["mttd_duration_source"] == "monotonic_clock"
    assert timing["mean"] == 10.5
    assert timing["median"] == 10.5
    assert timing["p95"] == 19.05
    assert timing["minimum"] == 1.0
    assert timing["maximum"] == 20.0
    assert timing["denominator"] == 20
    assert metrics["timing"]["mttd_by_scenario"]["cpu-spike-1"]["mean"] == 1.0


def test_failed_live_injection_is_not_counted_as_detection_or_miss():
    cases = live_cases()
    failed = cases[0]
    failed.update({
        "injection_status": "failed",
        "detection_result": "not_measured",
        "detected_at": None,
        "injected_monotonic_ns": None,
        "detected_monotonic_ns": None,
        "failure_reason": "synthetic injection precondition failed",
        "remediation_outcome": "not_applicable",
        "remediation_result": None,
        "verification_outcome": "not_applicable",
        "recovered_at": None,
    })
    metrics = compute_metrics(
        cases,
        healthy_controls(),
        schema_version=2,
        run_envelope=run_envelope(evaluation_mode="live_lab"),
    )

    assert metrics["detection"]["true_positives"] == 19
    assert metrics["detection"]["false_negatives"] == 0
    assert metrics["detection"]["successful_injections"] == 19
    assert metrics["detection"]["failed_injections"] == 1
    assert metrics["detection"]["sample_count"] == 19
    assert metrics["detection"]["excluded_count"] == 1
    assert metrics["timing"]["mttd"]["denominator"] == 19
    assert metrics["timing"]["mttd_failed_injections"] == ["cpu-spike-1"]


def test_live_detection_rejects_nonunique_trial_association():
    cases = live_cases()
    cases[1]["trial_id"] = cases[0]["trial_id"]

    with pytest.raises(BenchmarkIncomplete, match="trial_id values must be unique"):
        compute_metrics(
            cases,
            healthy_controls(),
            schema_version=2,
            run_envelope=run_envelope(evaluation_mode="live_lab"),
        )


def test_live_detected_case_requires_monotonic_detection_time():
    cases = live_cases()
    cases[0]["detected_monotonic_ns"] = None

    with pytest.raises(BenchmarkIncomplete, match="monotonic"):
        compute_metrics(
            cases,
            healthy_controls(),
            schema_version=2,
            run_envelope=run_envelope(evaluation_mode="live_lab"),
        )


def test_run_envelope_hashes_exact_matrix_bytes(tmp_path):
    matrix_path = tmp_path / "matrix.json"
    write_matrix(matrix_path)
    envelope = run_envelope(matrix_path)

    metrics = compute_metrics(
        synthetic(),
        healthy_controls(),
        schema_version=2,
        run_envelope=envelope,
        scenario_matrix_path=matrix_path,
    )

    assert metrics["run_metadata"]["scenario_matrix_sha256"] == (
        hashlib.sha256(matrix_path.read_bytes()).hexdigest()
    )


def test_run_envelope_rejects_matrix_digest_mismatch(tmp_path):
    matrix_path = tmp_path / "matrix.json"
    write_matrix(matrix_path)
    envelope = run_envelope(matrix_path)
    envelope["scenario_matrix_sha256"] = "0" * 64

    with pytest.raises(BenchmarkIncomplete, match="does not match"):
        compute_metrics(
            synthetic(),
            healthy_controls(),
            schema_version=2,
            run_envelope=envelope,
            scenario_matrix_path=matrix_path,
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"run_id": ""}, "run_id"),
        ({"evaluation_mode": "production"}, "evaluation_mode"),
        ({"scenario_matrix_sha256": "not-a-digest"}, "SHA-256"),
        ({"started_at": "2026-01-01T00:00:00"}, "started_at"),
        (
            {
                "started_at": "2026-01-01T00:05:00+00:00",
                "completed_at": "2026-01-01T00:04:00+00:00",
            },
            "must not precede",
        ),
        (
            {
                "started_at": "2026-01-01T00:01:00+00:00",
                "completed_at": "2026-01-01T00:20:00+00:00",
            },
            "injected_at must fall within",
        ),
        (
            {
                "started_at": "2025-12-31T23:59:00+00:00",
                "completed_at": "2026-01-01T00:14:00+00:00",
            },
            "ended_at must fall within",
        ),
    ],
)
def test_run_envelope_rejects_invalid_or_inconsistent_metadata(
    changes, message
):
    with pytest.raises(BenchmarkIncomplete, match=message):
        compute_metrics(
            synthetic(),
            healthy_controls(),
            schema_version=2,
            run_envelope=run_envelope(**changes),
        )


def test_run_envelope_rejects_case_and_control_cross_run_ids():
    cases = synthetic()
    cases[0]["run_id"] = "different-run"
    with pytest.raises(BenchmarkIncomplete, match="Scenario .* run_id"):
        compute_metrics(
            cases,
            healthy_controls(),
            schema_version=2,
            run_envelope=run_envelope(),
        )

    controls = healthy_controls()
    controls[0]["run_id"] = "different-run"
    with pytest.raises(BenchmarkIncomplete, match="Control .* run_id"):
        compute_metrics(
            synthetic(),
            controls,
            schema_version=2,
            run_envelope=run_envelope(),
        )


@pytest.mark.parametrize("record_kind", ["case", "control"])
def test_run_envelope_bounds_legacy_query_timestamp(record_kind):
    cases = synthetic()
    controls = healthy_controls()
    provenance = [{
        "source": "prometheus",
        "status": "available",
        "source_observed_at": "2025-12-31T23:58:00+00:00",
        "query_timestamp": "2026-01-01T00:21:00+00:00",
        "timestamp_provenance": "instant_query",
    }]
    if record_kind == "case":
        cases[0]["evidence_provenance"] = provenance
        owner = "Scenario"
    else:
        controls[0]["evidence_provenance"] = provenance
        owner = "Control"

    with pytest.raises(
        BenchmarkIncomplete,
        match=f"{owner} .* query_timestamp must fall within",
    ):
        compute_metrics(
            cases,
            controls,
            schema_version=2,
            run_envelope=run_envelope(),
        )


def test_run_envelope_requires_v2_and_rejects_missing_or_legacy_run():
    with pytest.raises(BenchmarkIncomplete, match="requires a run envelope"):
        compute_metrics(
            synthetic(), healthy_controls(), schema_version=2
        )
    with pytest.raises(BenchmarkIncomplete, match="requires schema_version 2"):
        compute_metrics(
            synthetic(),
            healthy_controls(),
            run_envelope=run_envelope(),
        )


def test_results_cli_accepts_documented_schema(tmp_path, monkeypatch, capsys):
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps({
        "cases": synthetic(),
        "controls": healthy_controls(),
    }))
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 0
    metrics = json.loads(capsys.readouterr().out)
    assert metrics["cases"] == 20
    assert metrics["controls"] == 5
    assert metrics["classification"]["macro_f1"] == 1.0
    assert metrics["classification_metrics"]["macro_f1_denominator"] == 6
    assert metrics["target_correctness"]["status"] == "not_measured"


def test_results_cli_accepts_optional_schema_version(tmp_path, monkeypatch, capsys):
    results_path = tmp_path / "versioned-results.json"
    results_path.write_text(json.dumps({
        "schema_version": 1,
        "cases": synthetic(),
        "controls": healthy_controls(),
        "future_optional_metadata": {"producer": "synthetic-test"},
    }))
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 0
    assert json.loads(capsys.readouterr().out)["cases"] == 20


@pytest.mark.parametrize("version", [None, True, 3, "1"])
def test_results_cli_rejects_invalid_schema_versions(
    tmp_path, monkeypatch, capsys, version
):
    results_path = tmp_path / "invalid-version-results.json"
    results_path.write_text(json.dumps({
        "schema_version": version,
        "cases": synthetic(),
        "controls": healthy_controls(),
    }))
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 2
    assert "schema_version must be integer 1 or 2" in capsys.readouterr().err


def test_results_cli_accepts_v2_envelope_without_asserting_live_execution(
    tmp_path, monkeypatch, capsys
):
    results_path = tmp_path / "enveloped-results.json"
    results_path.write_text(json.dumps({
        "schema_version": 2,
        "run": run_envelope(evaluation_mode="live_lab"),
        "cases": live_cases(),
        "controls": healthy_controls(),
    }))
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["cases"] == 20
    assert output["run_metadata"]["evaluation_mode"] == "live_lab"
    assert output["run_metadata"]["evaluation_mode_is_declared_only"] is True
    assert output["run_metadata"]["execution_independently_verified"] is False


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"schema_version": 2, "cases": synthetic(),
             "controls": healthy_controls()},
            "requires a run envelope",
        ),
        (
            {"schema_version": 1, "run": run_envelope(),
             "cases": synthetic(), "controls": healthy_controls()},
            "requires schema_version 2",
        ),
        (
            {"schema_version": 2, "run": None,
             "cases": synthetic(), "controls": healthy_controls()},
            "requires a run envelope",
        ),
    ],
)
def test_results_cli_rejects_malformed_envelope_shape(
    tmp_path, monkeypatch, capsys, payload, message
):
    results_path = tmp_path / "bad-envelope.json"
    results_path.write_text(json.dumps(payload))
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_results_cli_rejects_digest_mismatch_without_traceback(
    tmp_path, monkeypatch, capsys
):
    envelope = run_envelope()
    envelope["scenario_matrix_sha256"] = "0" * 64
    results_path = tmp_path / "bad-digest-results.json"
    results_path.write_text(json.dumps({
        "schema_version": 2,
        "run": envelope,
        "cases": synthetic(),
        "controls": healthy_controls(),
    }))
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 2
    captured = capsys.readouterr()
    assert "does not match" in captured.err
    assert "Traceback" not in captured.err


def test_results_cli_rejects_nonmatching_matrix_bytes_without_traceback(
    tmp_path, monkeypatch, capsys
):
    matrix_path = tmp_path / "same-matrix-different-format.json"
    matrix_path.write_text(
        json.dumps(load_scenario_matrix(), indent=2) + " \n"
    )
    envelope = run_envelope()
    results_path = tmp_path / "matrix-bytes-mismatch.json"
    results_path.write_text(json.dumps({
        "schema_version": 2,
        "run": envelope,
        "cases": synthetic(),
        "controls": healthy_controls(),
    }))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_benchmark.py",
            "--results",
            str(results_path),
            "--scenarios",
            str(matrix_path),
        ],
    )

    assert run_benchmark.main() == 2
    captured = capsys.readouterr()
    assert "does not match" in captured.err
    assert "Traceback" not in captured.err


def test_results_cli_rejects_unsupported_mode_without_traceback(
    tmp_path, monkeypatch, capsys
):
    envelope = run_envelope(evaluation_mode="production")
    results_path = tmp_path / "unsupported-mode.json"
    results_path.write_text(json.dumps({
        "schema_version": 2,
        "run": envelope,
        "cases": synthetic(),
        "controls": healthy_controls(),
    }))
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 2
    captured = capsys.readouterr()
    assert "evaluation_mode" in captured.err
    assert "Traceback" not in captured.err


def test_results_cli_reports_unreadable_results_file_without_traceback(
    tmp_path, monkeypatch, capsys
):
    missing_path = tmp_path / "missing-results.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_benchmark.py", "--results", str(missing_path)],
    )

    assert run_benchmark.main() == 2
    captured = capsys.readouterr()
    assert "unable to read valid results JSON" in captured.err
    assert "Traceback" not in captured.err


def test_results_cli_rejects_malformed_optional_metadata_without_traceback(
    tmp_path, monkeypatch, capsys
):
    cases = synthetic()
    cases[0]["observed_target_id"] = {"pod": "target"}
    results_path = tmp_path / "malformed-metadata.json"
    results_path.write_text(json.dumps({
        "schema_version": 1,
        "cases": cases,
        "controls": healthy_controls(),
    }))
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 2
    captured = capsys.readouterr()
    assert "INCOMPLETE:" in captured.err
    assert "observed_target_id" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "contents",
    [
        "{malformed json",
        "[]",
        json.dumps({"cases": [{"injected_at": 123}], "controls": []}),
    ],
)
def test_results_cli_reports_malformed_input_without_traceback(
    tmp_path, monkeypatch, capsys, contents
):
    results_path = tmp_path / "bad-results.json"
    results_path.write_text(contents)
    monkeypatch.setattr(
        sys, "argv", ["run_benchmark.py", "--results", str(results_path)]
    )

    assert run_benchmark.main() == 2
    captured = capsys.readouterr()
    assert "INCOMPLETE:" in captured.err
    assert "Traceback" not in captured.err


def test_results_cli_reports_missing_scenario_matrix_without_traceback(
    tmp_path, monkeypatch, capsys
):
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps({
        "cases": synthetic(),
        "controls": healthy_controls(),
    }))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_benchmark.py",
            "--results",
            str(results_path),
            "--scenarios",
            str(tmp_path / "missing-scenarios.json"),
        ],
    )

    assert run_benchmark.main() == 2
    captured = capsys.readouterr()
    assert "INCOMPLETE:" in captured.err
    assert "Unable to load scenario matrix" in captured.err
    assert "Traceback" not in captured.err
