import numpy as np
import pytest

from app.services.anomaly_detector import TelemetryAnomalyDetector


@pytest.fixture
def baseline():
    # Deterministic synthetic test data only; not real service telemetry.
    rng = np.random.default_rng(42)

    return np.column_stack(
        [
            rng.normal(0.2, 0.02, 100).clip(0),
            rng.normal(0.095, 0.01, 100).clip(0),
            rng.normal(49_000_000, 1_000_000, 100).clip(0),
            rng.normal(0.02, 0.005, 100).clip(0),
        ]
    ).tolist()


def test_detector_requires_baseline_before_scoring():
    detector = TelemetryAnomalyDetector()

    with pytest.raises(RuntimeError, match="baseline"):
        detector.score([[0.2, 0.095, 49_000_000, 0.02]])


def test_detector_rejects_too_small_baseline():
    detector = TelemetryAnomalyDetector()

    with pytest.raises(ValueError, match="At least 20"):
        detector.fit([[0.2, 0.095, 49_000_000, 0.02]] * 5)


def test_detector_rejects_invalid_values():
    detector = TelemetryAnomalyDetector()
    detector.fit(
        [[0.2, 0.095, 49_000_000, 0.02]] * 20
    )

    with pytest.raises(ValueError, match="NaN or infinite"):
        detector.score([[0.2, float("nan"), 49_000_000, 0.02]])


def test_detector_returns_scores_without_side_effects(baseline):
    detector = TelemetryAnomalyDetector()
    detector.fit(baseline)

    results = detector.score(
        [
            [0.2, 0.095, 49_000_000, 0.02],
            [100.0, 8.0, 2_000_000_000, 4.0],
        ]
    )

    assert len(results) == 2
    assert all(isinstance(row["anomaly"], bool) for row in results)
    assert all(np.isfinite(row["anomaly_score"]) for row in results)
    assert all(
        set(row) == {"anomaly", "anomaly_score"} for row in results
    )

def test_detector_rejects_wrong_feature_count(baseline):
    detector = TelemetryAnomalyDetector()
    detector.fit(baseline)

    with pytest.raises(ValueError, match="Expected rows"):
        detector.score([[0.2, 0.095, 49_000_000]])


def test_detector_rejects_negative_features(baseline):
    detector = TelemetryAnomalyDetector()
    detector.fit(baseline)

    with pytest.raises(ValueError, match="cannot be negative"):
        detector.score([[0.2, 0.095, -1.0, 0.02]])


@pytest.mark.parametrize("contamination", [0.0, -0.1, 0.51, 1.0])
def test_detector_rejects_invalid_contamination(contamination):
    with pytest.raises(ValueError, match="contamination"):
        TelemetryAnomalyDetector(contamination=contamination)


def test_detector_flags_extreme_observation(baseline):
    detector = TelemetryAnomalyDetector()
    detector.fit(baseline)

    result = detector.score(
        [[100.0, 8.0, 2_000_000_000, 4.0]]
    )

    assert result[0]["anomaly"] is True
