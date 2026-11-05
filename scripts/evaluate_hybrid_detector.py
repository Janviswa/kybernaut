"""Compare Isolation Forest with an experimental MAD hybrid."""

import csv
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import (
    FEATURE_NAMES,
    TelemetryAnomalyDetector,
)

BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
TRAIN_FRACTION = 0.70
THRESHOLDS = (3, 4, 5)


def robust_stats(X):
    median = np.median(X, axis=0)
    mad = np.median(np.abs(X - median), axis=0)
    scale = 1.4826 * mad
    std = np.std(X, axis=0)
    scale = np.where(scale > 1e-12, scale, np.maximum(std, 1e-12))
    return median, scale


def main():
    with BASELINE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    timestamps = sorted({r["timestamp"] for r in rows})
    split = int(len(timestamps) * TRAIN_FRACTION)
    train_times = set(timestamps[:split])

    train = [r for r in rows if r["timestamp"] in train_times]
    test = [r for r in rows if r["timestamp"] not in train_times]

    X_train = np.array([
        [float(r[name]) for name in FEATURE_NAMES] for r in train
    ])
    X_test = np.array([
        [float(r[name]) for name in FEATURE_NAMES] for r in test
    ])

    detector = TelemetryAnomalyDetector()
    detector.fit(X_train.tolist())
    if_results = detector.score(X_test.tolist())
    if_flags = np.array([r["anomaly"] for r in if_results], dtype=bool)

    median, scale = robust_stats(X_train)
    mad_scores = np.abs((X_test - median) / scale)

    print(f"Baseline: {BASELINE.name}")
    print(f"Training rows: {len(X_train)}")
    print(f"Held-out rows: {len(X_test)}")
    print("\nHeld-out comparison (unlabelled telemetry)")
    print(f"Isolation Forest flagged: {if_flags.sum()}/{len(if_flags)}")

    for threshold in THRESHOLDS:
        mad_flags = (mad_scores > threshold).any(axis=1)
        hybrid_flags = if_flags | mad_flags
        print(f"\nMAD threshold: {threshold}")
        print(f"  MAD-only flagged: {mad_flags.sum()}/{len(mad_flags)}")
        print(f"  Hybrid flagged: {hybrid_flags.sum()}/{len(hybrid_flags)}")
        print(f"  Hybrid alert rate: {hybrid_flags.mean():.1%}")

    # Synthetic probes are evaluated separately, never added to baseline.
    center = median
    probes = np.array([
        [center[0] * 10, center[1], center[2], center[3]],
        [center[0], center[1] * 10, center[2], center[3]],
        [center[0], center[1], center[2] * 2, center[3]],
        [center[0], center[1], center[2], center[3] * 10],
        [100.0, 8.0, 2_000_000_000.0, 4.0],
    ])
    probe_names = [
        "Traffic spike", "Latency spike", "Memory spike",
        "CPU spike", "Combined extreme",
    ]
    probe_if = np.array([
        r["anomaly"] for r in detector.score(probes.tolist())
    ])
    probe_mad = np.abs((probes - median) / scale)

    print("\nSynthetic probe comparison (not real incidents)")
    for i, name in enumerate(probe_names):
        print(f"  {name}: IF={bool(probe_if[i])}")
        for threshold in THRESHOLDS:
            mad_flag = bool((probe_mad[i] > threshold).any())
            hybrid_flag = bool(probe_if[i] or mad_flag)
            print(
                f"    MAD>{threshold}: {mad_flag}; "
                f"hybrid: {hybrid_flag}"
            )

    print(
        "\nInterpretation: exploratory results only. The held-out rows "
        "are unlabelled, so alert rates are not confirmed false-positive "
        "rates. Synthetic probes do not establish production accuracy."
    )


if __name__ == "__main__":
    main()
