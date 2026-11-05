"""Test synthetic telemetry anomalies against the v6 baseline.

Synthetic probes are not real incidents. This script does not modify
the baseline CSV or trigger remediation.
"""

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import (
    FEATURE_NAMES,
    TelemetryAnomalyDetector,
)

BASELINE_PATH = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
TRAIN_FRACTION = 0.70


def main():
    with BASELINE_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    timestamps = sorted({row["timestamp"] for row in rows})
    split_index = int(len(timestamps) * TRAIN_FRACTION)
    train_times = set(timestamps[:split_index])

    training_rows = [
        row for row in rows if row["timestamp"] in train_times
    ]
    training_data = [
        [float(row[name]) for name in FEATURE_NAMES]
        for row in training_rows
    ]

    detector = TelemetryAnomalyDetector()
    detector.fit(training_data)

    # Use a typical training observation as the starting point.
    center = [
        sorted(sample[i] for sample in training_data)[len(training_data) // 2]
        for i in range(len(FEATURE_NAMES))
    ]

    probes = {
        "Traffic spike": [center[0] * 10, center[1], center[2], center[3]],
        "Latency spike": [center[0], center[1] * 10, center[2], center[3]],
        "Memory spike": [center[0], center[1], center[2] * 2, center[3]],
        "CPU spike": [center[0], center[1], center[2], center[3] * 10],
        "Combined extreme": [100.0, 8.0, 2_000_000_000.0, 4.0],
    }

    print(f"Baseline: {BASELINE_PATH.name}")
    print(f"Training observations: {len(training_data)}")
    print("\nSynthetic probe results (not real incidents):")

    for name, probe in probes.items():
        result = detector.score([probe])[0]
        print(
            f"{name:18} | flagged={str(result['anomaly']):5} "
            f"| score={result['anomaly_score']:.6f}"
        )

    print(
        "\nNote: these probes are exploratory. A spike may not be flagged "
        "when other features remain normal. Results do not establish "
        "production detection accuracy."
    )


if __name__ == "__main__":
    main()
