"""Inspect held-out rows that exceed the MAD threshold."""

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

    X = np.array([
        [float(r[name]) for name in FEATURE_NAMES] for r in train
    ])

    median = np.median(X, axis=0)
    mad = np.median(np.abs(X - median), axis=0)
    scale = 1.4826 * mad
    std = np.std(X, axis=0)
    scale = np.where(scale > 1e-12, scale, np.maximum(std, 1e-12))

    print(f"Threshold: {THRESHOLD}")
    found = 0

    for row in test:
        values = np.array([float(row[name]) for name in FEATURE_NAMES])
        scores = np.abs((values - median) / scale)
        exceeded = np.flatnonzero(scores > THRESHOLD)

        if len(exceeded):
            found += 1
            print(f"\nTimestamp: {row['timestamp']}")
            print(f"Service: {row['service']}")
            for i in exceeded:
                direction = "HIGH" if values[i] > median[i] else "LOW"
                print(
                    f"  {FEATURE_NAMES[i]}: {values[i]:.8g} "
                    f"({direction}; median={median[i]:.8g}, "
                    f"deviation score={scores[i]:.2f})"
                )

    print(f"\nFlagged held-out rows: {found}/{len(test)}")


if __name__ == "__main__":
    main()
