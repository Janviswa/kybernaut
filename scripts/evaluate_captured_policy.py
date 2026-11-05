"""Compare one captured latency observation against each service policy.

The originating service is unknown. Results are exploratory comparisons,
not attribution of the incident to any service.
"""
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import FEATURE_NAMES
from app.services.anomaly_policy import DirectionAwareAnomalyPolicy

BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
THRESHOLD = 5.0
INCIDENT = {
    "request_rate": 0.10169491525423728,
    "p95_latency_seconds": 0.975,
    "memory_bytes": 50958336.0,
    "cpu_cores_used": 0.005389830508474576,
}

def main():
    with BASELINE.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    timestamps = sorted({row["timestamp"] for row in rows})
    split = int(len(timestamps) * 0.70)
    train_times = set(timestamps[:split])

    print("Captured incident service: UNKNOWN")
    print(f"Threshold: {THRESHOLD}")
    print("Comparisons below are exploratory, not service attribution.")

    for service in (
        "order-service", "payment-service",
        "user-service", "inventory-service",
    ):
        training = [
            [float(row[name]) for name in FEATURE_NAMES]
            for row in rows
            if row["service"] == service
            and row["timestamp"] in train_times
        ]
        policy = DirectionAwareAnomalyPolicy(threshold=THRESHOLD)
        policy.fit(training)
        result = policy.evaluate(
            [[INCIDENT[name] for name in FEATURE_NAMES]]
        )[0]

        print(f"\n{service}")
        for name in ("p95_latency_seconds", "memory_bytes", "cpu_cores_used"):
            detail = result["features"][name]
            print(
                f"  {name}: direction={detail['direction']}, "
                f"score={detail['deviation_score']:.2f}, "
                f"classification={detail['classification']}"
            )
        print("  Actionable candidates:", result["actionable_candidates"])

    print("\nThe observation was not added to the baseline.")

if __name__ == "__main__":
    main()
