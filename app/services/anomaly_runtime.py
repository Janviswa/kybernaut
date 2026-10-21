"""Read-only runtime integration for direction-aware telemetry anomalies."""

import csv
import logging
from pathlib import Path

import httpx

from app.services.anomaly_policy import DirectionAwareAnomalyPolicy
from app.services.anomaly_detector import FEATURE_NAMES
from app.services.prometheus_features import collect_service_features

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"

EXPECTED_SERVICES = (
    "order-service",
    "payment-service",
    "user-service",
    "inventory-service",
)

POLICY_THRESHOLD = 5.0

_policies: dict[str, DirectionAwareAnomalyPolicy] = {}
_initialization_error: str | None = None


def initialize_anomaly_policies() -> None:
    """Fit one read-only policy per service from its historical baseline."""
    global _policies, _initialization_error

    _policies = {}
    _initialization_error = None

    try:
        with BASELINE_PATH.open(
            newline="", encoding="utf-8"
        ) as file:
            reader = csv.DictReader(file)

            required = {"service", *FEATURE_NAMES}
            if not reader.fieldnames or not required.issubset(
                reader.fieldnames
            ):
                raise ValueError("Baseline CSV has missing columns")

            grouped: dict[str, list[list[float]]] = {
                service: [] for service in EXPECTED_SERVICES
            }

            for row in reader:
                service = row.get("service")
                if service not in grouped:
                    continue

                values = [float(row[name]) for name in FEATURE_NAMES]
                grouped[service].append(values)

        fitted = {}
        for service, rows in grouped.items():
            if len(rows) < 20:
                raise ValueError(
                    f"Insufficient baseline rows for {service}"
                )

            policy = DirectionAwareAnomalyPolicy(
                threshold=POLICY_THRESHOLD
            )
            policy.fit(rows)
            fitted[service] = policy

        _policies = fitted
        logger.info(
            "Initialized direction-aware policies for %d services",
            len(_policies),
        )

    except (OSError, ValueError, TypeError) as exc:
        _policies = {}
        _initialization_error = type(exc).__name__
        logger.exception("Anomaly policies could not be initialized")


async def evaluate_service_anomaly(service: str) -> dict:
    """Collect live features and evaluate them without side effects."""
    if service not in EXPECTED_SERVICES:
        raise ValueError("Unsupported service")

    policy = _policies.get(service)
    if policy is None:
        return {
            "status": "unavailable",
            "reason": _initialization_error or "policy_not_initialized",
        }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            collected = await collect_service_features(client)

        features = collected.get(service)
        if features is None:
            return {
                "status": "unavailable",
                "reason": "incomplete_telemetry",
            }

        observation = [[features[name] for name in FEATURE_NAMES]]
        result = policy.evaluate(observation)[0]

        return {
            "status": "evaluated",
            "threshold": policy.threshold,
            **result,
        }

    except Exception as exc:
        logger.warning(
            "Anomaly evaluation unavailable for %s (%s)",
            service,
            type(exc).__name__,
        )
        return {
            "status": "unavailable",
            "reason": "telemetry_collection_failed",
        }
