#!/usr/bin/env python3
"""Run isolated, exploratory Isolation Forest diagnostics on the frozen F.28 data."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from typing import Any

import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import (  # noqa: E402
    FEATURE_NAMES,
    TelemetryAnomalyDetector,
)
from scripts.evaluate_anomaly_benchmark import (  # noqa: E402
    AnomalyBenchmarkIncomplete,
    _timestamp,
    validate_comparative_anomaly_artifact,
)

DEFAULT_ARTIFACT = (
    PROJECT_ROOT
    / "benchmark"
    / "results"
    / "three-way-anomaly-benchmark-v4.json"
)
DEFAULT_BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "benchmark"
    / "results"
    / "isolation-forest-diagnostic-v1.json"
)
DIAGNOSTIC_SCHEMA_VERSION = 1
ESTIMATORS = 200
RANDOM_STATE = 42
CONTAMINATION_VARIANTS = (0.01, 0.10)
CONTROL_SCORE_TOLERANCE = 1e-12


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _integrity_digest(report: dict[str, Any]) -> str:
    payload = {key: value for key, value in report.items() if key != "integrity"}
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return _sha256(canonical)


def _read_inputs(
    artifact_path: str | Path,
    baseline_path: str | Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], str, str]:
    artifact_bytes = Path(artifact_path).read_bytes()
    try:
        artifact = json.loads(artifact_bytes)
    except json.JSONDecodeError as exc:
        raise AnomalyBenchmarkIncomplete(
            "Frozen comparative artifact is not valid JSON."
        ) from exc
    if artifact.get("schema_version") != 4:
        raise AnomalyBenchmarkIncomplete(
            "Diagnostics require the schema-4 comparative artifact."
        )
    validate_comparative_anomaly_artifact(artifact)

    baseline_bytes = Path(baseline_path).read_bytes()
    try:
        rows = list(csv.DictReader(baseline_bytes.decode("utf-8").splitlines()))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise AnomalyBenchmarkIncomplete(
            "Frozen baseline CSV is invalid."
        ) from exc
    if _sha256(baseline_bytes) != artifact["provenance"]["baseline_sha256"]:
        raise AnomalyBenchmarkIncomplete(
            "Baseline digest does not match the schema-4 artifact."
        )
    expected_columns = {"timestamp", "service", *FEATURE_NAMES}
    if not rows or not expected_columns.issubset(rows[0]):
        raise AnomalyBenchmarkIncomplete(
            "Frozen baseline is missing required columns."
        )
    return (
        artifact,
        rows,
        _sha256(artifact_bytes),
        _sha256(baseline_bytes),
    )


def _feature_values(row: dict[str, str]) -> list[float]:
    try:
        values = [float(row[name]) for name in FEATURE_NAMES]
    except (KeyError, TypeError, ValueError) as exc:
        raise AnomalyBenchmarkIncomplete(
            "Baseline row has invalid feature values."
        ) from exc
    if not all(math.isfinite(value) for value in values):
        raise AnomalyBenchmarkIncomplete(
            "Baseline row contains non-finite feature values."
        )
    return values


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "minimum": None,
            "median": None,
            "maximum": None,
            "mean": None,
        }
    return {
        "count": len(values),
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
        "mean": statistics.fmean(values),
    }


def _decision_summary(
    predictions: list[dict[str, Any]],
) -> dict[str, Any]:
    tp = sum(
        item["ground_truth_anomaly"] and item["anomaly"]
        for item in predictions
    )
    fp = sum(
        not item["ground_truth_anomaly"] and item["anomaly"]
        for item in predictions
    )
    tn = sum(
        not item["ground_truth_anomaly"] and not item["anomaly"]
        for item in predictions
    )
    fn = sum(
        item["ground_truth_anomaly"] and not item["anomaly"]
        for item in predictions
    )
    return {
        "eligible_observation_count": len(predictions),
        "true_positives": tp,
        "false_positives": fp,
        "true_negatives": tn,
        "false_negatives": fn,
        "predicted_anomaly_count": tp + fp,
    }


def _git_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def build_isolation_forest_diagnostic(
    *,
    artifact_path: str | Path = DEFAULT_ARTIFACT,
    baseline_path: str | Path = DEFAULT_BASELINE,
) -> dict[str, Any]:
    artifact, baseline_rows, artifact_sha256, baseline_sha256 = _read_inputs(
        artifact_path, baseline_path
    )
    provenance = artifact["provenance"]
    if provenance.get("feature_order") != list(FEATURE_NAMES):
        raise AnomalyBenchmarkIncomplete(
            "Artifact feature order does not match the detector interface."
        )

    training_timestamps = set(
        provenance["split"]["training_timestamps"]
    )
    services = provenance["services"]
    training_by_service: dict[str, list[list[float]]] = {
        service: [] for service in services
    }
    training_rows_by_service: dict[str, list[dict[str, str]]] = {
        service: [] for service in services
    }
    for row in baseline_rows:
        service = row.get("service")
        if service not in training_by_service:
            continue
        try:
            timestamp = _timestamp(datetime.fromisoformat(row["timestamp"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AnomalyBenchmarkIncomplete(
                "Baseline row has an invalid timestamp."
            ) from exc
        if timestamp in training_timestamps:
            training_rows_by_service[service].append(row)
            training_by_service[service].append(_feature_values(row))

    expected_per_service = provenance["split"]["per_service"]
    for service in services:
        if len(training_by_service[service]) != expected_per_service[service][
            "training_row_count"
        ]:
            raise AnomalyBenchmarkIncomplete(
                f"Training rows do not match artifact split for {service}."
            )

    observations = [
        sample for sample in artifact["samples"]
        if sample["status"] == "evaluated"
    ]
    if not observations:
        raise AnomalyBenchmarkIncomplete(
            "The frozen artifact has no eligible observations."
        )
    if any(sample["service"] not in training_by_service for sample in observations):
        raise AnomalyBenchmarkIncomplete(
            "An evaluation sample has no per-service training set."
        )

    baseline_by_service_time = {
        (
            row["service"],
            _timestamp(datetime.fromisoformat(row["timestamp"])),
        ): row
        for row in baseline_rows
    }
    for sample in observations:
        key = (sample["service"], sample["timestamp"])
        baseline_row = baseline_by_service_time.get(key)
        if sample["scenario"] == "chronological_baseline_holdout_assumed_normal":
            if baseline_row is None or any(
                sample["features"][feature]
                != float(baseline_row[feature])
                for feature in FEATURE_NAMES
            ):
                raise AnomalyBenchmarkIncomplete(
                    "Saved baseline holdout does not match frozen CSV."
                )

    model_specs: list[dict[str, Any]] = [
        {
            "name": "unscaled_control",
            "preprocessing": "none",
            "contamination": 0.05,
            "variable_changed": "none; reproduces schema-4 configuration",
        },
        {
            "name": "standardized_diagnostic",
            "preprocessing": "StandardScaler fit on training rows per service",
            "contamination": 0.05,
            "variable_changed": "feature scaling only",
        },
        *[
            {
                "name": f"unscaled_contamination_{contamination:.2f}",
                "preprocessing": "none",
                "contamination": contamination,
                "variable_changed": "contamination only",
            }
            for contamination in CONTAMINATION_VARIANTS
        ],
    ]

    predictions_by_method: dict[str, list[dict[str, Any]]] = {
        spec["name"]: [] for spec in model_specs
    }
    feature_training_values: dict[str, dict[str, list[float]]] = {
        service: {
            feature: [
                values[index]
                for values in training_by_service[service]
            ]
            for index, feature in enumerate(FEATURE_NAMES)
        }
        for service in services
    }
    for service in services:
        train = np.asarray(training_by_service[service], dtype=float)
        service_observations = [
            item for item in observations if item["service"] == service
        ]
        evaluation = np.asarray(
            [
                [item["features"][feature] for feature in FEATURE_NAMES]
                for item in service_observations
            ],
            dtype=float,
        )

        control = TelemetryAnomalyDetector(
            contamination=0.05,
            random_state=RANDOM_STATE,
        )
        control.fit(train)
        control_results = control.score(evaluation)

        for spec in model_specs:
            name = spec["name"]
            if name == "unscaled_control":
                results = control_results
            else:
                variant_train = train
                variant_evaluation = evaluation
                scaler = None
                if spec["preprocessing"] != "none":
                    scaler = StandardScaler()
                    variant_train = scaler.fit_transform(train)
                    variant_evaluation = scaler.transform(evaluation)
                model = IsolationForest(
                    n_estimators=ESTIMATORS,
                    contamination=spec["contamination"],
                    random_state=RANDOM_STATE,
                )
                model.fit(variant_train)
                flags = model.predict(variant_evaluation)
                scores = model.decision_function(variant_evaluation)
                results = [
                    {
                        "anomaly": bool(flag == -1),
                        "anomaly_score": float(score),
                    }
                    for flag, score in zip(flags, scores)
                ]
            for sample, result in zip(service_observations, results):
                predictions_by_method[name].append({
                    "sample_id": sample["sample_id"],
                    "service": sample["service"],
                    "scenario": sample["scenario"],
                    "timestamp": sample["timestamp"],
                    "ground_truth_anomaly": sample["ground_truth_anomaly"],
                    "anomaly": result["anomaly"],
                    "decision_function": result["anomaly_score"],
                })

    saved_control = {
        sample["sample_id"]: sample for sample in observations
    }
    control_predictions = predictions_by_method["unscaled_control"]
    flag_matches = sum(
        saved_control[prediction["sample_id"]]["predictions"][
            "isolation_forest"
        ]["anomaly"] == prediction["anomaly"]
        for prediction in control_predictions
    )
    score_deltas = [
        abs(
            saved_control[prediction["sample_id"]]["isolation_forest_score"]
            - prediction["decision_function"]
        )
        for prediction in control_predictions
    ]

    method_reports = {}
    for spec in model_specs:
        name = spec["name"]
        method_predictions = predictions_by_method[name]
        by_group: dict[tuple[str, bool], list[dict[str, Any]]] = {}
        for prediction in method_predictions:
            key = (
                prediction["scenario"],
                prediction["ground_truth_anomaly"],
            )
            by_group.setdefault(key, []).append(prediction)
        method_reports[name] = {
            "configuration": {
                **spec,
                "n_estimators": ESTIMATORS,
                "random_state": RANDOM_STATE,
                "fit_scope": "separate per service on the same 42-row training split",
            },
            "decision_summary": _decision_summary(method_predictions),
            "score_distributions_by_scenario_and_label": [
                {
                    "scenario": scenario,
                    "ground_truth_anomaly": label,
                    **_distribution([
                        item["decision_function"] for item in group
                    ]),
                    "anomaly_flag_count": sum(
                        item["anomaly"] for item in group
                    ),
                }
                for (scenario, label), group in sorted(by_group.items())
            ],
            "predictions": method_predictions,
        }

    feature_profiles = []
    grouped_observations: dict[tuple[str, bool], list[dict[str, Any]]] = {}
    for sample in observations:
        key = (sample["scenario"], sample["ground_truth_anomaly"])
        grouped_observations.setdefault(key, []).append(sample)
    for (scenario, label), group in sorted(grouped_observations.items()):
        for feature in FEATURE_NAMES:
            values = [item["features"][feature] for item in group]
            training_values = [
                feature_training_values[item["service"]][feature]
                for item in group
            ]
            outside_count = sum(
                value < min(training) or value > max(training)
                for value, training in zip(values, training_values)
            )
            feature_profiles.append({
                "scenario": scenario,
                "ground_truth_anomaly": label,
                "feature": feature,
                **_distribution(values),
                "outside_per_service_training_range_count": outside_count,
            })

    report = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "diagnostic": "exploratory_isolation_forest_zero_recall",
        "source_artifact": {
            "path": Path(artifact_path).name,
            "schema_version": artifact["schema_version"],
            "sha256": artifact_sha256,
        },
        "inputs": {
            "baseline_path": Path(baseline_path).name,
            "baseline_sha256": baseline_sha256,
            "dataset_sha256": artifact["provenance"]["dataset_sha256"],
            "eligible_observation_count": len(observations),
            "unavailable_observation_count": sum(
                sample["status"] == "unavailable"
                for sample in artifact["samples"]
            ),
            "features_in_model_order": list(FEATURE_NAMES),
            "services": list(services),
            "training_split": artifact["provenance"]["split"],
            "same_saved_evaluation_samples_for_all_variants": True,
            "label_assumptions": artifact["provenance"]["label_assumptions"],
        },
        "environment": {
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "scikit_learn_version": sklearn.__version__,
            "code_revision": _git_revision(),
            "code_revision_scope": (
                "HEAD only; does not identify uncommitted working-tree changes"
            ),
        },
        "control_reproduction": {
            "saved_v4_flag_matches": flag_matches,
            "eligible_observation_count": len(control_predictions),
            "all_flags_match": flag_matches == len(control_predictions),
            "maximum_absolute_decision_function_delta": max(score_deltas),
            "scores_match_within_1e-12": (
                max(score_deltas) <= CONTROL_SCORE_TOLERANCE
            ),
        },
        "method_reports": method_reports,
        "feature_value_profiles_vs_training_ranges": feature_profiles,
        "interpretation_limits": [
            "All positive labels are synthetic and normal labels are assumed; none are independently verified operational labels.",
            "All variants are exploratory diagnostics on the same frozen fixture; results are not unbiased performance estimates.",
            "No variant is selected or recommended from these held-out labels.",
            "Standardization is a diagnostic-only change to preprocessing, not a runtime proposal.",
            "Contamination variants change the model decision threshold behavior and are diagnostics, not threshold tuning.",
            "Unscaled Isolation Forest decision_function values are joint-model scores, not feature-attributed scores.",
            "Integrity hashes detect accidental or uncoordinated changes but do not authenticate authorship or prove detector execution.",
        ],
    }
    report["integrity"] = {
        "algorithm": "sha256",
        "canonicalization": (
            "UTF-8 JSON with sorted keys, compact separators, and no NaN; "
            "digest covers the complete report except the integrity object"
        ),
        "payload_sha256": _integrity_digest(report),
    }
    return report


def validate_diagnostic_report(report: Any) -> None:
    if not isinstance(report, dict) or report.get("schema_version") != (
        DIAGNOSTIC_SCHEMA_VERSION
    ):
        raise AnomalyBenchmarkIncomplete(
            "Diagnostic report schema is invalid."
        )
    integrity = report.get("integrity")
    if (
        not isinstance(integrity, dict)
        or integrity.get("algorithm") != "sha256"
    ):
        raise AnomalyBenchmarkIncomplete(
            "Diagnostic report integrity digest does not match payload."
        )
    try:
        expected_digest = _integrity_digest(report)
    except (TypeError, ValueError) as exc:
        raise AnomalyBenchmarkIncomplete(
            "Diagnostic report payload cannot be canonically encoded."
        ) from exc
    if integrity.get("payload_sha256") != expected_digest:
        raise AnomalyBenchmarkIncomplete(
            "Diagnostic report integrity digest does not match payload."
        )
    methods = report.get("method_reports")
    if not isinstance(methods, dict) or set(methods) != {
        "unscaled_control",
        "standardized_diagnostic",
        "unscaled_contamination_0.01",
        "unscaled_contamination_0.10",
    }:
        raise AnomalyBenchmarkIncomplete(
            "Diagnostic report method set is invalid."
        )
    inputs = report.get("inputs")
    declared_count = (
        inputs.get("eligible_observation_count")
        if isinstance(inputs, dict)
        else None
    )
    if type(declared_count) is not int or declared_count <= 0:
        raise AnomalyBenchmarkIncomplete(
            "Diagnostic eligible observation count is invalid."
        )
    baseline_predictions: dict[str, tuple[Any, ...]] | None = None
    for method in methods.values():
        if not isinstance(method, dict):
            raise AnomalyBenchmarkIncomplete(
                "Diagnostic method report is invalid."
            )
        predictions = method.get("predictions")
        if (
            not isinstance(predictions, list)
            or len(predictions) != declared_count
        ):
            raise AnomalyBenchmarkIncomplete(
                "Diagnostic method prediction count is invalid."
            )
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("sample_id"), str)
            or not item["sample_id"]
            or not isinstance(item.get("service"), str)
            or not isinstance(item.get("scenario"), str)
            or not isinstance(item.get("timestamp"), str)
            or not isinstance(item.get("ground_truth_anomaly"), bool)
            for item in predictions
        ):
            raise AnomalyBenchmarkIncomplete(
                "Diagnostic prediction identity is invalid."
            )
        current_ids = {item["sample_id"] for item in predictions}
        if len(current_ids) != len(predictions):
            raise AnomalyBenchmarkIncomplete(
                "Diagnostic predictions contain duplicate sample IDs."
            )
        current_metadata = {
            item["sample_id"]: (
                item["service"],
                item["scenario"],
                item["timestamp"],
                item["ground_truth_anomaly"],
            )
            for item in predictions
        }
        if baseline_predictions is None:
            baseline_predictions = current_metadata
        elif current_metadata != baseline_predictions:
            raise AnomalyBenchmarkIncomplete(
                "Diagnostic methods do not use identical evaluation inputs."
            )
        if any(
            not isinstance(item.get("ground_truth_anomaly"), bool)
            or not isinstance(item.get("anomaly"), bool)
            or isinstance(item.get("decision_function"), bool)
            or not isinstance(item.get("decision_function"), (int, float))
            or not math.isfinite(item["decision_function"])
            or item["anomaly"] != (item["decision_function"] < 0)
            for item in predictions
        ):
            raise AnomalyBenchmarkIncomplete(
                "Diagnostic score and prediction payload is inconsistent."
            )
        if method.get("decision_summary") != _decision_summary(predictions):
            raise AnomalyBenchmarkIncomplete(
                "Diagnostic summary does not match saved predictions."
            )
    reproduction = report.get("control_reproduction")
    expected_count = len(baseline_predictions or {})
    matched_count = (
        reproduction.get("saved_v4_flag_matches")
        if isinstance(reproduction, dict)
        else None
    )
    reported_count = (
        reproduction.get("eligible_observation_count")
        if isinstance(reproduction, dict)
        else None
    )
    max_score_delta = (
        reproduction.get("maximum_absolute_decision_function_delta")
        if isinstance(reproduction, dict)
        else None
    )
    scores_match = (
        reproduction.get("scores_match_within_1e-12")
        if isinstance(reproduction, dict)
        else None
    )
    if (
        not isinstance(reproduction, dict)
        or type(reported_count) is not int
        or reported_count != expected_count
        or reported_count != declared_count
        or expected_count <= 0
        or type(matched_count) is not int
        or matched_count < 0
        or matched_count > expected_count
        or matched_count != expected_count
        or reproduction.get("all_flags_match") is not True
        or type(max_score_delta) not in (int, float)
        or not math.isfinite(max_score_delta)
        or max_score_delta < 0
        or type(scores_match) is not bool
        or scores_match
        != (max_score_delta <= CONTROL_SCORE_TOLERANCE)
        or scores_match is not True
    ):
        raise AnomalyBenchmarkIncomplete(
            "Control reproduction summary is inconsistent or did not pass."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument(
        "--validate",
        metavar="REPORT",
        help="Validate an existing diagnostic report without fitting models.",
    )
    args = parser.parse_args()

    try:
        if args.validate:
            report_path = Path(args.validate)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            validate_diagnostic_report(report)
            print(f"VALID: diagnostic report integrity and invariants match {report_path}")
            return 0
        output_path = Path(args.output)
        if output_path.exists():
            raise FileExistsError(f"File exists: {output_path}")
        report = build_isolation_forest_diagnostic(
            artifact_path=args.artifact,
            baseline_path=args.baseline,
        )
        validate_diagnostic_report(report)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(report, indent=2) + "\n")
    except (
        AnomalyBenchmarkIncomplete,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 2

    print(f"Diagnostic report: {output_path}")
    print(
        "Control reproduces saved flags: "
        f"{report['control_reproduction']['all_flags_match']}; "
        "this is an exploratory synthetic diagnostic, not a runtime change."
    )
    for name, result in report["method_reports"].items():
        summary = result["decision_summary"]
        print(
            f"{name}: TP={summary['true_positives']} "
            f"FP={summary['false_positives']} "
            f"TN={summary['true_negatives']} "
            f"FN={summary['false_negatives']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
