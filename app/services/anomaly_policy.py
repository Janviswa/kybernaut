"""Direction-aware, read-only anomaly policy for Kybernaut telemetry."""

from collections.abc import Sequence

import numpy as np

from app.services.anomaly_detector import FEATURE_NAMES


ACTIONABLE_HIGH_FEATURES = frozenset({
    "p95_latency_seconds",
    "memory_bytes",
    "cpu_cores_used",
})

OPERATIONAL_LATENCY_THRESHOLD_SECONDS = 1.0

def _validate_rows(
    rows: Sequence[Sequence[float]],
) -> np.ndarray:
    """Validate telemetry observations without changing their values."""
    try:
        values = np.asarray(rows, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("Telemetry must contain numeric values") from exc

    if values.ndim != 2 or values.shape[1] != len(FEATURE_NAMES):
        raise ValueError(
            f"Expected rows with {len(FEATURE_NAMES)} features: "
            f"{', '.join(FEATURE_NAMES)}"
        )

    if values.shape[0] < 1:
        raise ValueError("At least one telemetry row is required")

    if not np.isfinite(values).all():
        raise ValueError("Telemetry cannot contain NaN or infinite values")

    if (values < 0).any():
        raise ValueError("Telemetry features cannot be negative")

    return values


class DirectionAwareAnomalyPolicy:
    """Classify unusual telemetry as actionable candidates or informational."""

    def __init__(self, *, threshold: float = 5.0) -> None:
        if not np.isfinite(threshold) or threshold <= 0:
            raise ValueError("threshold must be a finite positive number")

        self.threshold = float(threshold)
        self._median: np.ndarray | None = None
        self._scale: np.ndarray | None = None

    def fit(self, baseline: Sequence[Sequence[float]]) -> None:
        """Estimate robust feature statistics from historical baseline data."""
        values = _validate_rows(baseline)

        self._median = np.median(values, axis=0)
        mad = np.median(
            np.abs(values - self._median),
            axis=0,
        )
        scale = 1.4826 * mad

        # Fallback when MAD is zero or near zero.
        std = np.std(values, axis=0)
        self._scale = np.where(
            scale > 1e-12,
            scale,
            np.maximum(std, 1e-12),
        )

    def evaluate(
        self,
        observations: Sequence[Sequence[float]],
    ) -> list[dict]:
        """Evaluate observations without triggering alerts or remediation."""
        if self._median is None or self._scale is None:
            raise RuntimeError("Fit the policy with a baseline first")

        values = _validate_rows(observations)
        deviations = np.abs(
            (values - self._median) / self._scale
        )

        results = []

        for row, scores in zip(values, deviations):
            features = {}

            for index, name in enumerate(FEATURE_NAMES):
                value = float(row[index])
                median = float(self._median[index])
                score = float(scores[index])
                direction = (
                    "HIGH" if value > median
                    else "LOW" if value < median
                    else "NORMAL"
                )

                if score > self.threshold:
                    if (
                        name in ACTIONABLE_HIGH_FEATURES
                        and direction == "HIGH"
                    ):
                        classification = "actionable_candidate"
                    else:
                        classification = "informational"
                else:
                    classification = "normal"

                features[name] = {
                    "value": value,
                    "baseline_median": median,
                    "direction": direction,
                    "deviation_score": score,
                    "classification": classification,
                }

            actionable = [
                name for name, detail in features.items()
                if detail["classification"] == "actionable_candidate"
            ]
            informational = [
                name for name, detail in features.items()
                if detail["classification"] == "informational"
            ]

            operational_findings = []

            latency = features["p95_latency_seconds"]
            if latency["value"] > OPERATIONAL_LATENCY_THRESHOLD_SECONDS:
                operational_findings.append({
                    "type": "high_latency",
                    "severity": "warning",
                    "value": latency["value"],
                    "threshold": OPERATIONAL_LATENCY_THRESHOLD_SECONDS,
                })

            results.append({
                "features": features,
                "actionable_candidates": actionable,
                "informational_deviations": informational,
                "operational_findings": operational_findings,
            })

        return results
