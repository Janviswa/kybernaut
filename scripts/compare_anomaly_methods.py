"""Compare anomaly methods on v6 telemetry and synthetic probes.

Exploratory experiment only. Does not modify production code or CSV files.
"""

import csv
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import RobustScaler

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import FEATURE_NAMES, TelemetryAnomalyDetector

BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
TRAIN_FRACTION = 0.70


def load_training_data():
    with BASELINE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    timestamps = sorted({r["timestamp"] for r in rows})
    split = int(len(timestamps) * TRAIN_FRACTION)
    train_times = set(timestamps[:split])
    train = [r for r in rows if r["timestamp"] in train_times]

    X = np.array(
        [[float(r[name]) for name in FEATURE_NAMES] for r in train],
        dtype=float,
    )
    if len(X) < 20 or not np.isfinite(X).all():
        raise ValueError("Training data is insufficient or invalid")
    return X


def robust_mad_scores(X, probes):
    """Score each feature using median/MAD, with a std fallback."""
    median = np.median(X, axis=0)
    mad = np.median(np.abs(X - median), axis=0)
    scale = 1.4826 * mad

    # Avoid division by zero for constant features.
    std = np.std(X, axis=0)
    scale = np.where(scale > 1e-12, scale, np.maximum(std, 1e-12))

    z = np.abs((probes - median) / scale)
    return z.max(axis=1), z


def main():
    X = load_training_data()
    center = np.median(X, axis=0)

    probes = np.array([
        [center[0] * 10, center[1], center[2], center[3]],
        [center[0], center[1] * 10, center[2], center[3]],
        [center[0], center[1], center[2] * 2, center[3]],
        [center[0], center[1], center[2], center[3] * 10],
        [100.0, 8.0, 2_000_000_000.0, 4.0],
    ])

    names = [
        "Traffic spike",
        "Latency spike",
        "Memory spike",
        "CPU spike",
        "Combined extreme",
    ]

    current = TelemetryAnomalyDetector()
    current.fit(X.tolist())
    current_results = current.score(probes.tolist())

    scaled = make_pipeline(
        RobustScaler(),
        IsolationForest(
            n_estimators=200,
            contamination=0.05,
            random_state=42,
        ),
    )
    scaled.fit(X)
    scaled_flags = scaled.predict(probes) == -1
    scaled_scores = scaled.decision_function(probes)

    mad_scores, feature_z = robust_mad_scores(X, probes)

    print(f"Baseline: {BASELINE.name}")
    print(f"Training rows: {len(X)}")
    print("\nSynthetic probes — not real incidents")
    print(
        f"{'Probe':18} | {'Current':7} | {'Scaled IF':9} | "
        f"{'MAD max-z':9} | {'Largest deviation'}"
    )
    print("-" * 83)

    for i, name in enumerate(names):
        feature_idx = int(np.argmax(feature_z[i]))
        print(
            f"{name:18} | "
            f"{str(current_results[i]['anomaly']):7} | "
            f"{str(bool(scaled_flags[i])):9} | "
            f"{mad_scores[i]:9.2f} | "
            f"{FEATURE_NAMES[feature_idx]}"
        )
        print(
            f"{'':18}   scores: current="
            f"{current_results[i]['anomaly_score']:.6f}, "
            f"scaled={scaled_scores[i]:.6f}"
        )

    print("\nInterpretation:")
    print("- Current and scaled IF flags use each model's own decision boundary.")
    print("- MAD max-z is a deviation score, not a calibrated probability.")
    print("- Compare methods; do not select a production model from five probes.")
    print("- Validate normal false-positive rates and labelled incidents first.")


if __name__ == "__main__":
    main()
