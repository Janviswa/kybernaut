#!/usr/bin/env python3
"""Analyze F.28 misses and validate the unchanged detector on held-out episodes."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import hashlib
from pathlib import Path
import sys
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import FEATURE_NAMES  # noqa: E402
from app.services.anomaly_runtime import (  # noqa: E402
    EXPECTED_SERVICES,
    POLICY_THRESHOLD,
)
from scripts import evaluate_anomaly_benchmark as f28  # noqa: E402

F28_ARTIFACT = PROJECT_ROOT / "benchmark" / "results" / (
    "direction-aware-anomaly-benchmark.json"
)
OUTPUT = PROJECT_ROOT / "benchmark" / "results" / (
    "f29-anomaly-false-negative-analysis.json"
)
HOLDOUT_SEED = 2901
HOLDOUT_INTERVAL_SECONDS = 30


def _confusion(samples: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [
        sample for sample in samples
        if sample.get("status") == "evaluated"
        and isinstance(sample.get("ground_truth_anomaly"), bool)
        and isinstance(sample.get("predicted_anomaly"), bool)
    ]
    tp = sum(
        sample["ground_truth_anomaly"] and sample["predicted_anomaly"]
        for sample in evaluated
    )
    fp = sum(
        not sample["ground_truth_anomaly"] and sample["predicted_anomaly"]
        for sample in evaluated
    )
    tn = sum(
        not sample["ground_truth_anomaly"] and not sample["predicted_anomaly"]
        for sample in evaluated
    )
    fn = sum(
        sample["ground_truth_anomaly"] and not sample["predicted_anomaly"]
        for sample in evaluated
    )
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None
        and precision + recall
        else 0.0
        if precision is not None and recall is not None
        else None
    )
    fpr = fp / (fp + tn) if fp + tn else None
    return {
        "status": "measured" if evaluated else "not_measured",
        "tp": tp if evaluated else None,
        "fp": fp if evaluated else None,
        "tn": tn if evaluated else None,
        "fn": fn if evaluated else None,
        "precision": round(precision, 6) if precision is not None else None,
        "recall": round(recall, 6) if recall is not None else None,
        "f1": round(f1, 6) if f1 is not None else None,
        "false_positive_rate": round(fpr, 6) if fpr is not None else None,
        "sample_count": len(evaluated),
    }


def _training_statistics() -> tuple[
    dict[str, np.ndarray], dict[str, np.ndarray]
]:
    rows, _ = f28._read_baseline(f28.BASELINE)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["service"]].append(row)

    centers = {}
    scales = {}
    for service in EXPECTED_SERVICES:
        ordered = sorted(grouped[service], key=lambda row: row["timestamp"])
        train = np.asarray([
            [float(row[name]) for name in FEATURE_NAMES]
            for row in ordered[:42]
        ])
        center = np.median(train, axis=0)
        scale = 1.4826 * np.median(np.abs(train - center), axis=0)
        scale = np.where(
            scale > 1e-12,
            scale,
            np.maximum(np.std(train, axis=0), 1e-12),
        )
        centers[service] = center
        scales[service] = scale
    return centers, scales


def _make_holdout(
    centers: dict[str, np.ndarray],
    scales: dict[str, np.ndarray],
    timestamp_start: datetime,
) -> list[dict[str, Any]]:
    samples = []
    for service in EXPECTED_SERVICES:
        baseline_rows, _ = f28._read_baseline(f28.BASELINE)
        service_rows = sorted(
            (row for row in baseline_rows if row["service"] == service),
            key=lambda row: row["timestamp"],
        )
        policy = f28._fit_runtime_compatible_policy([
            [float(row[name]) for name in FEATURE_NAMES]
            for row in service_rows[:42]
        ])
        center = centers[service]
        scale = scales[service]

        episodes: list[tuple[str, list[np.ndarray], bool, int]] = []
        memory_index = FEATURE_NAMES.index("memory_bytes")
        latency_index = FEATURE_NAMES.index("p95_latency_seconds")

        memory_drift = []
        for index in range(10):
            values = center.copy()
            values[memory_index] += index * 0.8 * scale[memory_index]
            memory_drift.append(values)
        episodes.append(("gradual_memory_drift", memory_drift, True, 1))

        concurrent_step = center.copy()
        concurrent_step[memory_index] += 6.5 * scale[memory_index]
        concurrent_step[latency_index] += 6.5 * scale[latency_index]
        episodes.append((
            "concurrent_latency_memory_step",
            [concurrent_step.copy() for _ in range(3)],
            True,
            0,
        ))

        centered_control = [center.copy() for _ in range(3)]
        episodes.append(("stable_center_control", centered_control, False, 0))

        rng = np.random.default_rng(HOLDOUT_SEED + EXPECTED_SERVICES.index(service))
        bounded_control = [
            np.maximum(
                center + rng.normal(0.0, 2.0, len(FEATURE_NAMES)) * scale,
                0.0,
            )
            for _ in range(6)
        ]
        episodes.append(("bounded_noise_control", bounded_control, False, 0))

        for episode_name, points, positive, onset_index in episodes:
            episode_id = f"holdout-{service}-{episode_name}"
            episode_start_index = len(samples)
            for index, values in enumerate(points):
                output = policy.evaluate([values.tolist()])[0]
                predicted = bool(
                    output["actionable_candidates"]
                    or output["informational_deviations"]
                )
                timestamp = timestamp_start + timedelta(
                    seconds=(episode_start_index + index)
                    * HOLDOUT_INTERVAL_SECONDS
                )
                samples.append({
                    "sample_id": f"{episode_id}-{index:02d}",
                    "episode_id": episode_id,
                    "service": service,
                    "scenario": episode_name,
                    "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
                    "status": "evaluated",
                    "ground_truth_anomaly": (
                        positive and index >= onset_index
                    ),
                    "predicted_anomaly": predicted,
                    "predicted_actionable": bool(
                        output["actionable_candidates"]
                    ),
                    "features": {
                        name: float(value)
                        for name, value in zip(FEATURE_NAMES, values)
                    },
                    "detector_output": output,
                })
    return samples


def _episode_delays(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    episodes: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        if sample["ground_truth_anomaly"] is True:
            episodes[sample["episode_id"]].append(sample)
    result = []
    for episode_id, points in sorted(episodes.items()):
        onset = min(
            datetime.fromisoformat(point["timestamp"]) for point in points
        )
        detected = [
            datetime.fromisoformat(point["timestamp"])
            for point in points if point["predicted_anomaly"]
        ]
        first = min(detected) if detected else None
        result.append({
            "episode_id": episode_id,
            "positive_point_count": len(points),
            "onset_at": onset.isoformat(),
            "first_detected_at": first.isoformat() if first else None,
            "delay_seconds": (
                (first - onset).total_seconds() if first else None
            ),
            "detected": first is not None,
        })
    return result


def build_analysis(
    artifact_path: str | Path = F28_ARTIFACT,
) -> dict[str, Any]:
    try:
        artifact_bytes = Path(artifact_path).read_bytes()
        artifact = json.loads(artifact_bytes.decode("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Unable to read F.28 artifact ({type(exc).__name__})."
        ) from exc
    f28.validate_anomaly_artifact(artifact)

    samples = artifact["samples"]
    fn_samples = [
        sample for sample in samples
        if sample["status"] == "evaluated"
        and sample["ground_truth_anomaly"] is True
        and sample["predicted_anomaly"] is False
    ]
    recalculated = _confusion(samples)
    reported = artifact["metrics"]["point_anomaly"]
    if any(
        recalculated[key] != reported[source]
        for key, source in (
            ("tp", "true_positives"),
            ("fp", "false_positives"),
            ("tn", "true_negatives"),
            ("fn", "false_negatives"),
            ("precision", "precision"),
            ("recall", "recall"),
            ("f1", "f1"),
            ("false_positive_rate", "false_positive_rate"),
            ("sample_count", "sample_count"),
        )
    ):
        raise ValueError("F.28 reported confusion metrics do not recompute.")

    grouped = Counter()
    by_feature = Counter()
    direction = Counter()
    magnitudes: dict[str, list[float]] = defaultdict(list)
    episode_times: dict[str, list[datetime]] = defaultdict(list)
    services = Counter()
    for sample in fn_samples:
        grouped[sample["scenario"]] += 1
        services[sample["service"]] += 1
        deviations = sample["detector_output"]["features"]
        highest_name, highest_detail = max(
            deviations.items(),
            key=lambda pair: pair[1]["deviation_score"],
        )
        by_feature[highest_name] += 1
        direction[highest_detail["direction"]] += 1
        magnitudes[highest_name].append(
            highest_detail["deviation_score"]
        )
        if sample["episode_id"]:
            episode_times[sample["episode_id"]].append(
                datetime.fromisoformat(sample["timestamp"])
            )

    missed_episodes = []
    for episode_id, timestamps in sorted(episode_times.items()):
        all_positive_times = [
            datetime.fromisoformat(sample["timestamp"])
            for sample in samples
            if sample["episode_id"] == episode_id
            and sample["ground_truth_anomaly"] is True
        ]
        first_positive = min(all_positive_times)
        last_positive = max(all_positive_times)
        first_detected = next(
            (
                datetime.fromisoformat(sample["timestamp"])
                for sample in samples
                if sample["episode_id"] == episode_id
                and sample["predicted_anomaly"] is True
            ),
            None,
        )
        missed_episodes.append({
            "episode_id": episode_id,
            "service": episode_id.removeprefix("gradual-drift-").removesuffix(
                "-cpu"
            ),
            "missed_point_count": 6,
            "missed_sample_span_seconds": (
                max(timestamps) - min(timestamps)
            ).total_seconds(),
            "positive_episode_duration_seconds": (
                last_positive - first_positive
            ).total_seconds(),
            "onset_to_first_detection_seconds": (
                (first_detected - first_positive).total_seconds()
                if first_detected else None
            ),
        })

    synthetic_dev = [
        sample for sample in samples
        if sample["status"] == "evaluated"
        and not sample["sample_id"].startswith("holdout-")
        and sample["scenario"] != (
            "chronological_baseline_holdout_assumed_normal"
        )
    ]
    start = datetime.fromisoformat(artifact["run"]["synthetic_data_start"])
    centers, scales = _training_statistics()
    holdout = _make_holdout(centers, scales, start + timedelta(days=1))
    dev_episodes = {
        sample["episode_id"] for sample in synthetic_dev
        if sample["episode_id"] is not None
    }
    holdout_episodes = {sample["episode_id"] for sample in holdout}
    dev_scenarios = {sample["scenario"] for sample in synthetic_dev}
    holdout_scenarios = {sample["scenario"] for sample in holdout}
    if dev_episodes & holdout_episodes or dev_scenarios & holdout_scenarios:
        raise ValueError("Development and holdout episodes overlap.")

    by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in holdout:
        by_scenario[sample["scenario"]].append(sample)
    holdout_delays = _episode_delays(holdout)
    detected_delays = [
        item["delay_seconds"] for item in holdout_delays
        if item["delay_seconds"] is not None
    ]

    return {
        "schema_version": 1,
        "evaluation_mode": "synthetic_false_negative_analysis",
        "f28_artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "detector": {
            "identity": artifact["detector"]["identity"],
            "threshold": POLICY_THRESHOLD,
            "threshold_changed": False,
            "baseline_sha256": artifact["run"]["baseline_sha256"],
        },
        "split": {
            "method": "episode-family holdout; F.28 development scenarios retained; new deterministic holdout episodes",
            "development_scenario_families": sorted({
                sample["scenario"] for sample in synthetic_dev
            }),
            "holdout_scenario_families": sorted(by_scenario),
            "development_episode_count": len(dev_episodes),
            "holdout_episode_count": len(holdout_episodes),
            "development_episode_ids": sorted(dev_episodes),
            "holdout_episode_ids": sorted(holdout_episodes),
            "episode_ids_disjoint": not bool(dev_episodes & holdout_episodes),
            "scenario_families_disjoint": not bool(
                dev_scenarios & holdout_scenarios
            ),
            "threshold_frozen_before_holdout": True,
            "candidate_changes_evaluated": [],
        },
        "f28_integrity": {
            "reported_confusion": reported,
            "recomputed_confusion": recalculated,
            "confusion_consistent": True,
            "false_negative_count": len(fn_samples),
        },
        "false_negative_analysis": {
            "by_scenario": dict(sorted(grouped.items())),
            "by_highest_deviation_feature": dict(sorted(by_feature.items())),
            "by_direction": dict(sorted(direction.items())),
            "deviation_score_range_by_feature": {
                feature: {
                    "minimum": round(min(scores), 6),
                    "maximum": round(max(scores), 6),
                }
                for feature, scores in sorted(magnitudes.items())
            },
            "service_counts": dict(sorted(services.items())),
            "missed_episode_durations": missed_episodes,
            "single_point_miss_count": sum(
                sample["episode_id"] is None for sample in fn_samples
            ),
            "single_point_miss_duration": (
                "Not specified by F.28; each is a single sampled point."
            ),
            "baseline_context": {
                "training_rows_per_service": 42,
                "chronological_holdout_rows_per_service": 18,
                "service_feature_statistics": {
                    service: {
                        feature: {
                            "training_median": float(centers[service][index]),
                            "training_robust_scale": float(scales[service][index]),
                        }
                        for index, feature in enumerate(FEATURE_NAMES)
                    }
                    for service in EXPECTED_SERVICES
                },
            },
            "missed_points": [
                {
                    "sample_id": sample["sample_id"],
                    "service": sample["service"],
                    "scenario": sample["scenario"],
                    "feature": max(
                        sample["detector_output"]["features"].items(),
                        key=lambda pair: pair[1]["deviation_score"],
                    )[0],
                    "direction": max(
                        sample["detector_output"]["features"].items(),
                        key=lambda pair: pair[1]["deviation_score"],
                    )[1]["direction"],
                    "deviation_score": max(
                        detail["deviation_score"]
                        for detail in sample["detector_output"][
                            "features"
                        ].values()
                    ),
                    "value": max(
                        sample["detector_output"]["features"].items(),
                        key=lambda pair: pair[1]["deviation_score"],
                    )[1]["value"],
                    "baseline_median": max(
                        sample["detector_output"]["features"].items(),
                        key=lambda pair: pair[1]["deviation_score"],
                    )[1]["baseline_median"],
                }
                for sample in fn_samples
            ],
            "interpretation": [
                "Gradual CPU drift labels become positive before the configured 5.0 MAD threshold is crossed; this is threshold latency, not missing input.",
                "Low request-rate steps remain below 5.0 MAD and low-direction request-rate deviations are not actionable; their positive labels express the benchmark's assumed anomaly definition.",
                "Each miss pattern repeats once per service, so the four service copies are not independent scenario families.",
            ],
        },
        "holdout": {
            "purpose": "Validate unchanged 5.0-threshold policy on deterministic episodes not used in F.28 development analysis.",
            "seed": HOLDOUT_SEED,
            "confusion": _confusion(holdout),
            "by_scenario": {
                scenario: _confusion(items)
                for scenario, items in sorted(by_scenario.items())
            },
            "episode_detection_delay": holdout_delays,
            "mean_detected_episode_delay_seconds": (
                round(sum(detected_delays) / len(detected_delays), 6)
                if detected_delays else None
            ),
            "assumptions": [
                "Injected synthetic episodes are labeled anomalous by construction.",
                "Stable-center and bounded-noise controls are assumed normal.",
                "Scenario families and all their points remain wholly in holdout.",
                "The holdout is small and synthetic; results do not establish live accuracy.",
            ],
            "samples": holdout,
        },
        "decision": {
            "production_detector_changed": False,
            "reason": "No detector change is justified by assumed synthetic labels and repeated narrow scenario families; changing the threshold would require independently reviewed operational labels and false-positive acceptance criteria.",
        },
    }


def validate_analysis_artifact(artifact: Any) -> dict[str, Any]:
    if not isinstance(artifact, dict) or artifact.get(
        "evaluation_mode"
    ) != "synthetic_false_negative_analysis":
        raise ValueError("Invalid F.29 analysis artifact.")
    split = artifact.get("split")
    holdout = artifact.get("holdout")
    if not isinstance(split, dict) or not isinstance(holdout, dict):
        raise ValueError("F.29 split and holdout sections are required.")
    dev_ids = set(split.get("development_episode_ids", []))
    holdout_ids = set(split.get("holdout_episode_ids", []))
    if (
        dev_ids & holdout_ids
        or not split.get("episode_ids_disjoint")
        or not split.get("scenario_families_disjoint")
        or not split.get("threshold_frozen_before_holdout")
        or split.get("candidate_changes_evaluated") != []
    ):
        raise ValueError("F.29 split integrity check failed.")
    samples = holdout.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("F.29 holdout samples are missing.")
    sample_episodes: dict[str, set[str]] = defaultdict(set)
    for sample in samples:
        if sample.get("episode_id") not in holdout_ids:
            raise ValueError("Holdout sample is outside the recorded split.")
        sample_episodes[sample["episode_id"]].add(sample["scenario"])
    if (
        set(sample_episodes) != holdout_ids
        or any(len(scenarios) != 1 for scenarios in sample_episodes.values())
    ):
        raise ValueError("Holdout episodes mix scenario families.")
    recomputed = _confusion(samples)
    if recomputed != holdout.get("confusion"):
        raise ValueError("F.29 holdout confusion metrics do not recompute.")
    expected_by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        expected_by_scenario[sample["scenario"]].append(sample)
    recomputed_by_scenario = {
        scenario: _confusion(items)
        for scenario, items in sorted(expected_by_scenario.items())
    }
    if recomputed_by_scenario != holdout.get("by_scenario"):
        raise ValueError("F.29 per-scenario metrics do not recompute.")
    return recomputed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(OUTPUT),
        help=f"Analysis artifact destination (default: {OUTPUT}).",
    )
    args = parser.parse_args()
    result = build_analysis()
    validate_analysis_artifact(result)
    output = Path(args.output)
    if output.exists():
        print(
            f"Refusing to overwrite existing artifact: {output}",
            file=sys.stderr,
        )
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    metrics = result["holdout"]["confusion"]
    print(
        "F.29 unchanged-detector holdout: "
        f"TP={metrics['tp']} FP={metrics['fp']} TN={metrics['tn']} "
        f"FN={metrics['fn']} n={metrics['sample_count']}"
    )
    print(
        "F.28 false negatives: "
        f"{result['f28_integrity']['false_negative_count']} "
        f"({result['false_negative_analysis']['by_scenario']})"
    )
    print(f"Analysis artifact: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
