#!/usr/bin/env python3
"""Evaluate the active direction-aware MAD policy on synthetic point labels."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import (  # noqa: E402
    FEATURE_NAMES,
    TelemetryAnomalyDetector,
)
from app.services.anomaly_policy import (  # noqa: E402
    ACTIONABLE_HIGH_FEATURES,
    OPERATIONAL_LATENCY_THRESHOLD_SECONDS,
    DirectionAwareAnomalyPolicy,
)
from app.services.anomaly_runtime import (  # noqa: E402
    BASELINE_VERSION,
    EXPECTED_BASELINE_SHA256,
    EXPECTED_SERVICES,
    BaselineIntegrityError,
    POLICY_THRESHOLD,
    read_validated_baseline,
)

BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
OUTPUT = PROJECT_ROOT / "benchmark" / "results" / (
    "direction-aware-anomaly-benchmark-v2.json"
)
COMPARATIVE_OUTPUT = PROJECT_ROOT / "benchmark" / "results" / (
    "three-way-anomaly-benchmark-v5.json"
)
TRAIN_FRACTION = 0.70
MINIMUM_RUNTIME_BASELINE_ROWS = 20
SAMPLE_INTERVAL_SECONDS = 30
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, SCHEMA_VERSION}
COMPARATIVE_SCHEMA_VERSION = 5
SUPPORTED_COMPARATIVE_SCHEMA_VERSIONS = {
    2, 3, 4, COMPARATIVE_SCHEMA_VERSION
}
SYNTHETIC_SEED = 2801
ISOLATION_FOREST_CONTAMINATION = 0.05
ISOLATION_FOREST_RANDOM_STATE = 42
ISOLATION_FOREST_ESTIMATORS = 200


class AnomalyBenchmarkIncomplete(ValueError):
    pass


def _comparative_integrity_digest(artifact: dict[str, Any]) -> str:
    payload = {
        key: value for key, value in artifact.items()
        if key != "integrity"
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _comparative_dataset_digest(samples: list[dict[str, Any]]) -> str:
    dataset_payload = [
        {
            key: sample[key]
            for key in (
                "sample_id", "service", "scenario", "episode_id",
                "timestamp", "status", "failure_reason", "label_source",
                "ground_truth_anomaly", "features",
            )
        }
        for sample in samples
    ]
    canonical = json.dumps(
        dataset_payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _read_baseline(
    path: str | Path,
    *,
    exploratory: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    try:
        rows, digest = read_validated_baseline(
            path, exploratory=exploratory
        )
    except BaselineIntegrityError as exc:
        raise AnomalyBenchmarkIncomplete(
            f"Baseline integrity validation failed ({exc})."
        ) from exc
    return rows, digest


def _source_tree_state() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain",
             "--untracked-files=all"],
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"
    return "modified" if result.stdout.strip() else "clean"


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def build_reproducibility_metadata(
    *,
    baseline_path: str | Path,
    baseline_sha256: str,
    exploratory: bool,
    evaluator_path: str | Path,
    configuration: dict[str, Any],
    random_seeds: dict[str, int],
    label_provenance: dict[str, Any],
) -> dict[str, Any]:
    source_paths = {
        "evaluator": Path(evaluator_path),
        "runtime": PROJECT_ROOT / "app/services/anomaly_runtime.py",
        "policy": PROJECT_ROOT / "app/services/anomaly_policy.py",
        "detector": PROJECT_ROOT / "app/services/anomaly_detector.py",
    }
    try:
        source_hashes = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in source_paths.items()
        }
    except OSError as exc:
        raise AnomalyBenchmarkIncomplete(
            "Unable to hash evaluation source files."
        ) from exc
    return {
        "evaluation_mode": (
            "exploratory" if exploratory
            else "approved_baseline_engineering_check"
        ),
        "baseline_version": BASELINE_VERSION if not exploratory else None,
        "baseline_sha256": baseline_sha256,
        "baseline_filename": Path(baseline_path).name,
        "approved_baseline_sha256": EXPECTED_BASELINE_SHA256,
        "source_revision": _git_revision(),
        "source_tree_state": _source_tree_state(),
        "source_sha256": source_hashes,
        "python_version": platform.python_version(),
        "packages": {
            "numpy": _package_version("numpy"),
            "scikit-learn": _package_version("scikit-learn"),
        },
        "configuration": configuration,
        "random_seeds": random_seeds,
        "label_provenance": label_provenance,
    }


def _validate_reproducibility_metadata(
    metadata: Any,
    *,
    baseline_sha256: str,
) -> None:
    mode = metadata.get("evaluation_mode") if isinstance(metadata, dict) else None
    packages = metadata.get("packages") if isinstance(metadata, dict) else None
    revision = metadata.get("source_revision") if isinstance(metadata, dict) else None
    if (
        not isinstance(metadata, dict)
        or metadata.get("evaluation_mode") not in {
            "approved_baseline_engineering_check", "exploratory"
        }
        or metadata.get("baseline_sha256") != baseline_sha256
        or not isinstance(baseline_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", baseline_sha256) is None
        or metadata.get("approved_baseline_sha256")
        != EXPECTED_BASELINE_SHA256
        or (
            mode == "approved_baseline_engineering_check"
            and baseline_sha256 != EXPECTED_BASELINE_SHA256
        )
        or metadata.get("baseline_version")
        != (
            BASELINE_VERSION
            if mode == "approved_baseline_engineering_check"
            else None
        )
        or not isinstance(metadata.get("baseline_filename"), str)
        or not metadata["baseline_filename"]
        or (
            mode == "approved_baseline_engineering_check"
            and metadata.get("baseline_filename") != BASELINE.name
        )
        or (
            revision is not None
            and (
                not isinstance(revision, str)
                or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision)
                is None
            )
        )
        or metadata.get("source_tree_state") not in {
            "clean", "modified", "unknown"
        }
        or not isinstance(metadata.get("source_sha256"), dict)
        or set(metadata["source_sha256"]) != {
            "evaluator", "runtime", "policy", "detector"
        }
        or any(
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            for digest in metadata["source_sha256"].values()
        )
        or not isinstance(metadata.get("python_version"), str)
        or not metadata.get("python_version")
        or not isinstance(packages, dict)
        or set(packages) != {"numpy", "scikit-learn"}
        or any(
            version is not None
            and (not isinstance(version, str) or not version)
            for version in packages.values()
        )
        or not isinstance(metadata.get("configuration"), dict)
        or not isinstance(metadata.get("random_seeds"), dict)
        or any(
            type(seed) is not int
            for seed in metadata["random_seeds"].values()
        )
        or not isinstance(metadata.get("label_provenance"), dict)
    ):
        raise AnomalyBenchmarkIncomplete(
            "Reproducibility metadata is invalid."
        )
    provenance = metadata["label_provenance"]
    if provenance.get("independently_reviewed_labels_present") is not False:
        raise AnomalyBenchmarkIncomplete(
            "Synthetic benchmark label provenance is invalid."
        )


def _artifact_freshness(artifact: dict[str, Any]) -> str:
    metadata = artifact.get("reproducibility")
    if not isinstance(metadata, dict):
        return "HISTORICAL: source-tree reproducibility metadata is absent."
    current_revision = _git_revision()
    if metadata.get("source_revision") != current_revision:
        return "STALE: artifact source revision differs from current HEAD."
    current_sources = {
        "evaluator": Path(__file__),
        "runtime": PROJECT_ROOT / "app/services/anomaly_runtime.py",
        "policy": PROJECT_ROOT / "app/services/anomaly_policy.py",
        "detector": PROJECT_ROOT / "app/services/anomaly_detector.py",
    }
    for name, path in current_sources.items():
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return f"STALE: current {name} source could not be read."
        if digest != metadata["source_sha256"].get(name):
            return f"STALE: artifact {name} source differs from current source."
    if metadata.get("python_version") != platform.python_version():
        return "STALE: artifact Python version differs from this environment."
    current_packages = {
        "numpy": _package_version("numpy"),
        "scikit-learn": _package_version("scikit-learn"),
    }
    if metadata.get("packages") != current_packages:
        return "STALE: artifact dependency versions differ from this environment."
    if metadata.get("evaluation_mode") == "exploratory":
        return (
            "EXPLORATORY: non-approved baseline; structurally validated and "
            "identified by digest, but not runtime-qualified."
        )
    try:
        baseline_digest = hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    except OSError:
        return "STALE: current approved baseline could not be read."
    if baseline_digest != metadata.get("baseline_sha256"):
        return "STALE: artifact baseline digest differs from current baseline."
    if current_revision is None:
        return (
            "PARTIAL: source files, environment and approved baseline match; "
            "Git revision could not be verified."
        )
    return (
        "CURRENT: source hashes and approved baseline digest match this "
        "checkout; this does not establish operational validity."
    )


def _confusion(samples: list[dict[str, Any]], prediction_field: str) -> dict:
    evaluated = [
        sample for sample in samples
        if sample["status"] == "evaluated"
        and sample["ground_truth_anomaly"] is not None
        and sample[prediction_field] is not None
    ]
    tp = sum(
        sample["ground_truth_anomaly"] and sample[prediction_field]
        for sample in evaluated
    )
    fp = sum(
        not sample["ground_truth_anomaly"] and sample[prediction_field]
        for sample in evaluated
    )
    tn = sum(
        not sample["ground_truth_anomaly"] and not sample[prediction_field]
        for sample in evaluated
    )
    fn = sum(
        sample["ground_truth_anomaly"] and not sample[prediction_field]
        for sample in evaluated
    )
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None
        and precision + recall > 0
        else 0.0
        if precision is not None and recall is not None
        else None
    )
    false_positive_rate = fp / (fp + tn) if fp + tn else None
    return {
        "status": "measured" if evaluated else "not_measured",
        "true_positives": tp if evaluated else None,
        "false_positives": fp if evaluated else None,
        "true_negatives": tn if evaluated else None,
        "false_negatives": fn if evaluated else None,
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "f1": round(f1, 6) if f1 is not None else None,
        "false_positive_rate": (
            round(false_positive_rate, 6)
            if false_positive_rate is not None else None
        ),
        "sample_count": len(evaluated),
        "excluded_count": len(samples) - len(evaluated),
    }


def _fit_runtime_compatible_policy(
    baseline: list[list[float]],
) -> DirectionAwareAnomalyPolicy:
    if len(baseline) < MINIMUM_RUNTIME_BASELINE_ROWS:
        raise AnomalyBenchmarkIncomplete(
            "Insufficient baseline rows; active runtime minimum baseline "
            f"requires at least {MINIMUM_RUNTIME_BASELINE_ROWS} rows.")
    policy = DirectionAwareAnomalyPolicy(threshold=POLICY_THRESHOLD)
    policy.fit(baseline)
    return policy


def _metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [sample for sample in samples if sample["status"] == "evaluated"]
    normal = sum(sample["ground_truth_anomaly"] is False for sample in evaluated)
    anomalous = sum(sample["ground_truth_anomaly"] is True for sample in evaluated)
    unavailable = sum(sample["status"] != "evaluated" for sample in samples)
    episode_delays = []
    episode_ids = sorted({
        sample["episode_id"]
        for sample in evaluated
        if sample["episode_id"] is not None
        and sample["ground_truth_anomaly"] is True
    })
    for episode_id in episode_ids:
        points = [
            sample for sample in evaluated if sample["episode_id"] == episode_id
        ]
        onset = min(
            datetime.fromisoformat(sample["timestamp"])
            for sample in points
            if sample["ground_truth_anomaly"] is True
        )
        first_detected = next(
            (
                datetime.fromisoformat(sample["timestamp"])
                for sample in points
                if sample["predicted_anomaly"] is True
            ),
            None,
        )
        episode_delays.append(
            {
                "episode_id": episode_id,
                "onset_at": _timestamp(onset),
                "first_detected_at": (
                    _timestamp(first_detected)
                    if first_detected is not None else None
                ),
                "delay_seconds": (
                    (first_detected - onset).total_seconds()
                    if first_detected is not None else None
                ),
                "detected": first_detected is not None,
            }
        )
    detected_delays = [
        episode["delay_seconds"]
        for episode in episode_delays
        if episode["delay_seconds"] is not None
    ]
    return {
        "unit": "telemetry point",
        "labelled_point_count": normal + anomalous,
        "normal_point_count": normal,
        "anomalous_point_count": anomalous,
        "unavailable_point_count": unavailable,
        "point_anomaly": _confusion(samples, "predicted_anomaly"),
        "actionable_candidate": _confusion(
            samples, "predicted_actionable"
        ),
        "gradual_drift_detection_delay": {
            "status": "measured" if episode_delays else "not_measured",
            "unit": "seconds",
            "episode_count": len(episode_delays),
            "detected_episode_count": len(detected_delays),
            "missed_episode_count": len(episode_delays) - len(detected_delays),
            "mean_seconds": (
                round(sum(detected_delays) / len(detected_delays), 6)
                if detected_delays else None
            ),
            "episodes": episode_delays,
            "scope": (
                "Synthetic drift episodes only; no mapping to incident-level "
                "detection or response latency."
            ),
        },
    }


def build_anomaly_benchmark(
    *,
    baseline_path: str | Path = BASELINE,
    exploratory: bool = False,
) -> dict[str, Any]:
    rows, baseline_sha256 = _read_baseline(
        baseline_path, exploratory=exploratory
    )
    all_times = sorted({
        datetime.fromisoformat(row["timestamp"])
        for row in rows
    })
    if len(all_times) < 10:
        raise AnomalyBenchmarkIncomplete(
            "At least 10 distinct timestamps are required for a holdout.")
    split = int(len(all_times) * TRAIN_FRACTION)
    training_times = set(all_times[:split])
    holdout_times = set(all_times[split:])
    latest_baseline_time = max(all_times).astimezone(timezone.utc)
    synthetic_start = latest_baseline_time + timedelta(seconds=60)
    rng = np.random.default_rng(SYNTHETIC_SEED)
    services: dict[str, list[dict[str, Any]]] = {
        service: [] for service in EXPECTED_SERVICES
    }
    for row in rows:
        services[row["service"]].append(row)

    policies: dict[str, DirectionAwareAnomalyPolicy] = {}
    training_values: dict[str, np.ndarray] = {}
    holdout_rows: dict[str, list[dict[str, Any]]] = {}
    scales: dict[str, np.ndarray] = {}
    centers: dict[str, np.ndarray] = {}
    for service in EXPECTED_SERVICES:
        ordered = sorted(services[service], key=lambda item: item["timestamp"])
        train = [
            [float(row[name]) for name in FEATURE_NAMES]
            for row in ordered if datetime.fromisoformat(row["timestamp"]) in training_times
        ]
        held = [
            row for row in ordered
            if datetime.fromisoformat(row["timestamp"]) in holdout_times
        ]
        if not held:
            raise AnomalyBenchmarkIncomplete(
                f"{service} lacks chronological holdout rows.")
        policy = _fit_runtime_compatible_policy(train)
        values = np.asarray(train, dtype=float)
        median = np.median(values, axis=0)
        mad = np.median(np.abs(values - median), axis=0)
        scale = 1.4826 * mad
        scale = np.where(
            scale > 1e-12,
            scale,
            np.maximum(np.std(values, axis=0), 1e-12),
        )
        policies[service] = policy
        training_values[service] = values
        holdout_rows[service] = held
        centers[service] = median
        scales[service] = scale

    samples: list[dict[str, Any]] = []

    def add_sample(
        *,
        sample_id: str,
        service: str,
        timestamp: datetime,
        scenario: str,
        ground_truth: bool | None,
        values: list[float] | None,
        episode_id: str | None = None,
        status: str = "evaluated",
        failure_reason: str | None = None,
    ) -> None:
        output = None
        predicted_anomaly = None
        predicted_actionable = None
        if status == "evaluated":
            try:
                output = policies[service].evaluate([values])[0]
            except (TypeError, ValueError) as exc:
                status = "unavailable"
                failure_reason = f"invalid_observation:{type(exc).__name__}"
            else:
                predicted_anomaly = bool(
                    output["actionable_candidates"]
                    or output["informational_deviations"]
                )
                predicted_actionable = bool(output["actionable_candidates"])
        recorded_values = (
            {
                name: (
                    float(value) if math.isfinite(float(value)) else None
                )
                for name, value in zip(FEATURE_NAMES, values)
            }
            if values is not None else None
        )
        samples.append({
            "sample_id": sample_id,
            "service": service,
            "scenario": scenario,
            "episode_id": episode_id,
            "timestamp": _timestamp(timestamp),
            "status": status,
            "failure_reason": failure_reason,
            "ground_truth_anomaly": (
                ground_truth if status == "evaluated" else None
            ),
            "predicted_anomaly": predicted_anomaly,
            "predicted_actionable": predicted_actionable,
            "features": recorded_values,
            "detector_output": output,
        })

    # The chronological holdout is assumed to be normal historical telemetry.
    for service in EXPECTED_SERVICES:
        for index, row in enumerate(holdout_rows[service]):
            values = [float(row[name]) for name in FEATURE_NAMES]
            add_sample(
                sample_id=f"holdout-{service}-{index:02d}",
                service=service,
                timestamp=datetime.fromisoformat(row["timestamp"]),
                scenario="chronological_baseline_holdout_assumed_normal",
                ground_truth=False,
                values=values,
            )

    for service in EXPECTED_SERVICES:
        center = centers[service]
        scale = scales[service]
        service_origin = synthetic_start
        feature_indexes = {
            "request_rate": FEATURE_NAMES.index("request_rate"),
            "p95_latency_seconds": FEATURE_NAMES.index("p95_latency_seconds"),
            "memory_bytes": FEATURE_NAMES.index("memory_bytes"),
            "cpu_cores_used": FEATURE_NAMES.index("cpu_cores_used"),
        }

        # Bounded noise around the training center is labelled normal by design.
        for index in range(8):
            noise = rng.normal(0.0, 2.5, len(FEATURE_NAMES)) * scale
            values = np.maximum(center + noise, 0.0).tolist()
            add_sample(
                sample_id=f"noise-{service}-{index:02d}",
                service=service,
                timestamp=service_origin + timedelta(seconds=index * 30),
                scenario="bounded_multivariate_noise_assumed_normal",
                ground_truth=False,
                values=values,
            )

        for feature, direction in (
            ("cpu_cores_used", "up"),
            ("cpu_cores_used", "down"),
            ("p95_latency_seconds", "up"),
            ("p95_latency_seconds", "down"),
            ("memory_bytes", "up"),
            ("request_rate", "down"),
        ):
            index = feature_indexes[feature]
            values = center.copy()
            candidate = center[index] + (
                8.0 * scale[index] if direction == "up"
                else -8.0 * scale[index]
            )
            values[index] = max(0.0, candidate)
            add_sample(
                sample_id=f"step-{service}-{feature}-{direction}",
                service=service,
                timestamp=service_origin + timedelta(minutes=5),
                scenario=f"sudden_{direction}_{feature}",
                ground_truth=True,
                values=values.tolist(),
            )

        drift_id = f"gradual-drift-{service}-cpu"
        for index in range(12):
            values = center.copy()
            values[feature_indexes["cpu_cores_used"]] += (
                index * 0.75 * scale[feature_indexes["cpu_cores_used"]]
            )
            add_sample(
                sample_id=f"{drift_id}-{index:02d}",
                service=service,
                timestamp=service_origin
                + timedelta(minutes=10, seconds=index * SAMPLE_INTERVAL_SECONDS),
                scenario="gradual_cpu_drift",
                ground_truth=index > 0,
                values=values.tolist(),
                episode_id=drift_id,
            )

        add_sample(
            sample_id=f"missing-{service}",
            service=service,
            timestamp=service_origin + timedelta(minutes=20),
            scenario="missing_observation",
            ground_truth=None,
            values=None,
            status="unavailable",
            failure_reason="no_observation",
        )
        malformed = center.tolist()
        malformed[feature_indexes["cpu_cores_used"]] = float("nan")
        add_sample(
            sample_id=f"invalid-{service}",
            service=service,
            timestamp=service_origin + timedelta(minutes=21),
            scenario="non_finite_observation",
            ground_truth=None,
            values=malformed,
            failure_reason="invalid_observation",
        )

    unsupported = [{
        "scenario": "available_disk_space_decrease",
        "status": "unsupported",
        "reason": (
            "The active telemetry FEATURE_NAMES do not include available "
            "disk space; no disk prediction was fabricated."
        ),
    }]
    metrics = _metrics(samples)
    reproducibility = build_reproducibility_metadata(
        baseline_path=baseline_path,
        baseline_sha256=baseline_sha256,
        exploratory=exploratory,
        evaluator_path=__file__,
        configuration={
            "training_fraction": TRAIN_FRACTION,
            "minimum_training_rows": MINIMUM_RUNTIME_BASELINE_ROWS,
            "policy_threshold": POLICY_THRESHOLD,
            "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
            "isolation_forest_integrated_in_runtime": False,
        },
        random_seeds={"synthetic_dataset": SYNTHETIC_SEED},
        label_provenance={
            "baseline_holdout": "assumed_normal_not_independently_reviewed",
            "synthetic_controls": "assumed_normal_by_construction",
            "synthetic_positives": "synthetic_positive_by_construction",
            "independently_reviewed_labels_present": False,
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "reproducibility": reproducibility,
        "run": {
            "evaluation_mode": "synthetic",
            "run_id": "direction-aware-mad-synthetic-v1",
            "baseline_sha256": baseline_sha256,
            "synthetic_seed": SYNTHETIC_SEED,
            "latest_baseline_timestamp": _timestamp(latest_baseline_time),
            "synthetic_data_start": _timestamp(synthetic_start),
        },
        "detector": {
            "identity": "app.services.anomaly_policy.DirectionAwareAnomalyPolicy",
            "application_runtime_entry": (
                "app.services.anomaly_runtime.evaluate_service_anomaly"
            ),
            "isolation_forest_integrated_in_runtime": False,
            "policy_threshold": POLICY_THRESHOLD,
            "threshold_comparison": "deviation_score > threshold",
            "threshold_source": "app.services.anomaly_runtime.POLICY_THRESHOLD",
            "operational_latency_threshold_seconds": (
                OPERATIONAL_LATENCY_THRESHOLD_SECONDS
            ),
            "feature_names": list(FEATURE_NAMES),
            "actionable_high_features": sorted(ACTIONABLE_HIGH_FEATURES),
            "training_fraction": TRAIN_FRACTION,
            "training_minimum_rows": MINIMUM_RUNTIME_BASELINE_ROWS,
            "training_rows_per_service": {
                service: len(training_values[service])
                for service in EXPECTED_SERVICES
            },
            "holdout_rows_per_service": {
                service: len(holdout_rows[service])
                for service in EXPECTED_SERVICES
            },
            "contamination": None,
        },
        "dataset_provenance": {
            "baseline_path": str(Path(baseline_path).name),
            "baseline_sha256": baseline_sha256,
            "baseline_row_count": len(rows),
            "baseline_services": list(EXPECTED_SERVICES),
            "holdout_label_assumption": (
                "Chronological rows after the 70% timestamp split are assumed "
                "normal because the input is the historical baseline; labels "
                "are not independently reviewed."
            ),
            "synthetic_assumptions": {
                "seed": SYNTHETIC_SEED,
                "noise_standard_deviations": 2.5,
                "step_anomaly_scale_multiples": 8.0,
                "gradual_drift_step_scale_multiples": 0.75,
                "gradual_drift_points": 12,
                "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
                "missing_values_are_unavailable_not_normal": True,
            },
        },
        "metric_scope": {
            "primary": "point-level anomaly prediction",
            "prediction_rule": (
                "Positive if detector returns any actionable candidate or "
                "informational deviation."
            ),
            "actionability_reported_separately": True,
            "incident_level_detection_claim": False,
            "available_disk_space_supported": False,
        },
        "samples": samples,
        "unavailable_checks": [
            {
                "check_id": "runtime_minimum_baseline",
                "status": "unavailable",
                "required_rows": MINIMUM_RUNTIME_BASELINE_ROWS,
                "reason": "A 19-row fit is below the active runtime minimum.",
                "ground_truth_anomaly": None,
            },
            {
                "check_id": "available_disk_space",
                "status": "unsupported",
                "reason": unsupported[0]["reason"],
                "ground_truth_anomaly": None,
            },
        ],
        "metrics": metrics,
        "limitations": [
            "Synthetic and assumed labels do not establish live monitoring accuracy.",
            "The baseline holdout and generated noise are assumed normal.",
            "Static synthetic feature excursions are labeled positive by construction.",
            "Point-level counts do not represent incident-level detection rates.",
            "Only gradual-drift episodes have a modeled detection-delay origin.",
            "Detection does not imply an incident, policy approval, execution, or recovery.",
        ],
    }


def _comparison_label_source(sample: dict[str, Any]) -> str:
    if sample["status"] != "evaluated":
        return "unlabeled_unavailable"
    if sample["scenario"] == "chronological_baseline_holdout_assumed_normal":
        return "assumed_normal_baseline"
    if sample["scenario"] == "bounded_multivariate_noise_assumed_normal":
        return "assumed_normal_synthetic_control"
    if sample["scenario"] == "gradual_cpu_drift" and not sample[
        "ground_truth_anomaly"
    ]:
        return "synthetic_pre_onset_normal"
    if sample["ground_truth_anomaly"]:
        return "synthetic_positive_by_construction"
    return "assumed_normal_synthetic_control"


def _comparison_confusion(
    samples: list[dict[str, Any]],
    method: str,
    *,
    prediction_field: str = "anomaly",
) -> dict[str, Any]:
    eligible = [
        sample for sample in samples
        if sample["status"] == "evaluated"
        and isinstance(sample["ground_truth_anomaly"], bool)
        and isinstance(sample["predictions"][method][prediction_field], bool)
    ]
    tp = sum(
        sample["ground_truth_anomaly"]
        and sample["predictions"][method][prediction_field]
        for sample in eligible
    )
    fp = sum(
        not sample["ground_truth_anomaly"]
        and sample["predictions"][method][prediction_field]
        for sample in eligible
    )
    tn = sum(
        not sample["ground_truth_anomaly"]
        and not sample["predictions"][method][prediction_field]
        for sample in eligible
    )
    fn = sum(
        sample["ground_truth_anomaly"]
        and not sample["predictions"][method][prediction_field]
        for sample in eligible
    )
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None
        and precision + recall > 0
        else 0.0
        if precision is not None and recall is not None
        else None
    )
    false_positive_rate = fp / (fp + tn) if fp + tn else None
    return {
        "status": "measured" if eligible else "not_measured",
        "true_positives": tp if eligible else None,
        "false_positives": fp if eligible else None,
        "true_negatives": tn if eligible else None,
        "false_negatives": fn if eligible else None,
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "f1": round(f1, 6) if f1 is not None else None,
        "false_positive_rate": (
            round(false_positive_rate, 6)
            if false_positive_rate is not None else None
        ),
        "eligible_observation_count": len(eligible),
    }


def _comparison_delay(
    samples: list[dict[str, Any]],
    method: str,
    service: str | None = None,
) -> dict[str, Any]:
    scope = [
        sample for sample in samples
        if sample["status"] == "evaluated"
        and (service is None or sample["service"] == service)
    ]
    episode_ids = sorted({
        sample["episode_id"]
        for sample in scope
        if sample["episode_id"] is not None
        and sample["ground_truth_anomaly"] is True
    })
    episodes = []
    for episode_id in episode_ids:
        points = sorted(
            (
                sample for sample in scope
                if sample["episode_id"] == episode_id
            ),
            key=lambda sample: datetime.fromisoformat(sample["timestamp"]),
        )
        positive_points = [
            sample for sample in points
            if sample["ground_truth_anomaly"] is True
        ]
        onset = datetime.fromisoformat(positive_points[0]["timestamp"])
        first_detection = next(
            (
                datetime.fromisoformat(sample["timestamp"])
                for sample in points
                if datetime.fromisoformat(sample["timestamp"]) >= onset
                and sample["predictions"][method]["anomaly"] is True
            ),
            None,
        )
        episodes.append({
            "episode_id": episode_id,
            "onset_at": _timestamp(onset),
            "first_detected_at": (
                _timestamp(first_detection) if first_detection else None
            ),
            "delay_seconds": (
                (first_detection - onset).total_seconds()
                if first_detection else None
            ),
            "detected": first_detection is not None,
        })
    detected_delays = [
        item["delay_seconds"] for item in episodes
        if item["delay_seconds"] is not None
    ]
    return {
        "status": "measured" if episodes else "not_measured",
        "unit": "seconds",
        "onset_rule": "first timestamped point labeled anomalous",
        "detection_rule": (
            "first predicted anomaly at or after the labeled onset"
        ),
        "episode_count": len(episodes),
        "detected_episode_count": len(detected_delays),
        "missed_episode_count": len(episodes) - len(detected_delays),
        "mean_detected_delay_seconds": (
            round(sum(detected_delays) / len(detected_delays), 6)
            if detected_delays else None
        ),
        "episodes": episodes,
    }


def _comparison_metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    methods = ("isolation_forest", "direction_aware_mad", "hybrid")
    unavailable = [
        sample for sample in samples if sample["status"] != "evaluated"
    ]

    def scope_metrics(scope: list[dict[str, Any]]) -> dict[str, Any]:
        result = {
            "total_observation_count": len(scope),
            "eligible_observation_count": sum(
                sample["status"] == "evaluated" for sample in scope
            ),
            "unavailable_observation_count": sum(
                sample["status"] != "evaluated" for sample in scope
            ),
            "unavailable_by_reason": dict(sorted(Counter(
                sample["failure_reason"] or "unspecified"
                for sample in scope if sample["status"] != "evaluated"
            ).items())),
            "point_anomaly": {},
            "episode_detection_delay": {},
        }
        for method in methods:
            result["point_anomaly"][method] = _comparison_confusion(
                scope, method
            )
            result["episode_detection_delay"][method] = (
                _comparison_delay(scope, method)
            )
        result["mad_actionable_candidate"] = _comparison_confusion(
            scope, "direction_aware_mad",
            prediction_field="actionable_candidate",
        )
        return result

    overall = scope_metrics(samples)
    per_service = {
        service: scope_metrics([
            sample for sample in samples if sample["service"] == service
        ])
        for service in EXPECTED_SERVICES
    }
    return {
        **overall,
        "unavailable_by_reason": dict(sorted(Counter(
            sample["failure_reason"] or "unspecified"
            for sample in unavailable
        ).items())),
        "per_service": per_service,
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
    revision = result.stdout.strip()
    return revision or None


def build_comparative_anomaly_benchmark(
    *,
    baseline_path: str | Path = BASELINE,
    exploratory: bool = False,
) -> dict[str, Any]:
    """Compare IF, MAD, and their union on the same F.28 observations."""
    baseline_rows, baseline_sha256 = _read_baseline(
        baseline_path, exploratory=exploratory
    )
    f28 = build_anomaly_benchmark(
        baseline_path=baseline_path, exploratory=exploratory
    )
    all_times = sorted({
        datetime.fromisoformat(row["timestamp"])
        for row in baseline_rows
    })
    split_index = int(len(all_times) * TRAIN_FRACTION)
    training_times = all_times[:split_index]
    baseline_evaluation_times = all_times[split_index:]
    training_time_set = set(training_times)
    evaluation_timestamp_strings = {
        sample["timestamp"] for sample in f28["samples"]
    }
    normalized_training_timestamps = {
        _timestamp(stamp) for stamp in training_times
    }
    if normalized_training_timestamps.intersection(
        evaluation_timestamp_strings
    ):
        raise AnomalyBenchmarkIncomplete(
            "Training and evaluation timestamps overlap.")

    training_by_service: dict[str, list[list[float]]] = {}
    policies: dict[str, DirectionAwareAnomalyPolicy] = {}
    isolation_forests: dict[str, TelemetryAnomalyDetector] = {}
    per_service_split = {}
    for service in EXPECTED_SERVICES:
        ordered = sorted(
            (row for row in baseline_rows if row["service"] == service),
            key=lambda row: datetime.fromisoformat(row["timestamp"]),
        )
        training_rows = [
            row for row in ordered
            if datetime.fromisoformat(row["timestamp"]) in training_time_set
        ]
        held_rows = [
            row for row in ordered
            if datetime.fromisoformat(row["timestamp"])
            in set(baseline_evaluation_times)
        ]
        if len(training_rows) < MINIMUM_RUNTIME_BASELINE_ROWS or not held_rows:
            raise AnomalyBenchmarkIncomplete(
                f"{service} lacks sufficient chronological train/holdout rows.")
        training = [
            [float(row[name]) for name in FEATURE_NAMES]
            for row in training_rows
        ]
        training_by_service[service] = training
        policies[service] = _fit_runtime_compatible_policy(training)
        isolation_forests[service] = TelemetryAnomalyDetector(
            contamination=ISOLATION_FOREST_CONTAMINATION,
            random_state=ISOLATION_FOREST_RANDOM_STATE,
        )
        isolation_forests[service].fit(training)
        service_train_times = {
            _timestamp(datetime.fromisoformat(row["timestamp"]))
            for row in training_rows
        }
        service_eval_times = {
            _timestamp(datetime.fromisoformat(row["timestamp"]))
            for row in held_rows
        }
        if service_train_times.intersection(service_eval_times):
            raise AnomalyBenchmarkIncomplete(
                f"{service} training and holdout timestamps overlap.")
        per_service_split[service] = {
            "training_row_count": len(training_rows),
            "evaluation_baseline_row_count": len(held_rows),
            "training_timestamp_start": min(service_train_times),
            "training_timestamp_end": max(service_train_times),
            "evaluation_timestamp_start": min(service_eval_times),
            "evaluation_timestamp_end": max(service_eval_times),
        }

    comparison_samples = []
    for sample in f28["samples"]:
        predictions = None
        isolation_score = None
        mad_actionable_features = None
        mad_informational_features = None
        if sample["status"] == "evaluated":
            values = [sample["features"][name] for name in FEATURE_NAMES]
            isolation_result = isolation_forests[sample["service"]].score(
                [values]
            )[0]
            mad_output = sample["detector_output"]
            mad_prediction = bool(
                mad_output["actionable_candidates"]
                or mad_output["informational_deviations"]
            )
            isolation_prediction = isolation_result["anomaly"]
            predictions = {
                "isolation_forest": {
                    "anomaly": isolation_prediction,
                },
                "direction_aware_mad": {
                    "anomaly": mad_prediction,
                    "actionable_candidate": bool(
                        mad_output["actionable_candidates"]
                    ),
                },
                "hybrid": {
                    "anomaly": bool(isolation_prediction or mad_prediction),
                },
            }
            isolation_score = isolation_result["anomaly_score"]
            mad_actionable_features = list(
                mad_output["actionable_candidates"]
            )
            mad_informational_features = list(
                mad_output["informational_deviations"]
            )
        comparison_samples.append({
            "sample_id": sample["sample_id"],
            "service": sample["service"],
            "scenario": sample["scenario"],
            "episode_id": sample["episode_id"],
            "timestamp": sample["timestamp"],
            "status": sample["status"],
            "failure_reason": sample["failure_reason"],
            "label_source": _comparison_label_source(sample),
            "ground_truth_anomaly": sample["ground_truth_anomaly"],
            "features": sample["features"],
            "predictions": predictions,
            "isolation_forest_score": isolation_score,
            "mad_actionable_features": mad_actionable_features,
            "mad_informational_features": mad_informational_features,
        })

    evaluation_timestamps = sorted({
        sample["timestamp"] for sample in comparison_samples
    })
    training_timestamps = sorted(normalized_training_timestamps)
    if set(training_timestamps).intersection(evaluation_timestamps):
        raise AnomalyBenchmarkIncomplete(
            "Training and evaluation timestamps overlap.")
    dataset_sha256 = _comparative_dataset_digest(comparison_samples)
    reproducibility = build_reproducibility_metadata(
        baseline_path=baseline_path,
        baseline_sha256=baseline_sha256,
        exploratory=exploratory,
        evaluator_path=__file__,
        configuration={
            "training_fraction": TRAIN_FRACTION,
            "minimum_training_rows": MINIMUM_RUNTIME_BASELINE_ROWS,
            "mad_threshold": POLICY_THRESHOLD,
            "isolation_forest_estimators": ISOLATION_FOREST_ESTIMATORS,
            "isolation_forest_contamination": (
                ISOLATION_FOREST_CONTAMINATION
            ),
            "methods": ["isolation_forest", "direction_aware_mad", "hybrid"],
        },
        random_seeds={
            "synthetic_dataset": SYNTHETIC_SEED,
            "isolation_forest": ISOLATION_FOREST_RANDOM_STATE,
        },
        label_provenance={
            "baseline_holdout": "assumed_normal_not_independently_reviewed",
            "synthetic_controls": "assumed_normal_by_construction",
            "synthetic_positives": "synthetic_positive_by_construction",
            "independently_reviewed_labels_present": False,
        },
    )

    artifact = {
        "schema_version": COMPARATIVE_SCHEMA_VERSION,
        "benchmark": "three_way_synthetic_anomaly_comparison",
        "reproducibility": reproducibility,
        "run": {
            "evaluation_mode": "synthetic",
            "code_revision": _git_revision(),
            "code_revision_scope": (
                "HEAD revision only; does not identify uncommitted working-tree changes"
            ),
            "working_tree_state": reproducibility["source_tree_state"],
        },
        "provenance": {
            "baseline_path": Path(baseline_path).name,
            "baseline_sha256": baseline_sha256,
            "dataset_sha256": dataset_sha256,
            "baseline_row_count": len(baseline_rows),
            "features": list(FEATURE_NAMES),
            "feature_order": list(FEATURE_NAMES),
            "services": list(EXPECTED_SERVICES),
            "random_seeds": {
                "synthetic_dataset": SYNTHETIC_SEED,
                "isolation_forest": ISOLATION_FOREST_RANDOM_STATE,
            },
            "split": {
                "method": "chronological 70/30 split of distinct baseline timestamps, per service",
                "training_fraction": TRAIN_FRACTION,
                "training_timestamps": training_timestamps,
                "baseline_evaluation_timestamps": [
                    _timestamp(stamp) for stamp in baseline_evaluation_times
                ],
                "evaluation_sample_timestamp_start": min(
                    evaluation_timestamps
                ),
                "evaluation_sample_timestamp_end": max(
                    evaluation_timestamps
                ),
                "training_evaluation_overlap": False,
                "per_service": per_service_split,
            },
            "label_assumptions": {
                "assumed_normal_baseline": (
                    "Chronological baseline evaluation rows are assumed "
                    "normal; labels are not independently reviewed."
                ),
                "synthetic_controls": (
                    "Generated bounded-noise controls are assumed normal."
                ),
                "synthetic_positives": (
                    "Injected point and episode anomalies are labeled "
                    "positive by construction, not by independent operational review."
                ),
                "operational_labels_present": False,
            },
            "unavailable_sample_rules": {
                "missing_observation": "unavailable; excluded from every method's metrics",
                "invalid_observation": "unavailable; excluded from every method's metrics",
                "reason": (
                    "The shared F.28 evaluator's eligibility status is used "
                    "for all methods; unavailable observations are never normal."
                ),
            },
            "historical_f28_baseline_sha256": f28["run"]["baseline_sha256"],
        },
        "methods": {
            "isolation_forest": {
                "identity": "app.services.anomaly_detector.TelemetryAnomalyDetector",
                "n_estimators": ISOLATION_FOREST_ESTIMATORS,
                "contamination": ISOLATION_FOREST_CONTAMINATION,
                "random_state": ISOLATION_FOREST_RANDOM_STATE,
                "fit_scope": "per service on training timestamps only",
            },
            "direction_aware_mad": {
                "identity": "app.services.anomaly_policy.DirectionAwareAnomalyPolicy",
                "threshold": POLICY_THRESHOLD,
                "prediction_rule": (
                    "anomaly if any informational deviation or actionable candidate"
                ),
                "actionability_rule": (
                    "report actionable candidates separately; do not use them "
                    "as the shared point-anomaly prediction"
                ),
                "fit_scope": "per service on training timestamps only",
            },
            "hybrid": {
                "identity": "experimental union; not integrated in runtime",
                "prediction_rule": (
                    "Isolation Forest anomaly OR direction-aware MAD anomaly"
                ),
                "mad_threshold": POLICY_THRESHOLD,
                "fit_scope": "per service; component models use same training rows",
            },
        },
        "episode_delay_contract": {
            "scope": "timestamped synthetic positive episodes only",
            "onset": "first timestamped point labeled anomalous",
            "detection": "first predicted anomaly at or after onset",
            "aggregation": (
                "mean over detected episodes only; missed episodes remain "
                "listed with null delay and are counted separately"
            ),
            "not_operational_incident_latency": True,
        },
        "samples": comparison_samples,
        "metrics": _comparison_metrics(comparison_samples),
        "limitations": [
            "All labels are synthetic or assumed normal; no independently verified operational labels are present.",
            "Results compare method behavior on this fixture only and do not establish production detection quality.",
            "The hybrid is an experimental union and is not integrated in runtime.",
            "Baseline split is temporal within the same services, not service-transfer validation.",
            "Unavailable sample counts cover missing/invalid F.28 observations; stale/future telemetry episodes are not modeled in this dataset.",
            "Episode delay is defined only for generated timestamped episodes and is not incident response latency.",
        ],
    }
    artifact["integrity"] = {
        "algorithm": "sha256",
        "canonicalization": (
            "UTF-8 JSON with sorted keys, compact separators, and no NaN; "
            "digest covers the complete artifact except the integrity object"
        ),
        "payload_sha256": _comparative_integrity_digest(artifact),
    }
    return artifact


def validate_comparative_anomaly_artifact(
    artifact: Any,
) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise AnomalyBenchmarkIncomplete("Artifact must be a JSON object.")
    if (
        artifact.get("schema_version")
        not in SUPPORTED_COMPARATIVE_SCHEMA_VERSIONS
        or artifact.get("benchmark")
        != "three_way_synthetic_anomaly_comparison"
        or not isinstance(artifact.get("provenance"), dict)
        or not isinstance(artifact.get("methods"), dict)
        or not isinstance(artifact.get("samples"), list)
        or not isinstance(artifact.get("run"), dict)
    ):
        raise AnomalyBenchmarkIncomplete(
            "Comparative artifact schema is invalid.")
    if artifact["schema_version"] >= 4:
        integrity = artifact.get("integrity")
        if (
            not isinstance(integrity, dict)
            or integrity.get("algorithm") != "sha256"
            or integrity.get("canonicalization")
            != (
                "UTF-8 JSON with sorted keys, compact separators, and no NaN; "
                "digest covers the complete artifact except the integrity object"
            )
        ):
            raise AnomalyBenchmarkIncomplete(
                "Comparative artifact integrity metadata is invalid.")
        expected_integrity_digest = _comparative_integrity_digest(artifact)
        if integrity.get("payload_sha256") != expected_integrity_digest:
            raise AnomalyBenchmarkIncomplete(
                "Comparative artifact integrity digest does not match payload.")
    expected_method_configs = {
        "isolation_forest": {
            "identity": "app.services.anomaly_detector.TelemetryAnomalyDetector",
            "n_estimators": ISOLATION_FOREST_ESTIMATORS,
            "contamination": ISOLATION_FOREST_CONTAMINATION,
            "random_state": ISOLATION_FOREST_RANDOM_STATE,
            "fit_scope": "per service on training timestamps only",
        },
        "direction_aware_mad": {
            "identity": "app.services.anomaly_policy.DirectionAwareAnomalyPolicy",
            "threshold": POLICY_THRESHOLD,
            "prediction_rule": (
                "anomaly if any informational deviation or actionable candidate"
            ),
        },
        "hybrid": {
            "identity": "experimental union; not integrated in runtime",
            "prediction_rule": (
                "Isolation Forest anomaly OR direction-aware MAD anomaly"
            ),
            "mad_threshold": POLICY_THRESHOLD,
        },
    }
    if any(
        not isinstance(artifact["methods"].get(name), dict)
        or any(
            artifact["methods"][name].get(key) != value
            for key, value in config.items()
        )
        for name, config in expected_method_configs.items()
    ):
        raise AnomalyBenchmarkIncomplete(
            "Comparative method configuration is invalid.")
    provenance = artifact["provenance"]
    if artifact["schema_version"] >= 3 and (
        not isinstance(artifact["run"].get("code_revision_scope"), str)
    ):
        raise AnomalyBenchmarkIncomplete(
            "Comparative code revision scope is missing.")
    if artifact["schema_version"] == 4 and (
        artifact["run"].get("working_tree_state")
        != "not captured; code_revision identifies HEAD only"
        or provenance.get("feature_order") != list(FEATURE_NAMES)
        or provenance.get("random_seeds") != {
            "synthetic_dataset": SYNTHETIC_SEED,
            "isolation_forest": ISOLATION_FOREST_RANDOM_STATE,
        }
    ):
        raise AnomalyBenchmarkIncomplete(
            "Comparative reproducibility metadata is invalid.")
    if artifact["schema_version"] >= 5 and (
        artifact["run"].get("working_tree_state")
        not in {"clean", "modified", "unknown"}
        or provenance.get("feature_order") != list(FEATURE_NAMES)
        or provenance.get("random_seeds") != {
            "synthetic_dataset": SYNTHETIC_SEED,
            "isolation_forest": ISOLATION_FOREST_RANDOM_STATE,
        }
    ):
        raise AnomalyBenchmarkIncomplete(
            "Comparative reproducibility metadata is invalid.")
    baseline_digest = provenance.get("baseline_sha256")
    if (
        not isinstance(baseline_digest, str)
        or len(baseline_digest) != 64
        or any(char not in "0123456789abcdef" for char in baseline_digest)
        or provenance.get("historical_f28_baseline_sha256")
        != baseline_digest
        or provenance.get("features") != list(FEATURE_NAMES)
        or provenance.get("services") != list(EXPECTED_SERVICES)
    ):
        raise AnomalyBenchmarkIncomplete(
            "Comparative artifact provenance is invalid.")
    if artifact["schema_version"] >= 5:
        _validate_reproducibility_metadata(
            artifact.get("reproducibility"),
            baseline_sha256=baseline_digest,
        )
    split = provenance.get("split")
    if (
        not isinstance(split, dict)
        or split.get("method")
        != "chronological 70/30 split of distinct baseline timestamps, per service"
        or split.get("training_evaluation_overlap") is not False
        or not isinstance(split.get("training_timestamps"), list)
        or not isinstance(split.get("baseline_evaluation_timestamps"), list)
        or not isinstance(split.get("per_service"), dict)
    ):
        raise AnomalyBenchmarkIncomplete(
            "Comparative split metadata is invalid.")
    training_timestamps = set(split["training_timestamps"])
    baseline_evaluation_timestamps = set(
        split["baseline_evaluation_timestamps"]
    )
    if (
        not training_timestamps
        or training_timestamps.intersection(baseline_evaluation_timestamps)
        or not baseline_evaluation_timestamps
        or len(training_timestamps) != len(split["training_timestamps"])
        or len(baseline_evaluation_timestamps)
        != len(split["baseline_evaluation_timestamps"])
        or split.get("training_fraction") != TRAIN_FRACTION
        or not isinstance(split.get("per_service"), dict)
    ):
        raise AnomalyBenchmarkIncomplete(
            "Baseline split metadata is inconsistent.")
    sample_timestamps = set()
    sample_ids = set()
    methods = {"isolation_forest", "direction_aware_mad", "hybrid"}
    label_sources = {
        "assumed_normal_baseline",
        "assumed_normal_synthetic_control",
        "synthetic_pre_onset_normal",
        "synthetic_positive_by_construction",
        "unlabeled_unavailable",
    }
    for service in EXPECTED_SERVICES:
        service_split = split["per_service"].get(service)
        if not isinstance(service_split, dict):
            raise AnomalyBenchmarkIncomplete(
                f"Missing split metadata for {service}.")
        if service_split.get("training_row_count", 0) < (
            MINIMUM_RUNTIME_BASELINE_ROWS
        ) or service_split.get("evaluation_baseline_row_count", 0) < 1:
            raise AnomalyBenchmarkIncomplete(
                f"Invalid train/evaluation counts for {service}.")
        expected_service_rows = (
            provenance.get("baseline_row_count") // len(EXPECTED_SERVICES)
            if isinstance(provenance.get("baseline_row_count"), int)
            else 0
        )
        try:
            train_start = datetime.fromisoformat(
                service_split["training_timestamp_start"]
            )
            train_end = datetime.fromisoformat(
                service_split["training_timestamp_end"]
            )
            evaluation_start = datetime.fromisoformat(
                service_split["evaluation_timestamp_start"]
            )
            evaluation_end = datetime.fromisoformat(
                service_split["evaluation_timestamp_end"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AnomalyBenchmarkIncomplete(
                f"Invalid split timestamps for {service}.") from exc
        if (
            service_split["training_row_count"]
            + service_split["evaluation_baseline_row_count"]
            != expected_service_rows
            or _timestamp(train_start) not in training_timestamps
            or _timestamp(train_end) not in training_timestamps
            or _timestamp(evaluation_start) not in baseline_evaluation_timestamps
            or _timestamp(evaluation_end) not in baseline_evaluation_timestamps
            or train_end >= evaluation_start
        ):
            raise AnomalyBenchmarkIncomplete(
                f"Split boundaries are inconsistent for {service}.")

    for sample in artifact["samples"]:
        if not isinstance(sample, dict):
            raise AnomalyBenchmarkIncomplete("Every sample must be an object.")
        sample_id = sample.get("sample_id")
        if not isinstance(sample_id, str) or sample_id in sample_ids:
            raise AnomalyBenchmarkIncomplete(
                "Comparative sample IDs must be unique.")
        sample_ids.add(sample_id)
        if sample.get("service") not in EXPECTED_SERVICES:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} has unsupported service.")
        try:
            timestamp = datetime.fromisoformat(sample["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} timestamp is invalid.") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} timestamp is not timezone-aware.")
        sample_timestamps.add(_timestamp(timestamp))
        if sample.get("status") not in {"evaluated", "unavailable"}:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} status is invalid.")
        truth = sample.get("ground_truth_anomaly")
        if truth is not None and not isinstance(truth, bool):
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} label is invalid.")
        if sample.get("label_source") not in label_sources:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} label provenance is invalid.")
        if sample["status"] == "evaluated":
            features = sample.get("features")
            predictions = sample.get("predictions")
            if (
                not isinstance(truth, bool)
                or not isinstance(features, dict)
                or set(features) != set(FEATURE_NAMES)
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in features.values()
                )
                or not isinstance(predictions, dict)
                or set(predictions) != methods
            ):
                raise AnomalyBenchmarkIncomplete(
                    f"Evaluated sample {sample_id!r} is incomplete.")
            for method in methods:
                prediction = predictions[method]
                if (
                    not isinstance(prediction, dict)
                    or not isinstance(prediction.get("anomaly"), bool)
                ):
                    raise AnomalyBenchmarkIncomplete(
                        f"Sample {sample_id!r} has invalid {method} prediction.")
            mad = predictions["direction_aware_mad"]
            hybrid = predictions["hybrid"]
            if (
                not isinstance(mad.get("actionable_candidate"), bool)
                or hybrid["anomaly"] != (
                    predictions["isolation_forest"]["anomaly"]
                    or mad["anomaly"]
                )
                or mad["anomaly"] != bool(
                    sample.get("mad_actionable_features")
                    or sample.get("mad_informational_features")
                )
                or mad["actionable_candidate"] != bool(
                    sample.get("mad_actionable_features")
                )
                or not isinstance(sample.get("isolation_forest_score"), (int, float))
                or not math.isfinite(sample["isolation_forest_score"])
                or predictions["isolation_forest"]["anomaly"]
                != (sample["isolation_forest_score"] < 0)
            ):
                raise AnomalyBenchmarkIncomplete(
                    f"Sample {sample_id!r} method outputs are inconsistent.")
        elif (
            truth is not None
            or sample.get("predictions") is not None
            or sample.get("isolation_forest_score") is not None
            or sample.get("mad_actionable_features") is not None
            or sample.get("mad_informational_features") is not None
            or not isinstance(sample.get("failure_reason"), str)
            or not sample["failure_reason"]
            or (
                artifact["schema_version"] >= 3
                and sample.get("label_source") != "unlabeled_unavailable"
            )
        ):
            raise AnomalyBenchmarkIncomplete(
                f"Unavailable sample {sample_id!r} cannot have predictions or labels.")

    if training_timestamps.intersection(sample_timestamps):
        raise AnomalyBenchmarkIncomplete(
            "Training and evaluation timestamps overlap.")
    if not artifact["samples"]:
        raise AnomalyBenchmarkIncomplete(
            "Comparative artifact must contain samples.")
    expected_dataset_digest = _comparative_dataset_digest(artifact["samples"])
    if provenance.get("dataset_sha256") != expected_dataset_digest:
        raise AnomalyBenchmarkIncomplete(
            "Dataset digest does not match the saved shared inputs.")
    baseline_holdout_samples = [
        sample for sample in artifact["samples"]
        if sample.get("scenario")
        == "chronological_baseline_holdout_assumed_normal"
    ]
    if {
        _timestamp(datetime.fromisoformat(sample["timestamp"]))
        for sample in baseline_holdout_samples
    } != baseline_evaluation_timestamps:
        raise AnomalyBenchmarkIncomplete(
            "Baseline evaluation timestamps do not match saved holdout samples.")
    for service in EXPECTED_SERVICES:
        service_holdout = [
            sample for sample in baseline_holdout_samples
            if sample["service"] == service
        ]
        service_split = split["per_service"][service]
        if (
            len(service_holdout)
            != service_split["evaluation_baseline_row_count"]
            or len({
                sample["timestamp"] for sample in service_holdout
            }) != len(service_holdout)
            or min(
                datetime.fromisoformat(sample["timestamp"])
                for sample in service_holdout
            ) != datetime.fromisoformat(
                service_split["evaluation_timestamp_start"]
            )
            or max(
                datetime.fromisoformat(sample["timestamp"])
                for sample in service_holdout
            ) != datetime.fromisoformat(
                service_split["evaluation_timestamp_end"]
            )
        ):
            raise AnomalyBenchmarkIncomplete(
                f"Baseline holdout rows do not match split for {service}.")
    computed_metrics = _comparison_metrics(artifact["samples"])
    if artifact.get("metrics") != computed_metrics:
        raise AnomalyBenchmarkIncomplete(
            "Stored comparative metrics do not match samples.")
    return computed_metrics


def validate_anomaly_artifact(artifact: Any) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise AnomalyBenchmarkIncomplete("Artifact must be a JSON object.")
    if artifact.get("benchmark") == "three_way_synthetic_anomaly_comparison":
        return validate_comparative_anomaly_artifact(artifact)
    required = {
        "schema_version", "run", "detector", "dataset_provenance",
        "metric_scope", "samples", "unavailable_checks", "metrics",
        "limitations",
    }
    missing = required - artifact.keys()
    if missing:
        raise AnomalyBenchmarkIncomplete(
            f"Artifact is missing {', '.join(sorted(missing))}.")
    if (
        type(artifact["schema_version"]) is not int
        or artifact["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS
    ):
        raise AnomalyBenchmarkIncomplete("Unsupported artifact schema version.")
    if (
        not isinstance(artifact["run"], dict)
        or artifact["run"].get("evaluation_mode") != "synthetic"
        or not isinstance(artifact["detector"], dict)
        or not isinstance(artifact["dataset_provenance"], dict)
        or not isinstance(artifact["metric_scope"], dict)
        or artifact["detector"].get("policy_threshold") != POLICY_THRESHOLD
        or artifact["detector"].get("identity")
        != "app.services.anomaly_policy.DirectionAwareAnomalyPolicy"
        or artifact["metric_scope"].get("primary")
        != "point-level anomaly prediction"
        or artifact["run"].get("baseline_sha256")
        != artifact["dataset_provenance"].get("baseline_sha256")
        or not isinstance(artifact["unavailable_checks"], list)
    ):
        raise AnomalyBenchmarkIncomplete(
            "Artifact mode, detector configuration, or metric scope is invalid.")
    digest = artifact["run"].get("baseline_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise AnomalyBenchmarkIncomplete("Baseline digest is invalid.")
    if artifact["schema_version"] >= 2:
        _validate_reproducibility_metadata(
            artifact.get("reproducibility"),
            baseline_sha256=digest,
        )
    unavailable_checks = {
        check.get("check_id"): check
        for check in artifact["unavailable_checks"]
        if isinstance(check, dict)
    }
    if set(unavailable_checks) != {
        "runtime_minimum_baseline", "available_disk_space"
    } or unavailable_checks["runtime_minimum_baseline"].get("status") != (
        "unavailable"
    ) or unavailable_checks["runtime_minimum_baseline"].get(
        "required_rows"
    ) != MINIMUM_RUNTIME_BASELINE_ROWS or unavailable_checks[
        "available_disk_space"
    ].get("status") != "unsupported" or any(
        check.get("ground_truth_anomaly") is not None
        for check in unavailable_checks.values()
    ):
        raise AnomalyBenchmarkIncomplete(
            "Unsupported and unavailable checks must remain explicitly "
            "unlabeled.")

    samples = artifact["samples"]
    if not isinstance(samples, list) or not samples:
        raise AnomalyBenchmarkIncomplete("Artifact samples must be a nonempty list.")
    seen = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise AnomalyBenchmarkIncomplete("Every sample must be an object.")
        sample_id = sample.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id or sample_id in seen:
            raise AnomalyBenchmarkIncomplete("Sample IDs must be unique strings.")
        seen.add(sample_id)
        try:
            stamp = datetime.fromisoformat(sample["timestamp"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} has an invalid timestamp.") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} timestamp must be timezone-aware.")
        if sample.get("service") not in EXPECTED_SERVICES:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} has an unsupported service.")
        if sample.get("status") not in {"evaluated", "unavailable"}:
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} has an invalid status.")
        truth = sample.get("ground_truth_anomaly")
        if truth is not None and not isinstance(truth, bool):
            raise AnomalyBenchmarkIncomplete(
                f"Sample {sample_id!r} has an invalid ground-truth label.")
        if sample["status"] == "evaluated":
            if not isinstance(truth, bool) or not isinstance(
                sample.get("predicted_anomaly"), bool
            ) or not isinstance(sample.get("predicted_actionable"), bool):
                raise AnomalyBenchmarkIncomplete(
                    f"Evaluated sample {sample_id!r} needs boolean labels "
                    "and predictions.")
            output = sample.get("detector_output")
            if not isinstance(output, dict):
                raise AnomalyBenchmarkIncomplete(
                    f"Evaluated sample {sample_id!r} is missing detector output.")
            if not isinstance(sample.get("features"), dict) or set(
                sample["features"]
            ) != set(FEATURE_NAMES):
                raise AnomalyBenchmarkIncomplete(
                    f"Evaluated sample {sample_id!r} has incomplete features.")
            expected_anomaly = bool(
                output.get("actionable_candidates")
                or output.get("informational_deviations")
            )
            expected_actionable = bool(output.get("actionable_candidates"))
            if (
                expected_anomaly != sample["predicted_anomaly"]
                or expected_actionable != sample["predicted_actionable"]
            ):
                raise AnomalyBenchmarkIncomplete(
                    f"Sample {sample_id!r} prediction contradicts detector output.")
        elif (
            truth is not None
            or sample.get("predicted_anomaly") is not None
            or sample.get("predicted_actionable") is not None
            or sample.get("detector_output") is not None
        ):
            raise AnomalyBenchmarkIncomplete(
                f"Unavailable sample {sample_id!r} cannot have a prediction "
                "or ground-truth label.")

    computed = _metrics(samples)
    if artifact["metrics"] != computed:
        raise AnomalyBenchmarkIncomplete(
            "Stored metrics do not match sample labels and predictions.")
    return computed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    baseline_group = parser.add_mutually_exclusive_group()
    baseline_group.add_argument(
        "--baseline",
        default=str(BASELINE),
        help="Approved baseline input; exact pinned v6 identity is required.",
    )
    baseline_group.add_argument(
        "--exploratory-baseline",
        metavar="CSV",
        help=(
            "Explicitly evaluate a non-approved CSV as exploratory only; "
            "the supplied file is structurally validated and hashed."
        ),
    )
    parser.add_argument("--output")
    parser.add_argument(
        "--three-way",
        action="store_true",
        help=(
            "Build the versioned Isolation Forest/MAD/hybrid comparison "
            "instead of the historical F.28 MAD artifact."
        ),
    )
    parser.add_argument(
        "--validate",
        metavar="ARTIFACT",
        help="Validate an existing artifact and independently recompute metrics.",
    )
    args = parser.parse_args()
    try:
        if args.validate:
            artifact_path = Path(args.validate)
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            metrics = validate_anomaly_artifact(artifact)
            measured_count = (
                metrics["eligible_observation_count"]
                if artifact.get("benchmark")
                == "three_way_synthetic_anomaly_comparison"
                else metrics["point_anomaly"]["sample_count"]
            )
            print(
                "VALID: "
                f"{measured_count} labeled points; "
                f"metrics recomputed from {artifact_path}"
            )
            print(_artifact_freshness(artifact))
            print(json.dumps(metrics, indent=2))
            return 0
        exploratory = args.exploratory_baseline is not None
        baseline_path = (
            args.exploratory_baseline
            if exploratory else args.baseline
        )
        result = (
            build_comparative_anomaly_benchmark(
                baseline_path=baseline_path,
                exploratory=exploratory,
            )
            if args.three_way
            else build_anomaly_benchmark(
                baseline_path=baseline_path,
                exploratory=exploratory,
            )
        )
        validate_anomaly_artifact(result)
        output = Path(
            args.output
            or (COMPARATIVE_OUTPUT if args.three_way else OUTPUT)
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(result, indent=2) + "\n")
    except (
        AnomalyBenchmarkIncomplete,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 2

    metrics = result["metrics"]
    if args.three_way:
        print("Synthetic three-way anomaly comparison")
        print(f"Artifact: {output}")
        print(
            "Eligible points: "
            f"{metrics['eligible_observation_count']}; unavailable: "
            f"{metrics['unavailable_observation_count']}"
        )
        for method, confusion in metrics["point_anomaly"].items():
            print(
                f"{method}: TP={confusion['true_positives']} "
                f"FP={confusion['false_positives']} "
                f"TN={confusion['true_negatives']} "
                f"FN={confusion['false_negatives']} "
                f"precision={confusion['precision']} "
                f"recall={confusion['recall']} F1={confusion['f1']} "
                f"FPR={confusion['false_positive_rate']}"
            )
        print(
            "Synthetic/assumed-label comparison only; it does not establish "
            "production detection quality."
        )
        print(f"Evaluation mode: {result['reproducibility']['evaluation_mode']}")
        return 0

    point = metrics["point_anomaly"]
    delay = metrics["gradual_drift_detection_delay"]
    print("Synthetic point-level anomaly evaluation")
    print(
        f"Confusion matrix: TP={point['true_positives']} "
        f"FP={point['false_positives']} TN={point['true_negatives']} "
        f"FN={point['false_negatives']} "
        f"(n={point['sample_count']}, unavailable="
        f"{metrics['unavailable_point_count']})"
    )
    print(
        f"Precision={point['precision']} Recall={point['recall']} "
        f"F1={point['f1']} FPR={point['false_positive_rate']}"
    )
    print(
        "Gradual-drift delay: "
        f"{delay['mean_seconds']}s over {delay['detected_episode_count']}/"
        f"{delay['episode_count']} episodes"
    )
    print(
        "Actionable candidate counts: "
        f"{metrics['actionable_candidate']['true_positives']} TP, "
        f"{metrics['actionable_candidate']['false_positives']} FP; "
        "informational deviations are counted as point anomalies, not actions."
    )
    print(f"Artifact: {output}")
    print(f"Evaluation mode: {result['reproducibility']['evaluation_mode']}")
    print("Scope: synthetic telemetry points only; no live integrations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
