"""Read-only reliability metrics computed from persisted incidents."""

from collections import Counter
from datetime import datetime

from app.services.incident_repository import list_incidents


ACTIVE_STATUSES = {"OPEN", "REMEDIATING"}


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace().astimezone()

    return parsed


def calculate_incident_metrics() -> dict:
    """Return incident counts, MTTR, and recurrence metrics."""
    incidents = list_incidents()

    status_counts = Counter(
        incident.get("status", "UNKNOWN") for incident in incidents
    )
    severity_counts = Counter(
        incident.get("severity", "UNKNOWN") for incident in incidents
    )
    service_counts = Counter(
        incident.get("service", "UNKNOWN") for incident in incidents
    )

    resolved_durations = []
    for incident in incidents:
        if incident.get("status") != "RESOLVED":
            continue

        opened = _parse_timestamp(incident.get("opened_at"))
        resolved = _parse_timestamp(incident.get("resolved_at"))

        if opened is None or resolved is None or resolved < opened:
            continue

        resolved_durations.append((resolved - opened).total_seconds())

    fingerprint_counts = Counter(
        incident["fingerprint"]
        for incident in incidents
        if incident.get("fingerprint")
    )
    recurring_fingerprints = {
        fingerprint: count
        for fingerprint, count in fingerprint_counts.items()
        if count > 1
    }

    total = len(incidents)
    recurring_occurrences = sum(
        count for count in recurring_fingerprints.values()
    )

    return {
        "total_incidents": total,
        "active_incidents": sum(
            status_counts.get(status, 0) for status in ACTIVE_STATUSES
        ),
        "by_status": dict(status_counts),
        "by_severity": dict(severity_counts),
        "by_service": dict(service_counts),
        "resolved_incidents_with_duration": len(resolved_durations),
        "mttr_seconds": (
            sum(resolved_durations) / len(resolved_durations)
            if resolved_durations else None
        ),
        "recurring_fingerprint_count": len(recurring_fingerprints),
        "recurring_occurrences": recurring_occurrences,
        "recurrence_rate": (
            recurring_occurrences / total if total else 0.0
        ),
    }
