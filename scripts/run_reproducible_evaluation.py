#!/usr/bin/env python3
"""Generate a unique offline evaluation bundle from existing benchmark code."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import uuid
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_anomaly_benchmark import (  # noqa: E402
    BASELINE,
    build_comparative_anomaly_benchmark,
    validate_anomaly_artifact,
)
from scripts.run_synthetic_benchmark import (  # noqa: E402
    build_synthetic_benchmark,
    validate_synthetic_artifact,
)

DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "benchmark" / "results" / "evaluation-runs"
NOT_MEASURED = "not_measured"


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() or None


def _worktree_metadata() -> dict[str, Any]:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(PROJECT_ROOT), "status", "--porcelain",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"state": "unknown", "status_sha256": None}
    encoded = result.stdout.encode("utf-8")
    return {
        "state": "dirty" if result.stdout.strip() else "clean",
        "status_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _package_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def _metric_from_confusion(confusion: dict[str, Any]) -> dict[str, Any]:
    tp = confusion["true_positives"]
    fp = confusion["false_positives"]
    tn = confusion["true_negatives"]
    fn = confusion["false_negatives"]
    count = confusion["eligible_observation_count"]
    return {
        "status": "measured" if count else NOT_MEASURED,
        "sample_count": count,
        "confusion_matrix": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "precision": {
            "value": confusion["precision"],
            "numerator": tp,
            "denominator": tp + fp,
            "formula": "TP / (TP + FP)",
        },
        "recall": {
            "value": confusion["recall"],
            "numerator": tp,
            "denominator": tp + fn,
            "formula": "TP / (TP + FN)",
        },
        "f1": {
            "value": confusion["f1"],
            "numerator": 2 * tp,
            "denominator": 2 * tp + fp + fn,
            "formula": "2 * TP / (2 * TP + FP + FN)",
        },
        "false_positive_rate": {
            "value": confusion["false_positive_rate"],
            "numerator": fp,
            "denominator": fp + tn,
            "formula": "FP / (FP + TN)",
        },
        "true_positive_rate": {
            "value": confusion["recall"],
            "numerator": tp,
            "denominator": tp + fn,
            "formula": "TP / (TP + FN)",
        },
    }


def compose_report(
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
    environment: dict[str, Any],
    anomaly_artifact: dict[str, Any],
    synthetic_artifact: dict[str, Any],
) -> dict[str, Any]:
    anomaly_metrics = anomaly_artifact["metrics"]
    methods = {
        name: _metric_from_confusion(confusion)
        for name, confusion in anomaly_metrics["point_anomaly"].items()
    }
    feature_attribution: Counter[str] = Counter()
    for sample in anomaly_artifact["samples"]:
        for feature in sample.get("mad_actionable_features") or []:
            feature_attribution[f"actionable:{feature}"] += 1
        for feature in sample.get("mad_informational_features") or []:
            feature_attribution[f"informational:{feature}"] += 1

    summary = synthetic_artifact["summary"]
    synthetic_metrics = synthetic_artifact["metrics"]
    return {
        "schema_version": 1,
        "run": {
            "run_id": run_id,
            "started_at_utc": started_at,
            "completed_at_utc": completed_at,
            "evaluation_mode": "offline_synthetic",
            "live_execution_verified": False,
        },
        "environment": environment,
        "datasets": {
            "anomaly": {
                "identifier": anomaly_artifact["benchmark"],
                "baseline_sha256": anomaly_artifact["provenance"][
                    "baseline_sha256"
                ],
                "dataset_sha256": anomaly_artifact["provenance"][
                    "dataset_sha256"
                ],
                "total_observations": anomaly_metrics[
                    "total_observation_count"
                ],
                "eligible_observations": anomaly_metrics[
                    "eligible_observation_count"
                ],
                "unavailable_observations": anomaly_metrics[
                    "unavailable_observation_count"
                ],
                "features": anomaly_artifact["provenance"]["features"],
                "services": anomaly_artifact["provenance"]["services"],
                "label_provenance": anomaly_artifact["reproducibility"][
                    "label_provenance"
                ],
                "configuration": anomaly_artifact["reproducibility"][
                    "configuration"
                ],
                "random_seeds": anomaly_artifact["reproducibility"][
                    "random_seeds"
                ],
            },
            "incident_matrix": {
                "scenario_count": len(synthetic_artifact["cases"]),
                "control_count": len(synthetic_artifact["controls"]),
                "evaluation_mode": "synthetic deterministic fixtures",
                "live_faults_injected": False,
            },
        },
        "anomaly_detection": {
            "status": "measured_synthetic",
            "methods": methods,
            "per_service": anomaly_metrics["per_service"],
            "per_feature_attribution_counts": dict(
                sorted(feature_attribution.items())
            ),
            "per_feature_classification_metrics": {
                "status": NOT_MEASURED,
                "numerator": None,
                "denominator": None,
                "formula": "feature-level TP / feature-level labeled samples",
                "reason": (
                    "The dataset has anomaly-level labels, not independent "
                    "feature-level ground-truth labels."
                ),
            },
            "static_threshold_comparison": {
                "status": NOT_MEASURED,
                "numerator": None,
                "denominator": None,
                "formula": "TP / (TP + FP), Recall, F1, and FPR at prespecified static thresholds",
                "reason": (
                    "No static-threshold baseline with documented thresholds "
                    "selected before observing this evaluation was configured. "
                    "Choosing one after inspecting these outcomes would be "
                    "post-hoc tuning."
                ),
            },
            "false_positive_rate_definition": "FP / (FP + TN) over evaluated points labeled normal.",
            "limitations": [
                "Positive labels are synthetic by construction.",
                "Chronological baseline holdout is assumed normal and is not independently reviewed.",
                "The four services share generated scenario families; they are not independent incident populations.",
                "Isolation Forest and hybrid are comparison methods, not the application runtime detector.",
            ],
        },
        "operational_incident_workflow": {
            "status": "partial_synthetic_contract_evaluation",
            "synthetic_classifier": summary["classification"],
            "synthetic_diagnosis_availability": summary[
                "diagnosis_availability"
            ],
            "synthetic_policy_contract": summary["policy_contract_match"],
            "synthetic_policy_decision_counts": summary[
                "policy_contract_match"
            ]["decision_counts"],
            "synthetic_scenario_results": [
                {
                    "scenario_id": case["scenario_id"],
                    "expected_category": case["expected_category"],
                    "predicted_category": case["observed_category"],
                    "detection_result": case["detection_result"],
                    "diagnosis_status": case["diagnosis_status"],
                    "policy_evaluation": case["policy_evaluation"],
                    "remediation_outcome": case["remediation_outcome"],
                    "failure_reason": case["failure_reason"],
                }
                for case in synthetic_artifact["cases"]
            ],
            "synthetic_actions_executed": 0,
            "metrics": {
                "live_detection_mttd": {
                    "status": NOT_MEASURED,
                    "mean_seconds": None,
                    "median_seconds": None,
                    "p95_seconds": None,
                    "minimum_seconds": None,
                    "maximum_seconds": None,
                    "sample_count": 0,
                    "numerator": None,
                    "denominator": None,
                    "formula": "mean(first persisted incident monotonic time - successful injection monotonic time)",
                    "reason": "No controlled lab fault was injected or observed in this run.",
                },
                "live_incident_persistence": {
                    "status": NOT_MEASURED,
                    "success_rate": None,
                    "sample_count": 0,
                    "numerator": None,
                    "denominator": None,
                    "formula": "incidents persisted with a valid ID / successfully injected faults",
                    "reason": "The deterministic fixture benchmark does not run the live persistence path.",
                },
                "live_end_to_end_investigation": {
                    "status": NOT_MEASURED,
                    "success_rate": None,
                    "sample_count": 0,
                    "numerator": None,
                    "denominator": None,
                    "formula": "incidents completing required investigation stages / eligible detected incidents",
                    "reason": "No live incidents were created, persisted, or investigated.",
                },
                "approved_remediation_verification": {
                    "status": NOT_MEASURED,
                    "success_rate": None,
                    "sample_count": 0,
                    "numerator": None,
                    "denominator": None,
                    "formula": "verified recoveries / authorized remediation executions",
                    "reason": "No remediation was authorized or executed.",
                },
            },
            "synthetic_metrics": synthetic_metrics,
            "caveat": (
                "Synthetic classifier and policy fixture outcomes are not "
                "operational detection, persistence, or end-to-end workflow rates."
            ),
        },
        "remediation_safety": {
            "status": NOT_MEASURED,
            "total_safety_cases": None,
            "defined_scenario_count": 12,
            "scenarios_executed_by_report_runner": 0,
            "unsafe_requests_blocked": None,
            "unsafe_requests_incorrectly_allowed": None,
            "false_blocks_of_valid_authorized_requests": None,
            "safety_rejection_rate": None,
            "unauthorized_action_escape_count": None,
            "formula": "blocked unsafe requests / executed unsafe test cases",
            "numerator": None,
            "denominator": None,
            "live_or_test_remediation_operations_executed": 0,
            "reason": (
                "This offline benchmark does not execute the remediation API "
                "safety case suite. Unit-test coverage must not be interpreted "
                "as an empirical safety rate. No live or test remediation "
                "operation was executed by this runner."
            ),
            "related_test_modules": [
                "tests/test_api_security.py",
                "tests/test_operations_api.py",
                "tests/test_policy_engine.py",
                "tests/test_incident_response_graph.py",
                "tests/test_remediation_service.py",
                "tests/test_verification.py",
            ],
            "covered_safety_gates": [
                "missing or invalid operator JWT",
                "insufficient operator permissions",
                "missing or invalid authenticated operator authorization",
                "missing or pending human approval",
                "stale evidence",
                "mismatched incident/resource identity",
                "out-of-scope Kubernetes namespace",
                "unsupported remediation action",
                "resolved or nonexistent incident",
                "duplicate approval or execution request",
                "failed preconditions",
                "failed post-remediation verification",
            ],
        },
        "rca_quality": {
            "status": "NOT_MEASURED",
            "sample_count": 0,
            "json_validity_rate": None,
            "evidence_grounding_rate": None,
            "citation_resolution_rate": None,
            "insufficient_evidence_accuracy": None,
            "retry_count": None,
            "latency_mean_ms": None,
            "latency_p95_ms": None,
            "token_count_per_call": None,
            "numerator": None,
            "denominator": None,
            "formula": "rubric-scored supported RCA outputs / adjudicated labeled RCA outputs",
            "reason": (
                "No LLM provider was called; the RCA reference labels are "
                "explicitly unverified."
            ),
        },
        "api_latency": {
            "status": NOT_MEASURED,
            "request_count": 0,
            "concurrency": 0,
            "failure_count": 0,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "numerator": None,
            "denominator": None,
            "formula": "latency percentiles over successful read-only API requests",
            "reason": (
                "No API server was exercised and no repeatable request "
                "workload was run."
            ),
        },
        "metric_definitions": {
            "anomaly_confusion": "TP/FP/TN/FN are computed only over evaluated observations with synthetic or assumed-normal labels.",
            "false_positive_rate": "FP / (FP + TN); denominator contains evaluated observations labeled normal.",
            "detection_latency": "Live MTTD uses monotonic timestamps paired by unique trial_id; UTC timestamps are retained for audit only.",
            "missing_data": "Unavailable observations, unmeasured stages, and absent denominators remain explicitly unavailable; they are not treated as negatives or successes.",
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    anomaly = report["anomaly_detection"]
    anomaly_dataset = report["datasets"]["anomaly"]
    workflow = report["operational_incident_workflow"]
    lines = [
        "# Reproducible Kybernaut Evaluation",
        "",
        f"- Run ID: `{run['run_id']}`",
        f"- Started: `{run['started_at_utc']}`",
        f"- Completed: `{run['completed_at_utc']}`",
        f"- Mode: `{run['evaluation_mode']}` (live execution verified: no)",
        f"- Revision: `{report['environment']['git_revision'] or 'not available'}`",
        f"- Worktree: `{report['environment']['worktree']['state']}`",
        "",
        "## Anomaly detection (synthetic/assumed labels)",
        "",
        f"Dataset: `{anomaly_dataset['identifier']}`, "
        f"{anomaly_dataset['eligible_observations']}/"
        f"{anomaly_dataset['total_observations']} eligible observations; "
        f"{anomaly_dataset['unavailable_observations']} unavailable. "
        f"Services: {', '.join(anomaly_dataset['services'])}.",
        "",
        "| Method | TP | FP | TN | FN | Precision | Recall/TPR | F1 | FPR | n |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, metrics in anomaly["methods"].items():
        counts = metrics["confusion_matrix"]
        lines.append(
            f"| {name} | {counts['tp']} | {counts['fp']} | {counts['tn']} "
            f"| {counts['fn']} | {metrics['precision']['value']} "
            f"| {metrics['recall']['value']} | {metrics['f1']['value']} "
            f"| {metrics['false_positive_rate']['value']} "
            f"| {metrics['sample_count']} |"
        )
    lines.extend([
        "",
        "FPR is `FP / (FP + TN)` over evaluated observations labeled normal. "
        "Synthetic positives and assumed-normal holdout/control points are "
        "not independent operational ground truth.",
        "",
        "### Per-service results",
        "",
        "| Service | Method | TP | FP | TN | FN | Precision | Recall | F1 | FPR | n |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for service, service_data in anomaly["per_service"].items():
        for method, confusion in service_data["point_anomaly"].items():
            lines.append(
                f"| {service} | {method} | {confusion['true_positives']} "
                f"| {confusion['false_positives']} "
                f"| {confusion['true_negatives']} "
                f"| {confusion['false_negatives']} "
                f"| {confusion['precision']} | {confusion['recall']} "
                f"| {confusion['f1']} | {confusion['false_positive_rate']} "
                f"| {confusion['eligible_observation_count']} |"
            )
    lines.extend([
        "",
        "Per-feature attribution counts are descriptive only; feature-level "
        "precision/recall/F1 are not measured because no feature-level labels "
        "exist.",
        "",
        "## Incident and policy evaluation",
        "",
        f"- Synthetic classifier: "
        f"`{json.dumps(workflow['synthetic_classifier'], sort_keys=True)}`",
        f"- Synthetic diagnosis fixture availability: "
        f"`{json.dumps(workflow['synthetic_diagnosis_availability'], sort_keys=True)}`",
        f"- Synthetic policy contract: "
        f"`{json.dumps(workflow['synthetic_policy_contract'], sort_keys=True)}`",
        f"- Synthetic policy decisions: "
        f"`{json.dumps(workflow['synthetic_policy_decision_counts'], sort_keys=True)}`",
        "- Live MTTD, persistence, end-to-end investigation, and authorized "
        "remediation verification: **not measured**.",
        "- No faults were injected and no actions were executed.",
        "",
        "### Synthetic scenario records",
        "",
        "| Scenario | Expected | Predicted | Detection | Diagnosis fixture | Policy | Remediation |",
        "|---|---|---|---|---|---|---|",
    ])
    for case in workflow["synthetic_scenario_results"]:
        policy = case["policy_evaluation"].get(
            "actual_decision", case["policy_evaluation"].get("status")
        )
        lines.append(
            f"| {case['scenario_id']} | {case['expected_category']} "
            f"| {case['predicted_category']} | {case['detection_result']} "
            f"| {case['diagnosis_status']} | {policy} "
            f"| {case['remediation_outcome']} |"
        )
    lines.extend([
        "",
        "## Other metrics",
        "",
        "| Metric | Status | Reason |",
        "|---|---|---|",
        f"| Remediation safety rate | {report['remediation_safety']['status']} "
        f"| {report['remediation_safety']['reason']} |",
        f"| RCA quality | {report['rca_quality']['status']} "
        f"| {report['rca_quality']['reason']} |",
        f"| API latency | {report['api_latency']['status']} "
        f"| {report['api_latency']['reason']} |",
        "",
        "## Reproducibility and limitations",
        "",
        f"- Baseline SHA-256: `{anomaly_dataset['baseline_sha256']}`",
        f"- Dataset SHA-256: `{anomaly_dataset['dataset_sha256']}`",
        f"- Configuration: `{anomaly_dataset['configuration']}`",
        f"- Random seeds: `{anomaly_dataset['random_seeds']}`",
        "- This report is from local synthetic fixtures, not production.",
        "- It does not reproduce live fault detection, real incident persistence, "
        "or remediation safety outcomes.",
        "- See the JSON report for complete per-service metrics, evidence "
        "labels, formulas, and explicit unmeasured reasons.",
        "",
    ])
    return "\n".join(lines)


def render_resume_metrics(report: dict[str, Any]) -> str:
    methods = report["anomaly_detection"]["methods"]
    mad = methods["direction_aware_mad"]
    dataset = report["datasets"]["anomaly"]
    return (
        "# Evidence-backed evaluation summary\n\n"
        "- Reproduced the direction-aware MAD policy on "
        f"{dataset['eligible_observations']} eligible synthetic/assumed-labeled "
        f"telemetry points across {len(dataset['services'])} services "
        f"(TP={mad['confusion_matrix']['tp']}, "
        f"FP={mad['confusion_matrix']['fp']}, "
        f"TN={mad['confusion_matrix']['tn']}, "
        f"FN={mad['confusion_matrix']['fn']}; "
        f"F1={mad['f1']['value']}, "
        f"FPR={mad['false_positive_rate']['value']}).\n"
        "- Compared Isolation Forest, direction-aware MAD, and their union "
        "on the same labeled sample set; see the report for separate "
        "per-service confusion matrices.\n\n"
        "These are synthetic/assumed-label measurements, not production "
        "performance. No live incident, LLM RCA, API latency, or remediation "
        "safety rate is claimed.\n"
    )


def run_evaluation(output_root: Path = DEFAULT_OUTPUT_ROOT) -> Path:
    started = datetime.now(timezone.utc)
    run_id = (
        "evaluation-" + started.strftime("%Y%m%dT%H%M%S")
        + "Z-" + uuid.uuid4().hex[:8]
    )
    anomaly_artifact = build_comparative_anomaly_benchmark()
    validate_anomaly_artifact(anomaly_artifact)
    synthetic_artifact = build_synthetic_benchmark(
        run_id=f"{run_id}-synthetic",
        now=started,
    )
    validate_synthetic_artifact(synthetic_artifact)
    completed = datetime.now(timezone.utc)

    try:
        baseline_sha256 = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError("Unable to hash the validated anomaly baseline.") from exc
    report = compose_report(
        run_id=run_id,
        started_at=_utc(started),
        completed_at=_utc(completed),
        environment={
            "operating_system": platform.platform(),
            "python_version": platform.python_version(),
            "numpy_version": _package_version("numpy"),
            "scikit_learn_version": _package_version("scikit-learn"),
            "git_revision": _git_revision(),
            "worktree": _worktree_metadata(),
            "baseline_file_sha256": baseline_sha256,
        },
        anomaly_artifact=anomaly_artifact,
        synthetic_artifact=synthetic_artifact,
    )
    report["run"]["started_at_utc"] = _utc(started)
    report["run"]["completed_at_utc"] = _utc(completed)
    destination = output_root / run_id
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "evaluation_report.json").write_text(
        json.dumps(report, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (destination / "anomaly_comparison.json").write_text(
        json.dumps(anomaly_artifact, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (destination / "synthetic_incident_benchmark.json").write_text(
        json.dumps(synthetic_artifact, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    (destination / "evaluation_report.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    (destination / "resume_metrics.md").write_text(
        render_resume_metrics(report), encoding="utf-8"
    )
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Parent for a new unique report directory.",
    )
    args = parser.parse_args(argv)
    try:
        destination = run_evaluation(args.output_root)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"EVALUATION INCOMPLETE: {exc}", file=sys.stderr)
        return 2
    print(f"Evaluation report written to {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
