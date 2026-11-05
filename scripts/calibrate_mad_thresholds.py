"""Explore MAD thresholds on training telemetry only."""

import csv
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import FEATURE_NAMES

BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"


def main():
    with BASELINE.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    timestamps = sorted({r["timestamp"] for r in rows})
    split = int(len(timestamps) * 0.70)
    train_times = set(timestamps[:split])
    train = [r for r in rows if r["timestamp"] in train_times]

    X = np.array([
        [float(r[name]) for name in FEATURE_NAMES]
        for r in train
    ])

    median = np.median(X, axis=0)
    mad = np.median(np.abs(X - median), axis=0)
    scale = 1.4826 * mad
    std = np.std(X, axis=0)
    scale = np.where(scale > 1e-12, scale, np.maximum(std, 1e-12))

    scores = np.abs((X - median) / scale)

    print(f"Training rows: {len(X)}")
    print("Exploratory training-data thresholds; not validated incident thresholds.")

    for threshold in (3, 4, 5):
        print(f"\nThreshold: {threshold}")
        total = 0
        for i, name in enumerate(FEATURE_NAMES):
            count = int((scores[:, i] > threshold).sum())
            total += count
            print(
                f"  {name}: {count}/{len(X)} "
                f"({count / len(X):.1%})"
            )
        combined = int((scores > threshold).any(axis=1).sum())
        print(
            f"  Rows with any feature above threshold: "
            f"{combined}/{len(X)} ({combined / len(X):.1%})"
        )


if __name__ == "__main__":
    main()
