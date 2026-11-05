"""Evaluate captured lab latency incident against the v6 baseline."""

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

# Captured from the live lab collector during the temporary latency injection.
INCIDENT = {
    "request_rate": 0.10169491525423728,
    "p95_latency_seconds": 0.975,
    "memory_bytes": 50958336.0,
    "cpu_cores_used": 0.005389830508474576,
}


def robust_stats(X):
    median = np.median(X, axis=0)
    mad = np.median(np.abs(X - median), axis=0)
    scale = 1.4826 * mad
    std = np.std(X, axis=0)
    scale = np.where(scale > 1e-12, scale, np.maximum(std, 1e-12))
    return median, scale


def main():
    with BASELINE.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    timestamps = sorted({row["timestamp"] for row in rows})
    split = int(len(timestamps) * TRAIN_FRACTION)
    train_times = set(timestamps[:split])
    train = [row for row in rows if row["timestamp"] in train_times]

    X_train = np.array([
        [float(row[name]) for name in FEATURE_NAMES]
        for row in train
    ])

    incident = np.array([INCIDENT[name] for name in FEATURE_NAMES])

    if not np.isfinite(X_train).all() or not np.isfinite(incident).all():
        raise ValueError("Non-finite feature value")
    if (X_train < 0).any() or (incident < 0).any():
        raise ValueError("Negative feature value")

    detector = TelemetryAnomalyDetector()
    detector.fit(X_train.tolist())
    if_result = detector.score([incident.tolist()])[0]

    median, scale = robust_stats(X_train)
    z_scores = np.abs((incident - median) / scale)

    print(f"Baseline: {BASELINE.name}")
    print(f"Training rows: {len(X_train)}")
    print("\nCaptured lab incident (not added to baseline):")
    for name, value, center, z in zip(
        FEATURE_NAMES, incident, median, z_scores
    ):
        direction = "HIGH" if value > center else "LOW"
        print(
            f"  {name}: value={value:.8g}, "
            f"training_median={center:.8g}, "
            f"direction={direction}, MAD_z={z:.2f}"
        )

    print("\nDetection results:")
    print(
        f"  Isolation Forest: flagged={if_result['anomaly']}, "
        f"score={if_result['anomaly_score']:.6f}"
    )

    latency_index = FEATURE_NAMES.index("p95_latency_seconds")
    latency_is_high = incident[latency_index] > median[latency_index]

    for threshold in THRESHOLDS:
        mad_flag = bool((z_scores > threshold).any())
        hybrid_flag = bool(if_result["anomaly"] or mad_flag)
        actionable_latency = bool(
            z_scores[latency_index] > threshold and latency_is_high
        )
        print(
            f"  Threshold {threshold}: MAD={mad_flag}, "
            f"hybrid={hybrid_flag}, "
            f"high-latency candidate={actionable_latency}"
        )

    print(
        "\nCaveat: this is one captured lab incident, not a labelled "
        "benchmark. Detection here does not establish production accuracy."
    )


if __name__ == "__main__":
    main()
