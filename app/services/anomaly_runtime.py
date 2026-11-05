"""Read-only runtime integration for direction-aware telemetry anomalies."""

import csv
import hashlib
import io
import logging
import math
from datetime import datetime
from pathlib import Path

import httpx

from app.services.anomaly_policy import DirectionAwareAnomalyPolicy
from app.services.anomaly_detector import FEATURE_NAMES
from app.services.prometheus_features import collect_service_features

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
# This identity is anchored in release-controlled code, not a mutable sidecar.
BASELINE_VERSION = "v6"
BASELINE_FILENAME = f"telemetry_baseline_{BASELINE_VERSION}.csv"
EXPECTED_BASELINE_SHA256 = (
    "777724eb6aae20da2104fef327dd691ab31a542be06f0c45fc5f66d8cfcac1a1"
)

EXPECTED_SERVICES = (
    "order-service",
    "payment-service",
    "user-service",
    "inventory-service",
)

EXPECTED_BASELINE_COLUMNS = (
    "timestamp",
    "service",
    *FEATURE_NAMES,
)
MINIMUM_BASELINE_ROWS_PER_SERVICE = 20
POLICY_THRESHOLD = 5.0

_policies: dict[str, DirectionAwareAnomalyPolicy] = {}
_initialization_error: str | None = None


class BaselineIntegrityError(ValueError):
    """A sanitized baseline validation failure."""


def read_validated_baseline(
    path: str | Path,
    *,
    exploratory: bool = False,
) -> tuple[list[dict[str, str]], str]:
    """Read the approved baseline or structurally validate an exploratory one."""
    baseline_path = Path(path)
    if not exploratory and baseline_path.name != BASELINE_FILENAME:
        raise BaselineIntegrityError("baseline_version_mismatch")
    try:
        raw = baseline_path.read_bytes()
    except OSError as exc:
        raise BaselineIntegrityError("baseline_read_failed") from exc

    digest = hashlib.sha256(raw).hexdigest()
    if not exploratory and digest != EXPECTED_BASELINE_SHA256:
        raise BaselineIntegrityError("baseline_digest_mismatch")

    try:
        text = raw.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if tuple(reader.fieldnames or ()) != EXPECTED_BASELINE_COLUMNS:
            raise BaselineIntegrityError("baseline_schema_mismatch")

        grouped_counts = dict.fromkeys(EXPECTED_SERVICES, 0)
        rows: list[dict[str, str]] = []
        seen_samples: set[tuple[str, datetime]] = set()
        previous_timestamp: datetime | None = None

        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise BaselineIntegrityError("baseline_row_shape_invalid")

            service = row["service"]
            if service not in grouped_counts:
                raise BaselineIntegrityError("baseline_unknown_service")

            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
            except ValueError as exc:
                raise BaselineIntegrityError(
                    "baseline_timestamp_invalid"
                ) from exc
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise BaselineIntegrityError(
                    "baseline_timestamp_timezone_missing"
                )
            if (
                previous_timestamp is not None
                and timestamp < previous_timestamp
            ):
                raise BaselineIntegrityError(
                    "baseline_timestamps_unordered"
                )
            previous_timestamp = timestamp

            sample_key = (service, timestamp)
            if sample_key in seen_samples:
                raise BaselineIntegrityError(
                    "baseline_duplicate_service_timestamp"
                )
            seen_samples.add(sample_key)

            for feature in FEATURE_NAMES:
                try:
                    value = float(row[feature])
                except ValueError as exc:
                    raise BaselineIntegrityError(
                        "baseline_feature_invalid"
                    ) from exc
                if not math.isfinite(value):
                    raise BaselineIntegrityError(
                        "baseline_feature_non_finite"
                    )
                if value < 0:
                    raise BaselineIntegrityError(
                        "baseline_feature_negative"
                    )

            grouped_counts[service] += 1
            rows.append(row)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise BaselineIntegrityError("baseline_csv_invalid") from exc

    for service, count in grouped_counts.items():
        if count < MINIMUM_BASELINE_ROWS_PER_SERVICE:
            raise BaselineIntegrityError(
                "baseline_service_coverage_insufficient"
            )

    return rows, digest


def _load_baseline_rows() -> dict[str, list[list[float]]]:
    rows, _ = read_validated_baseline(BASELINE_PATH)
    grouped: dict[str, list[list[float]]] = {
        service: [] for service in EXPECTED_SERVICES
    }
    for row in rows:
        grouped[row["service"]].append(
            [float(row[name]) for name in FEATURE_NAMES]
        )
    return grouped


def initialize_anomaly_policies() -> None:
    """Fit one read-only policy per service from its historical baseline."""
    global _policies, _initialization_error

    _policies = {}
    _initialization_error = None

    try:
        grouped = _load_baseline_rows()
        fitted = {}
        for service, rows in grouped.items():
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
    except BaselineIntegrityError as exc:
        _policies = {}
        _initialization_error = str(exc)
        logger.error(
            "Anomaly policies could not be initialized (%s)",
            _initialization_error,
        )
    except (TypeError, ValueError) as exc:
        _policies = {}
        _initialization_error = "baseline_policy_fit_failed"
        logger.error(
            "Anomaly policies could not be initialized (%s)",
            type(exc).__name__,
        )


async def evaluate_service_anomaly(service: str) -> dict:
    """Collect live features and evaluate them without side effects."""
    if service not in EXPECTED_SERVICES:
        raise ValueError("Unsupported service")

    policy = _policies.get(service)
    if policy is None:
        return {
            "status": "unavailable",
            "reason": _initialization_error or "policy_not_initialized",
            "collection_status": "not_applicable",
            "evaluation_status": "unavailable",
            "evaluation_reason": _initialization_error or "policy_not_initialized",
        }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            collected = await collect_service_features(client)
    except Exception as exc:
        logger.warning(
            "Anomaly evaluation unavailable for %s (%s)",
            service,
            type(exc).__name__,
        )
        return {
            "status": "unavailable",
            "reason": "telemetry_collection_failed",
            "collection_status": "failed",
            "evaluation_status": "not_applicable",
            "collection_reason": type(exc).__name__,
        }

    features = collected.get(service)
    if (
        not isinstance(features, dict)
        or any(name not in features for name in FEATURE_NAMES)
    ):
        return {
            "status": "unavailable",
            "reason": "incomplete_telemetry",
            "collection_status": "unavailable",
            "evaluation_status": "not_applicable",
            "collection_reason": "incomplete_telemetry",
        }

    try:
        observation = [[features[name] for name in FEATURE_NAMES]]
        result = policy.evaluate(observation)[0]
    except Exception as exc:
        logger.warning(
            "Anomaly evaluation unavailable for %s (%s)",
            service,
            type(exc).__name__,
        )
        return {
            "status": "unavailable",
            "reason": "telemetry_collection_failed",
            "collection_status": "success",
            "evaluation_status": "failed",
            "evaluation_reason": type(exc).__name__,
        }

    return {
        "status": "evaluated",
        "reason": None,
        "collection_status": "success",
        "evaluation_status": "success",
        "threshold": policy.threshold,
        "observed_at": features.get("observed_at"),
        "sample_timestamps": features.get("sample_timestamps"),
        **result,
    }
