"""Tests for direction-aware telemetry anomaly policies."""

import numpy as np
import pytest

from app.services.anomaly_detector import FEATURE_NAMES


def robust_stats(training_data):
    """Fit robust per-feature statistics on training data only."""
    X = np.asarray(training_data, dtype=float)
    median = np.median(X, axis=0)
    mad = np.median(np.abs(X - median), axis=0)
    scale = 1.4826 * mad
    std = np.std(X, axis=0)
    scale = np.where(scale > 1e-12, scale, np.maximum(std, 1e-12))
    return median, scale


def evaluate_policy(observation, median, scale, threshold=5):
    """Return deviations and direction-aware operational candidates."""
    values = np.asarray(observation, dtype=float)

    if values.shape != median.shape:
        raise ValueError("Observation feature count does not match baseline")
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Observation contains invalid telemetry")

    z_scores = np.abs((values - median) / scale)
    directions = np.where(values > median, "HIGH", "LOW")

    # High latency, memory and CPU are operational candidates.
    # Low latency and request-rate deviations are informational only.
    actionable_features = {
        "p95_latency_seconds",
        "memory_bytes",
        "cpu_cores_used",
    }

    actionable = {
        name: {
            "direction": directions[index],
            "z_score": float(z_scores[index]),
        }
        for index, name in enumerate(FEATURE_NAMES)
        if (
            name in actionable_features
            and directions[index] == "HIGH"
            and z_scores[index] > threshold
        )
    }

    return {
        "z_scores": dict(zip(FEATURE_NAMES, z_scores.tolist())),
        "directions": dict(zip(FEATURE_NAMES, directions.tolist())),
        "actionable": actionable,
    }


@pytest.fixture
def baseline_stats():
    # Deterministic synthetic baseline for policy logic tests.
    rng = np.random.default_rng(42)
    training = np.column_stack([
        rng.normal(0.2, 0.02, 200).clip(0.01),
        rng.normal(0.095, 0.01, 200).clip(0.01),
        rng.normal(49_000_000, 1_000_000, 200).clip(1),
        rng.normal(0.02, 0.005, 200).clip(0.001),
    ])
    return robust_stats(training)


def test_high_latency_is_actionable(baseline_stats):
    median, scale = baseline_stats
    observation = median.copy()
    latency = FEATURE_NAMES.index("p95_latency_seconds")
    observation[latency] = median[latency] + 10 * scale[latency]

    result = evaluate_policy(observation, median, scale)

    assert "p95_latency_seconds" in result["actionable"]
    assert result["actionable"]["p95_latency_seconds"]["direction"] == "HIGH"


@pytest.mark.parametrize("feature", ["memory_bytes", "cpu_cores_used"])
def test_high_resource_usage_is_actionable(baseline_stats, feature):
    median, scale = baseline_stats
    observation = median.copy()
    index = FEATURE_NAMES.index(feature)
    observation[index] = median[index] + 10 * scale[index]

    result = evaluate_policy(observation, median, scale)

    assert feature in result["actionable"]
    assert result["actionable"][feature]["direction"] == "HIGH"


def test_low_latency_is_not_actionable(baseline_stats):
    median, scale = baseline_stats
    observation = median.copy()
    latency = FEATURE_NAMES.index("p95_latency_seconds")
    observation[latency] = max(0, median[latency] - 10 * scale[latency])

    result = evaluate_policy(observation, median, scale)

    assert result["directions"]["p95_latency_seconds"] == "LOW"
    assert "p95_latency_seconds" not in result["actionable"]


def test_low_request_rate_is_not_actionable_by_itself(baseline_stats):
    median, scale = baseline_stats
    observation = median.copy()
    request_rate = FEATURE_NAMES.index("request_rate")
    observation[request_rate] = max(0, median[request_rate] - 10 * scale[request_rate])

    result = evaluate_policy(observation, median, scale)

    assert result["directions"]["request_rate"] == "LOW"
    assert result["actionable"] == {}


def test_normal_observation_has_no_actionable_alerts(baseline_stats):
    median, scale = baseline_stats

    result = evaluate_policy(median.copy(), median, scale)

    assert result["actionable"] == {}


@pytest.mark.parametrize(
    "bad_value",
    [float("nan"), float("inf"), -1.0],
)
def test_invalid_observation_is_rejected(baseline_stats, bad_value):
    median, scale = baseline_stats
    observation = median.copy()
    observation[0] = bad_value

    with pytest.raises(ValueError, match="invalid telemetry"):
        evaluate_policy(observation, median, scale)
