"""Read-only anomaly scoring for Kybernaut telemetry."""

from collections.abc import Sequence
from math import isfinite

import numpy as np
from sklearn.ensemble import IsolationForest


FEATURE_NAMES = (
    "request_rate",
    "p95_latency_seconds",
    "memory_bytes",
    "cpu_cores_used",
)


def _validate_rows(
    rows: Sequence[Sequence[float]],
    *,
    minimum_rows: int,
) -> np.ndarray:
    """Validate telemetry rows before passing them to the model."""
    try:
        values = np.asarray(rows, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("Telemetry must contain numeric values") from exc

    if values.ndim != 2 or values.shape[1] != len(FEATURE_NAMES):
        raise ValueError(
            f"Expected rows with {len(FEATURE_NAMES)} features: "
            f"{', '.join(FEATURE_NAMES)}"
        )

    if values.shape[0] < minimum_rows:
        raise ValueError(
            f"At least {minimum_rows} telemetry rows are required"
        )

    if not np.isfinite(values).all():
        raise ValueError("Telemetry cannot contain NaN or infinite values")

    if (values < 0).any():
        raise ValueError("Telemetry features cannot be negative")

    return values


class TelemetryAnomalyDetector:
    """Isolation Forest detector; scoring only, with no side effects."""

    def __init__(
        self,
        *,
        contamination: float = 0.05,
        random_state: int = 42,
    ) -> None:
        if not 0.0 < contamination <= 0.5:
            raise ValueError("contamination must be in (0, 0.5]")

        self.contamination = contamination
        self.random_state = random_state
        self._model: IsolationForest | None = None

    def fit(self, baseline: Sequence[Sequence[float]]) -> None:
        """Fit using historical baseline observations, not live-only data."""
        values = _validate_rows(baseline, minimum_rows=20)

        self._model = IsolationForest(
            n_estimators=200,
            contamination=self.contamination,
            random_state=self.random_state,
        )
        self._model.fit(values)

    def score(
        self,
        observations: Sequence[Sequence[float]],
    ) -> list[dict[str, float | bool]]:
        """Score observations against the fitted baseline.

        Lower anomaly_score values are more unusual.
        Anomaly flags are model outputs, not confirmed incidents.
        """
        if self._model is None:
            raise RuntimeError("Fit the detector with a baseline first")

        values = _validate_rows(observations, minimum_rows=1)

        predictions = self._model.predict(values)
        scores = self._model.decision_function(values)

        return [
            {
                "anomaly": bool(prediction == -1),
                "anomaly_score": float(score),
            }
            for prediction, score in zip(predictions, scores)
        ]
