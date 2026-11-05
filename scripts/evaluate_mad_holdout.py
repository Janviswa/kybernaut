"""Evaluate MAD thresholds on chronologically held-out v6 telemetry."""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import FEATURE_NAMES
from app.services.anomaly_runtime import (
    BaselineIntegrityError,
    read_validated_baseline,
)
from scripts.evaluate_anomaly_benchmark import (
    build_reproducibility_metadata,
)

BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
TRAIN_FRACTION = 0.70
THRESHOLDS = (3, 4, 5)


def fit_robust_stats(X):
    median = np.median(X, axis=0)
    mad = np.median(np.abs(X - median), axis=0)
    scale = 1.4826 * mad

    # Fallback for features with zero or near-zero MAD.
    std = np.std(X, axis=0)
    scale = np.where(scale > 1e-12, scale, np.maximum(std, 1e-12))
    return median, scale


def deviation_scores(X, median, scale):
    return np.abs((X - median) / scale)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", default=str(BASELINE))
    group.add_argument(
        "--exploratory-baseline",
        metavar="CSV",
        help="Evaluate a structurally valid, non-approved baseline explicitly.",
    )
    args = parser.parse_args(argv)
    exploratory = args.exploratory_baseline is not None
    baseline_path = args.exploratory_baseline or args.baseline
    try:
        rows, baseline_sha256 = read_validated_baseline(
            baseline_path, exploratory=exploratory
        )
    except BaselineIntegrityError as exc:
        parser.error(f"Baseline integrity validation failed ({exc}).")

    timestamps = sorted({row["timestamp"] for row in rows})
    if len(timestamps) < 10:
        raise ValueError("Not enough timestamps for a chronological split")

    split_index = int(len(timestamps) * TRAIN_FRACTION)
    train_times = set(timestamps[:split_index])
    test_times = set(timestamps[split_index:])

    train = [r for r in rows if r["timestamp"] in train_times]
    test = [r for r in rows if r["timestamp"] in test_times]

    X_train = np.array([
        [float(row[name]) for name in FEATURE_NAMES] for row in train
    ])
    X_test = np.array([
        [float(row[name]) for name in FEATURE_NAMES] for row in test
    ])

    if not np.isfinite(X_train).all() or not np.isfinite(X_test).all():
        raise ValueError("Baseline contains invalid feature values")

    median, scale = fit_robust_stats(X_train)
    test_scores = deviation_scores(X_test, median, scale)

    print(f"Baseline: {Path(baseline_path).name}")
    print(
        "Reproducibility metadata: "
        + json.dumps(build_reproducibility_metadata(
            baseline_path=baseline_path,
            baseline_sha256=baseline_sha256,
            exploratory=exploratory,
            evaluator_path=__file__,
            configuration={
                "training_fraction": TRAIN_FRACTION,
                "thresholds_analyzed": list(THRESHOLDS),
                "feature_names": list(FEATURE_NAMES),
            },
            random_seeds={},
            label_provenance={
                "baseline_holdout": (
                    "assumed_normal_not_independently_reviewed"
                ),
                "synthetic_probes": "synthetic_by_construction",
                "independently_reviewed_labels_present": False,
            },
        ), sort_keys=True)
    )
    print(f"Training rows: {len(X_train)}")
    print(f"Held-out rows: {len(X_test)}")
    print("\nHeld-out normal telemetry alert rates")

    for threshold in THRESHOLDS:
        per_feature = (test_scores > threshold).sum(axis=0)
        any_feature = (test_scores > threshold).any(axis=1).sum()

        print(f"\nThreshold {threshold}:")
        for name, count in zip(FEATURE_NAMES, per_feature):
            print(f"  {name}: {count}/{len(X_test)} ({count / len(X_test):.1%})")
        print(
            f"  Any feature: {any_feature}/{len(X_test)} "
            f"({any_feature / len(X_test):.1%})"
        )

    center = median
    probes = np.array([
        [center[0] * 10, center[1], center[2], center[3]],
        [center[0], center[1] * 10, center[2], center[3]],
        [center[0], center[1], center[2] * 2, center[3]],
        [center[0], center[1], center[2], center[3] * 10],
        [100.0, 8.0, 2_000_000_000.0, 4.0],
    ])
    names = [
        "Traffic spike", "Latency spike", "Memory spike",
        "CPU spike", "Combined extreme",
    ]
    probe_scores = deviation_scores(probes, median, scale)

    print("\nSynthetic probe results (not real incidents)")
    for name, scores in zip(names, probe_scores):
        largest = int(np.argmax(scores))
        print(
            f"  {name}: max-z={scores[largest]:.2f}, "
            f"feature={FEATURE_NAMES[largest]}"
        )
        for threshold in THRESHOLDS:
            flagged = bool((scores > threshold).any())
            print(f"    threshold {threshold}: flagged={flagged}")


if __name__ == "__main__":
    main()
