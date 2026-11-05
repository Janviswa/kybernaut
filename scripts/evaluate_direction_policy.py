"""Experimental direction-aware policy for MAD deviations."""

import csv
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import FEATURE_NAMES

BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
THRESHOLD = 5.0


def main():
    with BASELINE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    timestamps = sorted({r["timestamp"] for r in rows})
    split = int(len(timestamps) * 0.70)
    train_times = set(timestamps[:split])

    train = [r for r in rows if r["timestamp"] in train_times]
    test = [r for r in rows if r["timestamp"] not in train_times]

    X_train = np.array([
        [float(r[name]) for name in FEATURE_NAMES] for r in train
    ])

    median = np.median(X_train, axis=0)
    mad = np.median(np.abs(X_train - median), axis=0)
    scale = 1.4826 * mad
    std = np.std(X_train, axis=0)
    scale = np.where(scale > 1e-12, scale, np.maximum(std, 1e-12))

    high_risk_metrics = {
        "p95_latency_seconds",
        "memory_bytes",
        "cpu_cores_used",
    }

    actionable = []
    informational = []

    for row in test:
        values = np.array([float(row[name]) for name in FEATURE_NAMES])
        scores = np.abs((values - median) / scale)

        for i, name in enumerate(FEATURE_NAMES):
            if scores[i] <= THRESHOLD:
                continue

            direction = "HIGH" if values[i] > median[i] else "LOW"
            event = (
                row["timestamp"],
                row["service"],
                name,
                direction,
                float(values[i]),
                float(scores[i]),
            )

            if direction == "HIGH" and name in high_risk_metrics:
                actionable.append(event)
            else:
                informational.append(event)

    print(f"Baseline: {BASELINE.name}")
    print(f"Threshold: {THRESHOLD}")
    print(f"Held-out rows: {len(test)}")
    print(f"Actionable candidate events: {len(actionable)}")
    print(f"Informational candidate events: {len(informational)}")

    for title, events in (
        ("Actionable candidates", actionable),
        ("Informational candidates", informational),
    ):
        print(f"\n{title}:")
        if not events:
            print("  None")
        for timestamp, service, metric, direction, value, score in events:
            print(
                f"  {timestamp} | {service} | {metric} | {direction} "
                f"| value={value:.8g} | deviation={score:.2f}"
            )

    print(
        "\nExploratory policy only: these labels are not confirmed incidents, "
        "and thresholds need validation against labelled operational events."
    )


if __name__ == "__main__":
    main()
