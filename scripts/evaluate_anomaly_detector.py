
"""Evaluate Kybernaut's Isolation Forest on real baseline telemetry.

The held-out baseline contains later timestamps than the training data.
The extreme probe is synthetic and is evaluated separately.
This script does not change production services or trigger remediation.
"""

import csv
import math
import sys
from pathlib import Path
from statistics import mean

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import (
    FEATURE_NAMES,
    TelemetryAnomalyDetector,
)

BASELINE_PATH = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
TRAIN_FRACTION = 0.70


def load_rows():
    with BASELINE_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    required = {"timestamp", "service", *FEATURE_NAMES}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("Baseline CSV is empty or missing required columns")

    for row in rows:
        for feature in FEATURE_NAMES:
            value = float(row[feature])
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Invalid {feature} value in baseline")

    return rows


def main():
    rows = load_rows()

    # Split chronologically to avoid training on future timestamps.
    timestamps = sorted({row["timestamp"] for row in rows})
    if len(timestamps) < 10:
        raise ValueError("Need at least 10 distinct timestamps")

    split_index = int(len(timestamps) * TRAIN_FRACTION)
    train_times = set(timestamps[:split_index])
    test_times = set(timestamps[split_index:])

    train_rows = [row for row in rows if row["timestamp"] in train_times]
    test_rows = [row for row in rows if row["timestamp"] in test_times]

    train_x = [
        [float(row[name]) for name in FEATURE_NAMES]
        for row in train_rows
    ]
    test_x = [
        [float(row[name]) for name in FEATURE_NAMES]
        for row in test_rows
    ]

    print(f"Baseline: {BASELINE_PATH}")
    print(f"Total rows: {len(rows)}")
    print(f"Training rows: {len(train_x)}")
    print(f"Held-out rows: {len(test_x)}")
    print(f"Training timestamps: {len(train_times)}")
    print(f"Held-out timestamps: {len(test_times)}")

    # Constant features cannot teach the model how latency varies.
    print("\nTraining feature variation:")
    for index, name in enumerate(FEATURE_NAMES):
        values = [sample[index] for sample in train_x]
        print(
            f"  {name}: unique={len(set(values))}, "
            f"min={min(values):.6g}, max={max(values):.6g}"
        )

    detector = TelemetryAnomalyDetector()
    detector.fit(train_x)

    held_out = detector.score(test_x)
    anomaly_count = sum(item["anomaly"] for item in held_out)

    print("\nHeld-out baseline results:")
    print(f"  Scored rows: {len(held_out)}")
    print(f"  Flagged rows: {anomaly_count}")
    print(f"  Flag rate: {anomaly_count / len(held_out):.1%}")
    print(
        "  Mean anomaly score: "
        f"{mean(item['anomaly_score'] for item in held_out):.6f}"
    )

    # Synthetic extreme probe; not real telemetry or a labelled incident.
    probe = [[100.0, 8.0, 2_000_000_000.0, 4.0]]
    probe_result = detector.score(probe)[0]

    print("\nSynthetic extreme probe (not real telemetry):")
    print(f"  Flagged: {probe_result['anomaly']}")
    print(f"  Anomaly score: {probe_result['anomaly_score']:.6f}")

    print(
        "\nInterpretation: these are exploratory results, not proof of "
        "production detection accuracy. The held-out baseline has no "
        "labelled incidents. The v6 baseline includes variation in all "
        "four telemetry features, and the synthetic extreme probe was "
        "detected. Real incident detection still requires labelled "
        "incidents and broader anomaly testing."
    )


if __name__ == "__main__":
    main()
