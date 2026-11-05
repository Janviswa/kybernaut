"""Evaluate synthetic incident severities against baseline telemetry.

Exploratory evaluation only. Does not modify production code or baseline data.
Synthetic probes are not real incidents or validated severity labels.
"""

import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from app.services.anomaly_detector import (
    FEATURE_NAMES,
    TelemetryAnomalyDetector,
)
from app.services.anomaly_policy import DirectionAwareAnomalyPolicy

BASELINE_PATH = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
TRAIN_FRACTION = 0.70
THRESHOLD = 5.0

# Multipliers are experimental inputs, not validated incident severities.
PROBES = {
    "Traffic": ("request_rate", [1.25, 2.0, 5.0]),
    "Latency": ("p95_latency_seconds", [1.25, 2.0, 5.0]),
    "Memory": ("memory_bytes", [1.10, 1.25, 2.0]),
    "CPU": ("cpu_cores_used", [1.25, 2.0, 5.0]),
}


def load_training_data():
    with BASELINE_PATH.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    timestamps = sorted({row["timestamp"] for row in rows})
    split_index = int(len(timestamps) * TRAIN_FRACTION)
    training_timestamps = set(timestamps[:split_index])

    training_rows = [
        row for row in rows
        if row["timestamp"] in training_timestamps
    ]
    training_data = np.asarray(
        [
            [float(row[name]) for name in FEATURE_NAMES]
            for row in training_rows
        ],
        dtype=float,
    )

    if len(training_data) < 20 or not np.isfinite(training_data).all():
        raise ValueError("Training baseline is insufficient or invalid")

    return training_data


def main():
    training_data = load_training_data()

    statistical_policy = DirectionAwareAnomalyPolicy(
        threshold=THRESHOLD
    )
    statistical_policy.fit(training_data.tolist())

    isolation_forest = TelemetryAnomalyDetector()
    isolation_forest.fit(training_data.tolist())

    center = np.median(training_data, axis=0)

    print(f"Baseline: {BASELINE_PATH.name}")
    print(f"Training observations: {len(training_data)}")
    print(f"MAD threshold: {THRESHOLD}")
    print("Synthetic probes only; not real incident validation.\n")

    print(
        f"{'Incident':10} {'Multiplier':10} "
        f"{'MAD candidate':14} {'IF flagged':12} "
        f"{'Operational latency':20} {'MAD score':12}"
    )
    print("-" * 85)

    total = 0
    mad_detected = 0
    if_detected = 0
    operational_detected = 0

    for incident, (feature_name, multipliers) in PROBES.items():
        feature_index = FEATURE_NAMES.index(feature_name)

        for multiplier in multipliers:
            probe = center.copy()
            probe[feature_index] *= multiplier
            observation = [probe.tolist()]

            mad_result = statistical_policy.evaluate(observation)[0]
            if_result = isolation_forest.score(observation)[0]

            candidate = feature_name in mad_result["actionable_candidates"]
            if_flagged = if_result["anomaly"]
            operational = any(
                finding.get("type") == "high_latency"
                for finding in mad_result["operational_findings"]
            )
            score = mad_result["features"][feature_name]["deviation_score"]

            total += 1
            mad_detected += int(candidate)
            if_detected += int(if_flagged)
            operational_detected += int(operational)

            print(
                f"{incident:10} {multiplier:>8.2f}x "
                f"{str(candidate):14} {str(if_flagged):12} "
                f"{str(operational):20} {score:12.3f}"
            )

    print("\nSummary")
    print(f"MAD actionable candidates: {mad_detected}/{total}")
    print(f"Isolation Forest flags: {if_detected}/{total}")
    print(f"Operational latency findings: {operational_detected}/{total}")
    print(
        "\nThese are synthetic probe detection counts, not production "
        "precision, recall, or false-positive rates."
    )


if __name__ == "__main__":
    main()
