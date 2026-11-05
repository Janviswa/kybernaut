"""Read-only per-service evaluation of the direction-aware MAD policy.

Uses a chronological split of the existing baseline. Synthetic probes are
reported separately and are not treated as real incidents.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import FEATURE_NAMES
from app.services.anomaly_policy import DirectionAwareAnomalyPolicy
from app.services.anomaly_runtime import (
    BaselineIntegrityError,
    read_validated_baseline,
)
from scripts.evaluate_anomaly_benchmark import (
    build_reproducibility_metadata,
)

BASELINE = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
TRAIN_FRACTION = 0.70
THRESHOLD = 5.0
EXPECTED_SERVICES = (
    "order-service",
    "payment-service",
    "user-service",
    "inventory-service",
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--baseline", default=str(BASELINE))
    group.add_argument(
        "--exploratory-baseline",
        metavar="CSV",
        help="Evaluate a structurally valid, non-approved baseline explicitly.",
    )
    args = parser.parse_args(argv)
    exploratory = args.exploratory_baseline is not None
    baseline_path = args.exploratory_baseline or args.baseline
    try:
        rows, baseline_sha256 = read_validated_baseline(
            baseline_path, exploratory=exploratory
        )
    except BaselineIntegrityError as exc:
        parser.error(f"Baseline integrity validation failed ({exc}).")

    required = {"timestamp", "service", *FEATURE_NAMES}
    if not rows or not required.issubset(rows[0]):
        raise ValueError("Baseline is empty or missing required columns")

    timestamps = sorted({row["timestamp"] for row in rows})
    if len(timestamps) < 10:
        raise ValueError("Not enough timestamps for a chronological split")

    split_index = int(len(timestamps) * TRAIN_FRACTION)
    train_times = set(timestamps[:split_index])
    test_times = set(timestamps[split_index:])

    by_service = defaultdict(list)
    for row in rows:
        values = [float(row[name]) for name in FEATURE_NAMES]
        by_service[row["service"]].append((row, values))

    print(f"Baseline: {Path(baseline_path).name}")
    print(f"Threshold: {THRESHOLD}")
    print(
        "Reproducibility metadata: "
        + json.dumps(build_reproducibility_metadata(
            baseline_path=baseline_path,
            baseline_sha256=baseline_sha256,
            exploratory=exploratory,
            evaluator_path=__file__,
            configuration={
                "training_fraction": TRAIN_FRACTION,
                "threshold": THRESHOLD,
                "feature_names": list(FEATURE_NAMES),
            },
            random_seeds={},
            label_provenance={
                "baseline_holdout": (
                    "assumed_normal_not_independently_reviewed"
                ),
                "synthetic_probes": "synthetic_by_construction",
                "independently_reviewed_labels_present": False,
            },
        ), sort_keys=True)
    )
    print(f"Training timestamps: {len(train_times)}")
    print(f"Held-out timestamps: {len(test_times)}")

    policies = {}
    test_rows_by_service = {}

    print("\nPer-service holdout evaluation")
    for service in EXPECTED_SERVICES:
        service_rows = by_service.get(service, [])
        train = [
            values for row, values in service_rows
            if row["timestamp"] in train_times
        ]
        test_rows = [
            (row, values) for row, values in service_rows
            if row["timestamp"] in test_times
        ]

        if len(train) < 20 or not test_rows:
            print(f"\n{service}: insufficient train/holdout samples")
            continue

        policy = DirectionAwareAnomalyPolicy(threshold=THRESHOLD)
        policy.fit(train)
        policies[service] = policy
        test_rows_by_service[service] = test_rows

        results = policy.evaluate([values for _, values in test_rows])
        actionable = sum(bool(item["actionable_candidates"]) for item in results)
        informational = sum(bool(item["informational_deviations"]) for item in results)

        print(f"\n{service}")
        print(f"  Training rows: {len(train)}")
        print(f"  Holdout rows: {len(test_rows)}")
        print(f"  Rows with actionable candidates: {actionable}/{len(results)} "
              f"({actionable / len(results):.1%})")
        print(f"  Rows with informational deviations: {informational}/{len(results)} "
              f"({informational / len(results):.1%})")

        for feature in FEATURE_NAMES:
            count = sum(
                item["features"][feature]["classification"]
                == "actionable_candidate"
                for item in results
            )
            if count:
                print(f"  Actionable {feature}: {count}/{len(results)}")

    print("\nSynthetic high-memory probes (not real incidents)")
    memory_index = FEATURE_NAMES.index("memory_bytes")
    for service, policy in policies.items():
        train = [
            values for row, values in by_service[service]
            if row["timestamp"] in train_times
        ]
        center = [
            sorted(values[i] for values in train)[len(train) // 2]
            for i in range(len(FEATURE_NAMES))
        ]

        print(f"\n{service}")
        for multiplier in (1.05, 1.10, 1.25, 2.0):
            probe = center.copy()
            probe[memory_index] *= multiplier
            result = policy.evaluate([probe])[0]
            detail = result["features"]["memory_bytes"]
            print(
                f"  memory x{multiplier:.2f}: "
                f"score={detail['deviation_score']:.2f}, "
                f"classification={detail['classification']}"
            )

    print(
        "\nCaution: baseline holdout rows are unlabeled normal telemetry, "
        "and synthetic probes are not confirmed incidents. These results "
        "measure exploratory behavior, not production accuracy."
    )


if __name__ == "__main__":
    main()
