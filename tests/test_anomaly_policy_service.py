import numpy as np
import pytest

from app.services.anomaly_detector import FEATURE_NAMES
from app.services.anomaly_policy import DirectionAwareAnomalyPolicy


@pytest.fixture
def policy_and_center():
    rng = np.random.default_rng(42)
    baseline = np.column_stack([
        rng.normal(0.2, 0.02, 200).clip(0.01),
        rng.normal(0.095, 0.01, 200).clip(0.01),
        rng.normal(49_000_000, 1_000_000, 200).clip(1),
        rng.normal(0.02, 0.005, 200).clip(0.001),
    ])
    policy = DirectionAwareAnomalyPolicy(threshold=5)
    policy.fit(baseline.tolist())
    return policy, np.median(baseline, axis=0)


def test_high_latency_is_actionable_candidate(policy_and_center):
    policy, center = policy_and_center
    observation = center.copy()
    index = FEATURE_NAMES.index("p95_latency_seconds")
    observation[index] += 10

    result = policy.evaluate([observation.tolist()])[0]

    assert "p95_latency_seconds" in result["actionable_candidates"]


@pytest.mark.parametrize("feature", ["memory_bytes", "cpu_cores_used"])
def test_high_resource_usage_is_actionable_candidate(
    policy_and_center, feature
):
    policy, center = policy_and_center
    observation = center.copy()
    index = FEATURE_NAMES.index(feature)

    # Create a clearly elevated value relative to the baseline.
    observation[index] = center[index] * 100

    result = policy.evaluate([observation.tolist()])[0]

    assert feature in result["actionable_candidates"]


def test_low_request_rate_is_not_actionable_by_itself(policy_and_center):
    policy, center = policy_and_center
    observation = center.copy()
    observation[FEATURE_NAMES.index("request_rate")] = 0.0

    result = policy.evaluate([observation.tolist()])[0]

    assert result["actionable_candidates"] == []


def test_normal_observation_has_no_candidates(policy_and_center):
    policy, center = policy_and_center

    result = policy.evaluate([center.tolist()])[0]

    assert result["actionable_candidates"] == []
    assert result["informational_deviations"] == []


def test_policy_requires_fit():
    policy = DirectionAwareAnomalyPolicy()

    with pytest.raises(RuntimeError, match="Fit the policy"):
        policy.evaluate([[0.2, 0.095, 49_000_000, 0.02]])


def test_invalid_observation_is_rejected(policy_and_center):
    policy, center = policy_and_center
    observation = center.copy()
    observation[0] = float("nan")

    with pytest.raises(ValueError, match="NaN or infinite"):
        policy.evaluate([observation.tolist()])

def test_small_latency_increase_is_not_operationally_actionable():
    baseline = [
        [0.5, 0.0024, 49_000_000, 0.0045]
        for _ in range(30)
    ]
    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(baseline)

    observation = [[0.5, 0.00252, 49_000_000, 0.0045]]
    result = policy.evaluate(observation)[0]

    # A 5% latency increase is below the existing 1-second
    # operational threshold and must not trigger escalation.
    assert result["operational_findings"] == []


def test_latency_above_operational_threshold_is_flagged():
    baseline = [
        [0.5, 0.0024, 49_000_000, 0.0045]
        for _ in range(30)
    ]
    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(baseline)

    observation = [[0.5, 1.2, 49_000_000, 0.0045]]
    result = policy.evaluate(observation)[0]

    assert len(result["operational_findings"]) == 1
    finding = result["operational_findings"][0]
    assert finding["type"] == "high_latency"
    assert finding["severity"] == "warning"
    assert finding["threshold"] == 1.0


def test_small_latency_increase_preserves_statistical_evidence():
    baseline = [
        [0.5, 0.0024, 49_000_000, 0.0045]
        for _ in range(30)
    ]
    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(baseline)

    observation = [[0.5, 0.00252, 49_000_000, 0.0045]]
    result = policy.evaluate(observation)[0]

    assert result["operational_findings"] == []
    assert result["features"]["p95_latency_seconds"]["direction"] == "HIGH"

def test_constant_baseline_uses_standard_deviation_fallback():
    baseline = [
        [0.5, 0.0024, 49_000_000, 0.0045]
        for _ in range(30)
    ]

    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(baseline)

    assert np.all(np.isfinite(policy._scale))
    assert np.all(policy._scale > 0)

    result = policy.evaluate(baseline[:1])[0]

    assert result["actionable_candidates"] == []
    assert result["informational_deviations"] == []


def test_near_constant_baseline_keeps_finite_positive_scale():
    baseline = [
        [
            0.5,
            0.0024 + (i % 3) * 1e-13,
            49_000_000,
            0.0045,
        ]
        for i in range(30)
    ]

    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(baseline)

    assert np.all(np.isfinite(policy._scale))
    assert np.all(policy._scale > 0)

    result = policy.evaluate([baseline[-1]])[0]

    assert result["features"]["p95_latency_seconds"][
        "deviation_score"
    ] >= 0
