"""Metric computation for the 20-incident benchmark (guide step 20).

Definitions (also in docs/EVALUATION.md):
  Detection TP : injected incident that raised an alert within the window.
  Detection FN : injected incident recorded with detection_result "missed".
  Detection FP : alert raised during a healthy control window.
  precision = TP/(TP+FP); recall = TP/(TP+FN); F1 = harmonic mean.
  MTTD  : mean(detected_at - injected_at) over detected cases (seconds).
  MTTR  : mean(recovered_at - injected_at) over cases with verified recovery.
  Classification accuracy / macro-F1: observed_category vs expected_category.
No result is reported unless all 20 cases are present.
"""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any

TYPES = ("cpu_spike", "memory_leak", "pod_crash", "disk_full",
         "deployment_failure")
CASES_PER_TYPE = 4
EXPECTED_CASES = len(TYPES) * CASES_PER_TYPE
EXPECTED_CONTROLS = 5
RESULT_SCHEMA_VERSION = 1
ENVELOPED_RESULT_SCHEMA_VERSION = 2
DEFAULT_SCENARIO_MATRIX = (
    Path(__file__).resolve().parents[2] / "benchmark" / "scenarios.json"
)


class BenchmarkIncomplete(ValueError):
    pass


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _load_scenario_matrix(
    path: str | Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Load and minimally validate the canonical scenario/control matrix."""
    matrix_path = Path(path) if path is not None else DEFAULT_SCENARIO_MATRIX
    try:
        matrix_bytes = matrix_path.read_bytes()
        matrix = json.loads(matrix_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkIncomplete(
            f"Unable to load scenario matrix ({type(exc).__name__})."
        ) from exc
    if not isinstance(matrix, dict):
        raise BenchmarkIncomplete("Scenario matrix must contain an object.")

    scenarios = matrix.get("cases")
    if not isinstance(scenarios, list) or len(scenarios) != EXPECTED_CASES:
        raise BenchmarkIncomplete(
            f"Scenario matrix must declare {EXPECTED_CASES} cases.")
    expected_categories: dict[str, str] = {}
    for item in scenarios:
        if not isinstance(item, dict):
            raise BenchmarkIncomplete(
                "Every declared scenario must be an object.")
        scenario_id = item.get("scenario_id")
        category = item.get("expected_category")
        if (
            not isinstance(scenario_id, str) or not scenario_id
            or not isinstance(category, str) or category not in TYPES
            or not isinstance(item.get("scenario"), str)
            or not item["scenario"]
            or not isinstance(item.get("variant"), str)
            or not isinstance(item.get("expected_outcome"), str)
            or not item["expected_outcome"]
            or scenario_id in expected_categories
        ):
            raise BenchmarkIncomplete(
                "Scenario matrix has an invalid or duplicate scenario mapping.")
        expected_target_id = item.get("expected_target_id")
        if expected_target_id is not None and (
            not isinstance(expected_target_id, str)
            or not expected_target_id.strip()
        ):
            raise BenchmarkIncomplete(
                f"Scenario {scenario_id!r} has an invalid "
                "expected_target_id.")
        expected_categories[scenario_id] = category
    if set(expected_categories.values()) != set(TYPES) or any(
        sum(1 for value in expected_categories.values() if value == kind)
        != CASES_PER_TYPE
        for kind in TYPES
    ):
        raise BenchmarkIncomplete(
            "Scenario matrix must declare four cases per expected category.")

    controls = matrix.get("controls")
    recommended = matrix.get("controls_recommended")
    if (
        not isinstance(controls, list)
        or isinstance(recommended, bool)
        or not isinstance(recommended, int)
        or recommended != EXPECTED_CONTROLS
        or len(controls) != EXPECTED_CONTROLS
    ):
        raise BenchmarkIncomplete(
            "Scenario matrix has an invalid declared control set.")
    expected_control_ids: set[str] = set()
    for control in controls:
        if not isinstance(control, dict):
            raise BenchmarkIncomplete(
                "Every declared control must be an object.")
        control_id = control.get("control_id")
        if (
            not isinstance(control_id, str) or not control_id
            or control_id in expected_control_ids
            or control.get("expected_alert") is not False
            or control.get("expected_category") != "unknown"
            or not isinstance(control.get("condition"), str)
            or not control["condition"]
            or not _is_finite_number(control.get("duration_minutes"))
            or control["duration_minutes"] <= 0
        ):
            raise BenchmarkIncomplete(
                "Scenario matrix has an invalid or duplicate control.")
        expected_control_ids.add(control_id)
    return matrix, hashlib.sha256(matrix_bytes).hexdigest()


def load_scenario_matrix(
    path: str | Path | None = None,
) -> dict[str, Any]:
    return _load_scenario_matrix(path)[0]


def _t(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed.tzinfo and offset is not None else None


def _timestamp(case: dict, field: str, *, required: bool) -> datetime | None:
    value = case.get(field)
    case_id = case.get("scenario_id")
    if value is None and not required:
        return None
    if not isinstance(value, str):
        requirement = "a string" if required else "a string or null"
        raise BenchmarkIncomplete(
            f"Scenario {case_id!r} needs {field} to be {requirement}.")
    parsed = _t(value)
    if parsed is None:
        raise BenchmarkIncomplete(
            f"Scenario {case_id!r} needs a valid timezone-aware "
            f"{field} timestamp.")
    return parsed


def _validate_run_envelope(
    run: object,
    cases: list[dict],
    controls: list[dict],
    matrix_sha256: str,
) -> dict[str, Any]:
    if not isinstance(run, dict):
        raise BenchmarkIncomplete(
            "Schema version 2 requires a run envelope object.")

    required = {
        "run_id", "evaluation_mode", "scenario_matrix_sha256",
        "started_at", "completed_at",
    }
    missing = required - run.keys()
    if missing:
        raise BenchmarkIncomplete(
            f"Run envelope is missing {', '.join(sorted(missing))}.")

    run_id = run["run_id"]
    if not isinstance(run_id, str) or not run_id.strip():
        raise BenchmarkIncomplete("Run envelope run_id must be non-empty.")
    mode = run["evaluation_mode"]
    if not isinstance(mode, str) or mode not in {"synthetic", "live_lab"}:
        raise BenchmarkIncomplete(
            "Run envelope evaluation_mode must be synthetic or live_lab.")
    declared_digest = run["scenario_matrix_sha256"]
    if (
        not isinstance(declared_digest, str)
        or len(declared_digest) != 64
        or any(char not in "0123456789abcdef" for char in declared_digest)
    ):
        raise BenchmarkIncomplete(
            "Run envelope scenario_matrix_sha256 must be a lowercase "
            "SHA-256 hex digest.")
    if declared_digest != matrix_sha256:
        raise BenchmarkIncomplete(
            "Run envelope scenario_matrix_sha256 does not match the loaded "
            "scenario matrix bytes.")

    started_at = _t(run["started_at"])
    if started_at is None:
        raise BenchmarkIncomplete(
            "Run envelope started_at must be a timezone-aware timestamp.")
    completed_at = _t(run["completed_at"])
    if completed_at is None:
        raise BenchmarkIncomplete(
            "Run envelope completed_at must be a timezone-aware timestamp.")
    if completed_at < started_at:
        raise BenchmarkIncomplete(
            "Run envelope completed_at must not precede started_at.")

    def require_within_run(value: datetime | None, field: str, owner: str):
        if value is not None and not started_at <= value <= completed_at:
            raise BenchmarkIncomplete(
                f"{owner} {field} must fall within the run timestamps.")

    for case in cases:
        scenario_id = case["scenario_id"]
        if "run_id" in case and case["run_id"] != run_id:
            raise BenchmarkIncomplete(
                f"Scenario {scenario_id!r} run_id does not match its "
                "enclosing run.")
        for field in ("injected_at", "detected_at", "recovered_at"):
            value = _timestamp(case, field, required=field == "injected_at")
            require_within_run(value, field, f"Scenario {scenario_id!r}")
        policy = case.get("policy_approval")
        if isinstance(policy, dict):
            for field in (
                "decision_at", "approval_decided_at", "execution_started_at",
            ):
                value = _timestamp(policy, field, required=field == "decision_at")
                require_within_run(
                    value, field, f"Scenario {scenario_id!r} policy evidence"
                )
        for entry in case.get("evidence_provenance", []):
            if not isinstance(entry, dict):
                continue
            for field in (
                "query_evaluation_timestamp", "query_timestamp",
                "observation_time",
            ):
                if field in entry and entry[field] is not None:
                    value = _t(entry[field])
                    require_within_run(
                        value, field, f"Scenario {scenario_id!r} provenance"
                    )

    for control in controls:
        control_id = control["control_id"]
        if "run_id" in control and control["run_id"] != run_id:
            raise BenchmarkIncomplete(
                f"Control {control_id!r} run_id does not match its "
                "enclosing run.")
        for field in ("started_at", "ended_at"):
            value = _timestamp(control, field, required=True)
            require_within_run(value, field, f"Control {control_id!r}")
        for entry in control.get("evidence_provenance", []):
            if not isinstance(entry, dict):
                continue
            for field in (
                "query_evaluation_timestamp", "query_timestamp",
                "observation_time",
            ):
                if field in entry and entry[field] is not None:
                    value = _t(entry[field])
                    require_within_run(
                        value, field, f"Control {control_id!r} provenance"
                    )

    return {
        "status": "enveloped",
        "run_id": run_id,
        "evaluation_mode": mode,
        "scenario_matrix_sha256": declared_digest,
        "matrix_digest_matched": True,
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "evaluation_mode_is_declared_only": True,
        "execution_independently_verified": False,
    }


def _duration(start: datetime, end: datetime, *, case_id: object,
              end_field: str) -> float:
    seconds = (end - start).total_seconds()
    if seconds < 0:
        raise BenchmarkIncomplete(
            f"Scenario {case_id!r} has {end_field} before injected_at.")
    return seconds


def _optional_number(case: dict, field: str) -> None:
    value = case.get(field)
    if value is None:
        return
    if (
        not _is_finite_number(value)
        or value < 0
    ):
        raise BenchmarkIncomplete(
            f"Scenario {case.get('scenario_id')!r} has invalid {field}; "
            "expected a finite nonnegative number or null.")


def _optional_monotonic_ns(case: dict, field: str) -> None:
    value = case.get(field)
    if value is not None and (type(value) is not int or value < 0):
        raise BenchmarkIncomplete(
            f"Scenario {case.get('scenario_id')!r} has invalid {field}; "
            "expected a nonnegative integer or null.")


def _optional_target_id(case: dict) -> None:
    if "observed_target_id" not in case or case["observed_target_id"] is None:
        return
    value = case["observed_target_id"]
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkIncomplete(
            f"Scenario {case.get('scenario_id')!r} has invalid "
            "observed_target_id; expected a non-empty string or null.")


def _validate_evidence_provenance(record: dict, record_id: str) -> None:
    if "evidence_provenance" not in record:
        return
    provenance = record["evidence_provenance"]
    if not isinstance(provenance, list):
        raise BenchmarkIncomplete(
            f"Record {record_id!r} evidence_provenance must be a list.")

    timestamp_fields = (
        "source_sample_timestamp",
        "query_evaluation_timestamp",
        "observation_time",
        "source_observed_at",
        "query_timestamp",
    )
    for entry in provenance:
        if not isinstance(entry, dict):
            raise BenchmarkIncomplete(
                f"Record {record_id!r} has a malformed evidence provenance "
                "entry.")

        for field in timestamp_fields:
            if field not in entry or entry[field] is None:
                continue
            if not isinstance(entry[field], str) or _t(entry[field]) is None:
                raise BenchmarkIncomplete(
                    f"Record {record_id!r} has an invalid evidence "
                    f"provenance timestamp {field}.")

        for field in (
            "source_identity", "source", "status", "timestamp_provenance",
        ):
            if field not in entry:
                continue
            value = entry[field]
            valid = (
                isinstance(value, str)
                and bool(value.strip())
                and (
                    field != "status"
                    or value in {"available", "unavailable", "stale", "invalid"}
                )
            )
            if not valid:
                raise BenchmarkIncomplete(
                    f"Record {record_id!r} has an invalid evidence "
                    f"provenance field {field}.")

        if "evidence_ref" in entry and (
            not isinstance(entry["evidence_ref"], str)
            or not entry["evidence_ref"].strip()
        ):
            raise BenchmarkIncomplete(
                f"Record {record_id!r} has an invalid evidence_ref.")

        new_shape = any(field in entry for field in (
            "source_identity", "source_sample_timestamp",
            "query_evaluation_timestamp", "observation_time",
        ))
        if new_shape:
            if not {"source_identity", "status", "observation_time"} <= (
                entry.keys()
            ):
                continue
            if entry["observation_time"] is None:
                raise BenchmarkIncomplete(
                    f"Record {record_id!r} evidence observation_time "
                    "must be a timezone-aware timestamp.")
            if not {"source_sample_timestamp", "query_evaluation_timestamp"} <= (
                entry.keys()
            ):
                continue
        else:
            # F.13 used caller-attributed source_observed_at metadata.
            required = {
                "source", "status", "source_observed_at",
                "timestamp_provenance",
            }
            if not required <= entry.keys():
                continue
            if entry["source_observed_at"] is None:
                raise BenchmarkIncomplete(
                    f"Record {record_id!r} source_observed_at must be a "
                    "timezone-aware timestamp.")


def _validate_policy_approval(case: dict) -> None:
    if "policy_approval" not in case:
        return
    evidence = case["policy_approval"]
    scenario_id = case.get("scenario_id")
    if not isinstance(evidence, dict):
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} policy_approval must be an object.")

    required = {
        "policy_decision", "approval_required", "approval_outcome",
        "execution_status", "verification_result", "decision_at",
    }
    missing = required - evidence.keys()
    if missing:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} policy_approval is missing "
            f"{', '.join(sorted(missing))}.")

    decisions = {"allow", "deny", "require_approval"}
    approvals = {"not_required", "pending", "approved", "denied", "expired"}
    execution_states = {"not_executed", "executed", "failed"}
    verification_states = {
        "verified", "not_verified", "not_run", "not_applicable",
    }
    decision = evidence["policy_decision"]
    approval_required = evidence["approval_required"]
    approval = evidence["approval_outcome"]
    execution = evidence["execution_status"]
    verification = evidence["verification_result"]
    if not isinstance(decision, str) or decision not in decisions:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} has an invalid policy_decision.")
    if not isinstance(approval_required, bool):
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} approval_required must be boolean.")
    if not isinstance(approval, str) or approval not in approvals:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} has an invalid approval_outcome.")
    if not isinstance(execution, str) or execution not in execution_states:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} has an invalid execution_status.")
    if not isinstance(verification, str) or verification not in (
        verification_states
    ):
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} has an invalid verification_result.")

    if (decision == "require_approval") != approval_required:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} policy_decision contradicts "
            "approval_required.")
    if approval_required == (approval == "not_required"):
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} approval_outcome contradicts "
            "approval_required.")
    if verification != case.get("verification_outcome"):
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} policy verification_result "
            "contradicts verification_outcome.")

    case_execution = case.get("remediation_outcome")
    case_result = case.get("remediation_result")
    execution_matches = (
        (execution == "not_executed" and case_execution != "executed")
        or (execution == "executed" and
            case_execution == "executed" and case_result == "success")
        or (execution == "failed" and
            case_execution == "executed" and case_result == "failed")
    )
    if not execution_matches:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} execution_status contradicts "
            "remediation outcome.")
    if decision == "deny" and execution != "not_executed":
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} denied policy cannot execute.")
    if approval_required and approval != "approved" and (
        execution != "not_executed"
    ):
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} execution requires approved "
            "approval_outcome.")

    decision_at = _timestamp(evidence, "decision_at", required=True)
    approval_at = _timestamp(
        evidence, "approval_decided_at", required=False
    )
    execution_at = _timestamp(evidence, "execution_started_at", required=False)
    terminal_approval = approval in {"approved", "denied", "expired"}
    if terminal_approval and approval_at is None:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} requires approval_decided_at for a "
            "terminal approval outcome.")
    if not terminal_approval and approval_at is not None:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} approval_decided_at requires a "
            "terminal approval outcome.")
    if approval_at is not None and approval_at < decision_at:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} approval_decided_at precedes "
            "decision_at.")
    if execution in {"executed", "failed"}:
        if execution_at is None:
            raise BenchmarkIncomplete(
                f"Scenario {scenario_id!r} requires execution_started_at.")
        if execution_at < decision_at:
            raise BenchmarkIncomplete(
                f"Scenario {scenario_id!r} execution_started_at precedes "
                "decision_at.")
        if approval_required and (
            approval_at is None or approval_at > execution_at
        ):
            raise BenchmarkIncomplete(
                f"Scenario {scenario_id!r} execution must follow the "
                "approval decision.")
    elif execution_at is not None:
        raise BenchmarkIncomplete(
            f"Scenario {scenario_id!r} execution_started_at requires an "
            "execution attempt.")

    if "audit_event_refs" in evidence:
        refs = evidence["audit_event_refs"]
        if not isinstance(refs, list) or any(
            not isinstance(ref, str) or not ref.strip() for ref in refs
        ):
            raise BenchmarkIncomplete(
                f"Scenario {scenario_id!r} audit_event_refs must be a "
                "list of non-empty strings.")


def _f1(p: float, r: float) -> float:
    return 0.0 if p + r == 0 else 2 * p * r / (p + r)


def _category_metrics(
    cases: list[dict], controls: list[dict]
) -> dict[str, Any]:
    categories = (*TYPES, "unknown")
    rows = [
        (
            case["expected_category"],
            case.get("observed_category"),
        )
        for case in cases
    ] + [
        ("unknown", control["observed_category"])
        for control in controls
    ]
    confusion = {
        actual: {
            predicted: sum(
                1 for truth, result in rows
                if truth == actual and (
                    result is None if predicted == "unclassified"
                    else result == predicted
                )
            )
            for predicted in (*categories, "unclassified")
        }
        for actual in categories
    }
    per_category = {}
    for category in categories:
        tp = sum(
            1 for truth, result in rows
            if truth == category and result == category
        )
        fp = sum(
            1 for truth, result in rows
            if truth != category and result == category
        )
        fn = sum(
            1 for truth, result in rows
            if truth == category and result != category
        )
        support = sum(1 for truth, _ in rows if truth == category)
        predicted = sum(1 for _, result in rows if result == category)
        precision = tp / predicted if predicted else None
        recall = tp / support if support else None
        f1_denominator = 2 * tp + fp + fn
        per_category[category] = {
            "support": support,
            "predicted_count": predicted,
            "true_positives": tp,
            "false_positives": fp,
            "false_negatives": fn,
            "precision_denominator": predicted,
            "recall_denominator": support,
            "f1_numerator": 2 * tp,
            "f1_denominator": f1_denominator,
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": (
                round(2 * tp / f1_denominator, 4)
                if f1_denominator else None
            ),
        }
    correct = sum(1 for truth, result in rows if truth == result)
    unclassified = sum(1 for _, result in rows if result is None)
    incorrect = len(rows) - correct - unclassified
    return {
        "sample_count": len(rows),
        "correct_count": correct,
        "incorrect_count": incorrect,
        "unclassified_count": unclassified,
        "accuracy_numerator": correct,
        "accuracy_denominator": len(rows),
        "accuracy": round(correct / len(rows), 4) if rows else None,
        "confusion_matrix": confusion,
        "per_category": per_category,
        "macro_f1_including_unknown": round(
            mean(item["f1"] for item in per_category.values()
                 if item["f1"] is not None),
            4,
        ) if any(item["f1"] is not None for item in per_category.values())
        else None,
        "macro_f1_denominator": sum(
            1 for item in per_category.values() if item["f1"] is not None
        ),
        "missed_detection_count": sum(
            1 for case in cases if case["detection_result"] == "missed"
        ),
        "classification_is_independent_of_detection": True,
    }


def _target_correctness(
    cases: list[dict], matrix: dict[str, Any]
) -> dict[str, Any]:
    expected_ids = {
        item["scenario_id"]: item.get("expected_target_id")
        for item in matrix["cases"]
    }
    correct = 0
    comparable = 0
    exclusions: dict[str, int] = {}
    for case in cases:
        scenario_id = case["scenario_id"]
        expected = expected_ids[scenario_id]
        observed = case.get("observed_target_id")
        reason = None
        if expected is None:
            reason = "canonical_expected_target_not_defined"
        elif not isinstance(expected, str) or not expected:
            reason = "canonical_expected_target_invalid"
        elif not isinstance(observed, str) or not observed.strip():
            reason = "observed_target_id_missing_or_invalid"
        else:
            comparable += 1
            correct += int(observed == expected)
        if reason is not None:
            exclusions[reason] = exclusions.get(reason, 0) + 1
    return {
        "status": "measured" if comparable else "not_measured",
        "numerator": correct,
        "denominator": comparable,
        "sample_count": comparable,
        "excluded_count": len(cases) - comparable,
        "exclusions_by_reason": exclusions,
        "accuracy": round(correct / comparable, 4) if comparable else None,
    }


def _provenance_metrics(records: list[dict]) -> dict[str, Any]:
    valid_entries = 0
    invalid_entries = 0
    incomplete_entries = 0
    records_with_valid_entry = 0
    records_without_valid_entry = 0
    records_missing_field = 0
    records_with_invalid_field = 0
    legacy_entries = 0
    source_sample_timestamp_count = 0
    query_timestamp_count = 0
    observation_time_count = 0
    evidence_reference_count = 0
    source_status_counts = {
        status: 0 for status in ("available", "unavailable", "stale", "invalid")
    }

    for record in records:
        if "evidence_provenance" not in record:
            records_missing_field += 1
            records_without_valid_entry += 1
            continue
        provenance = record["evidence_provenance"]
        if not isinstance(provenance, list):
            records_with_invalid_field += 1
            records_without_valid_entry += 1
            continue
        record_valid = False
        for entry in provenance:
            new_shape = any(field in entry for field in (
                "source_identity", "source_sample_timestamp",
                "query_evaluation_timestamp", "observation_time",
            ))
            if new_shape:
                source = entry.get("source_identity")
                sample_timestamp = entry.get("source_sample_timestamp")
                query_timestamp = entry.get("query_evaluation_timestamp")
                observed_at = entry.get("observation_time")
                required = {
                    "source_identity", "status", "source_sample_timestamp",
                    "query_evaluation_timestamp", "observation_time",
                }
            else:
                legacy_entries += 1
                source = entry.get("source")
                sample_timestamp = None
                query_timestamp = entry.get("query_timestamp")
                observed_at = entry.get("source_observed_at")
                required = {
                    "source", "status", "source_observed_at",
                    "timestamp_provenance",
                }
            status = entry.get("status")
            if not required <= entry.keys():
                incomplete_entries += 1
                continue
            valid_entries += 1
            record_valid = True
            source_status_counts[status] += 1
            source_sample_timestamp_count += int(sample_timestamp is not None)
            query_timestamp_count += int(query_timestamp is not None)
            observation_time_count += int(observed_at is not None)
            evidence_reference_count += int(
                entry.get("evidence_ref") is not None
            )
        if record_valid:
            records_with_valid_entry += 1
        else:
            records_without_valid_entry += 1

    return {
        "record_count": len(records),
        "records_with_valid_provenance": records_with_valid_entry,
        "records_without_valid_provenance": records_without_valid_entry,
        "records_missing_provenance_field": records_missing_field,
        "records_with_malformed_provenance_field": records_with_invalid_field,
        "valid_entry_count": valid_entries,
        "incomplete_entry_count": incomplete_entries,
        "invalid_entry_count": invalid_entries,
        "legacy_entry_count": legacy_entries,
        "source_sample_timestamp_count": source_sample_timestamp_count,
        "query_evaluation_timestamp_count": query_timestamp_count,
        "observation_time_count": observation_time_count,
        "evidence_reference_count": evidence_reference_count,
        "reported_source_status_counts": source_status_counts,
        "freshness_assessed_entry_count": 0,
        "freshness_unassessed_entry_count": valid_entries,
        "freshness_unassessed_record_count": len(records),
        "freshness_limitation": (
            "A parseable source timestamp and declared provenance do not "
            "establish freshness. No freshness threshold or underlying "
            "scrape-age evidence is defined by this result schema."
        ),
    }


def _policy_approval_metrics(cases: list[dict]) -> dict[str, Any]:
    records = [
        case for case in cases if "policy_approval" in case
    ]
    decisions: dict[str, int] = {}
    approval_outcomes: dict[str, int] = {}
    execution_statuses: dict[str, int] = {}
    verification_results: dict[str, int] = {}
    audit_references = 0
    for case in records:
        evidence = case["policy_approval"]
        for counts, key in (
            (decisions, "policy_decision"),
            (approval_outcomes, "approval_outcome"),
            (execution_statuses, "execution_status"),
            (verification_results, "verification_result"),
        ):
            value = evidence[key]
            counts[value] = counts.get(value, 0) + 1
        audit_references += int(bool(evidence.get("audit_event_refs")))

    return {
        "status": "not_measured",
        "coverage_status": "schema_validated" if records else "no_records",
        "sample_count": len(records),
        "excluded_count": len(cases) - len(records),
        "policy_decision_counts": decisions,
        "approval_outcome_counts": approval_outcomes,
        "execution_status_counts": execution_statuses,
        "verification_result_counts": verification_results,
        "records_with_audit_event_refs": audit_references,
        "records_without_audit_event_refs": len(records) - audit_references,
        "safety_claim": None,
        "limitation": (
            "This reports schema-consistent, caller-recorded states only; "
            "it does not independently prove policy enforcement, approval "
            "ordering, execution, verification, or audit persistence."
        ),
    }


def validate_matrix(
    cases: list[dict], scenario_matrix_path: str | Path | None = None
) -> None:
    matrix = load_scenario_matrix(scenario_matrix_path)
    _validate_cases(cases, matrix)


def _validate_cases(
    cases: list[dict],
    matrix: dict[str, Any],
    *,
    require_live_timing: bool = False,
) -> None:
    if not isinstance(cases, list):
        raise BenchmarkIncomplete("Benchmark cases must be a list.")
    if len(cases) != EXPECTED_CASES:
        raise BenchmarkIncomplete(
            f"Benchmark requires {EXPECTED_CASES} cases; got {len(cases)}.")
    if any(not isinstance(case, dict) for case in cases):
        raise BenchmarkIncomplete("Every benchmark case must be an object.")
    expected_categories = {
        item["scenario_id"]: item["expected_category"]
        for item in matrix["cases"]
    }
    ids = [c.get("scenario_id") for c in cases]
    if (
        any(not isinstance(case_id, str) or not case_id for case_id in ids)
        or len(set(ids)) != len(ids)
        or set(ids) != set(expected_categories)
    ):
        raise BenchmarkIncomplete(
            "scenario_id values must match the declared 20-case matrix.")
    trial_ids: list[str] = []
    for case in cases:
        scenario_id = case["scenario_id"]
        if case.get("expected_category") != expected_categories[scenario_id]:
            raise BenchmarkIncomplete(
                f"Scenario {scenario_id!r} has an incorrect expected_category.")
    for case in cases:
        detection_result = case.get("detection_result")
        if not isinstance(detection_result, str) or detection_result not in {
            "detected", "missed", "not_measured"
        }:
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} must record "
                "detection_result as detected, missed, or not_measured.")
        injected_at = _timestamp(case, "injected_at", required=True)
        detected_at = _timestamp(case, "detected_at", required=False)
        if (case["detection_result"] == "detected") != (
                detected_at is not None):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} must record "
                "a timezone-aware detected_at exactly when detection_result "
                "is detected.")
        if detected_at is not None and not require_live_timing:
            _duration(
                injected_at, detected_at,
                case_id=case.get("scenario_id"), end_field="detected_at",
            )
        _optional_monotonic_ns(case, "injected_monotonic_ns")
        _optional_monotonic_ns(case, "detected_monotonic_ns")
        injected_monotonic = case.get("injected_monotonic_ns")
        detected_monotonic = case.get("detected_monotonic_ns")
        if (injected_monotonic is None) != (detected_monotonic is None):
            if detected_at is not None or detected_monotonic is not None:
                raise BenchmarkIncomplete(
                    f"Scenario {scenario_id!r} must record both monotonic "
                    "timestamps for a detected event.")
        if (
            injected_monotonic is not None
            and detected_monotonic is not None
            and detected_monotonic < injected_monotonic
        ):
            raise BenchmarkIncomplete(
                f"Scenario {scenario_id!r} has detected_monotonic_ns "
                "before injected_monotonic_ns.")
        if require_live_timing:
            trial_id = case.get("trial_id")
            injection_status = case.get("injection_status")
            if not isinstance(trial_id, str) or not trial_id.strip():
                raise BenchmarkIncomplete(
                    f"Live scenario {scenario_id!r} needs a non-empty trial_id.")
            trial_ids.append(trial_id)
            if not isinstance(injection_status, str) or injection_status not in {
                "succeeded", "failed"
            }:
                raise BenchmarkIncomplete(
                    f"Live scenario {scenario_id!r} needs injection_status "
                    "succeeded or failed.")
            if injection_status == "succeeded":
                if (
                    detection_result not in {"detected", "missed"}
                    or injected_monotonic is None
                ):
                    raise BenchmarkIncomplete(
                        f"Successfully injected live scenario {scenario_id!r} "
                        "needs a monotonic injection timestamp and a measured "
                        "detection outcome.")
                if detection_result == "detected" and (
                    detected_monotonic is None
                    or detected_at is None
                ):
                    raise BenchmarkIncomplete(
                        f"Detected live scenario {scenario_id!r} needs UTC "
                        "and monotonic detection timestamps.")
                if detection_result == "missed" and (
                    detected_monotonic is not None
                    or detected_at is not None
                ):
                    raise BenchmarkIncomplete(
                        f"Missed live scenario {scenario_id!r} cannot have a "
                        "detection timestamp.")
            elif (
                detection_result != "not_measured"
                or detected_at is not None
                or injected_monotonic is not None
                or detected_monotonic is not None
            ):
                raise BenchmarkIncomplete(
                    f"Failed injection {scenario_id!r} cannot count as a "
                    "detection observation.")
            if injection_status == "failed" and not case.get("failure_reason"):
                raise BenchmarkIncomplete(
                    f"Failed injection {scenario_id!r} needs failure_reason.")
        observed = case.get("observed_category")
        if observed is not None and (
            not isinstance(observed, str)
            or observed not in {*TYPES, "unknown"}
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has an unsupported "
                "observed_category.")
        _optional_target_id(case)
        _validate_evidence_provenance(case, scenario_id)
        _validate_policy_approval(case)
        if not isinstance(case.get("evidence"), list) or any(
                not isinstance(item, str) for item in case["evidence"]):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} must record evidence "
                "as a list of strings.")
        if "failure_reason" not in case:
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} must record "
                "failure_reason (or null).")
        if case.get("failure_reason") is not None and not isinstance(
                case["failure_reason"], str):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} failure_reason must "
                "be a string or null.")
        remediation_outcome = case.get("remediation_outcome")
        if not isinstance(remediation_outcome, str) or (
            remediation_outcome
            not in {"executed", "not_executed", "not_applicable"}
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has an invalid "
                "remediation_outcome.")
        remediation_result = case.get("remediation_result")
        if remediation_result is not None and (
            not isinstance(remediation_result, str)
            or remediation_result not in {"success", "failed"}
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has an invalid "
                "remediation_result.")
        if (case["remediation_outcome"] == "executed") != (
                case.get("remediation_result") is not None):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} must record "
                "remediation_result exactly when remediation_outcome is "
                "executed.")
        verification_outcome = case.get("verification_outcome")
        if not isinstance(verification_outcome, str) or (
            verification_outcome
            not in {"verified", "not_verified", "not_run", "not_applicable"}
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has an invalid "
                "verification_outcome.")
        recovered_at = _timestamp(case, "recovered_at", required=False)
        if ((case["verification_outcome"] == "verified")
                != (recovered_at is not None)):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} must record a "
                "timezone-aware recovered_at exactly when verification "
                "succeeds.")
        if recovered_at is not None:
            _duration(
                injected_at, recovered_at,
                case_id=case.get("scenario_id"), end_field="recovered_at",
            )
        if case["verification_outcome"] == "verified" and (
            case["remediation_outcome"] != "executed"
            or case.get("remediation_result") != "success"
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} cannot claim verified "
                "recovery without successful executed remediation.")
        if case["remediation_outcome"] != "executed" and (
            case["verification_outcome"] == "verified"
            or recovered_at is not None
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} cannot claim recovery "
                "without executed remediation.")
        if case["remediation_outcome"] == "not_executed" and (
            case["verification_outcome"] not in {"not_run", "not_applicable"}
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} with no executed "
                "remediation must use not_run or not_applicable verification.")
        for field in ("llm_latency_ms", "llm_tokens"):
            _optional_number(case, field)
        rca_json_valid = case.get("rca_json_valid")
        if rca_json_valid is not None and not isinstance(rca_json_valid, bool):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has invalid "
                "rca_json_valid; expected a boolean or null.")
    if require_live_timing and len(set(trial_ids)) != len(trial_ids):
        raise BenchmarkIncomplete(
            "Live scenario trial_id values must be unique.")


def compute_metrics(
    cases: list[dict],
    controls: list[dict] | None = None,
    *,
    scenario_matrix_path: str | Path | None = None,
    schema_version: object = None,
    run_envelope: object = None,
) -> dict[str, Any]:
    if schema_version is not None and (
        type(schema_version) is not int
        or schema_version not in {
            RESULT_SCHEMA_VERSION, ENVELOPED_RESULT_SCHEMA_VERSION
        }
    ):
        raise BenchmarkIncomplete(
            f"Unsupported result schema_version {schema_version!r}; "
            f"supported versions are {RESULT_SCHEMA_VERSION} and "
            f"{ENVELOPED_RESULT_SCHEMA_VERSION}.")
    if schema_version == ENVELOPED_RESULT_SCHEMA_VERSION and (
        run_envelope is None
    ):
        raise BenchmarkIncomplete(
            "Schema version 2 requires a run envelope object.")
    if schema_version != ENVELOPED_RESULT_SCHEMA_VERSION and (
        run_envelope is not None
    ):
        raise BenchmarkIncomplete(
            "A run envelope requires schema_version 2; refusing to treat it "
            "as a legacy result.")

    matrix, matrix_sha256 = _load_scenario_matrix(scenario_matrix_path)
    live_lab = (
        schema_version == ENVELOPED_RESULT_SCHEMA_VERSION
        and isinstance(run_envelope, dict)
        and run_envelope.get("evaluation_mode") == "live_lab"
    )
    _validate_cases(cases, matrix, require_live_timing=live_lab)
    if not isinstance(controls, list):
        raise BenchmarkIncomplete(
            "A complete benchmark requires all declared healthy controls.")
    expected_controls = {
        control["control_id"]: control for control in matrix["controls"]
    }
    control_ids = []
    for control in controls:
        if not isinstance(control, dict):
            raise BenchmarkIncomplete("Every healthy control must be an object.")
        control_id = control.get("control_id")
        if not isinstance(control_id, str) or control_id not in expected_controls:
            raise BenchmarkIncomplete("Every healthy control needs control_id.")
        control_ids.append(control_id)
        if control.get("alert_raised") is not None and not isinstance(
            control.get("alert_raised"), bool
        ):
            raise BenchmarkIncomplete(
                f"Control {control_id!r} must record boolean or null "
                "alert_raised.")
        if control.get("observed_category") != "unknown":
            raise BenchmarkIncomplete(
                f"Control {control_id!r} must record observed_category "
                "'unknown'.")
        started_at = _timestamp(control, "started_at", required=True)
        ended_at = _timestamp(control, "ended_at", required=True)
        if (ended_at - started_at).total_seconds() < 0:
            raise BenchmarkIncomplete(
                f"Control {control_id!r} has ended_at before started_at.")
        if not isinstance(control.get("evidence"), list) or any(
            not isinstance(item, str) for item in control["evidence"]
        ):
            raise BenchmarkIncomplete(
                f"Control {control_id!r} must record evidence as a list "
                "of strings.")
        _validate_evidence_provenance(control, control_id)
        if "failure_reason" not in control or (
            control["failure_reason"] is not None
            and not isinstance(control["failure_reason"], str)
        ):
            raise BenchmarkIncomplete(
                f"Control {control_id!r} must record failure_reason as a "
                "string or null.")
    if len(set(control_ids)) != len(control_ids):
        raise BenchmarkIncomplete("Duplicate control_id values.")
    if set(control_ids) != set(expected_controls):
        raise BenchmarkIncomplete(
            "Healthy control IDs must match the declared control matrix.")

    detection_cases = (
        [case for case in cases if case.get("injection_status") == "succeeded"]
        if live_lab else cases
    )
    failed_injection_cases = (
        [case for case in cases if case.get("injection_status") == "failed"]
        if live_lab else []
    )
    case_detection_states = {
        case["detection_result"] for case in detection_cases
    }
    controls_measured = all(
        control["alert_raised"] is not None for control in controls
    )
    controls_unmeasured = all(
        control["alert_raised"] is None for control in controls
    )
    detection_measured = (
        case_detection_states <= {"detected", "missed"}
        and (bool(detection_cases) if live_lab else True)
        and controls_measured
    )
    detection_unmeasured = (
        case_detection_states == {"not_measured"}
        and controls_unmeasured
    )
    valid_live_observations = (
        live_lab
        and all(
            case["detection_result"] == "not_measured"
            for case in failed_injection_cases
        )
        and controls_measured
    )
    if not (
        (detection_measured and (controls_measured or not live_lab))
        or detection_unmeasured
        or valid_live_observations
    ):
        raise BenchmarkIncomplete(
            "Detection outcomes and healthy-control alerts must be either "
            "fully measured or explicitly not_measured together.")

    if schema_version == ENVELOPED_RESULT_SCHEMA_VERSION:
        run_metadata = _validate_run_envelope(
            run_envelope, cases, controls, matrix_sha256
        )
    else:
        run_metadata = {
            "status": "legacy",
            "run_id": None,
            "evaluation_mode": None,
            "scenario_matrix_sha256": None,
            "matrix_digest_matched": False,
            "execution_independently_verified": False,
        }

    tp_count = sum(
        1 for c in detection_cases if c["detection_result"] == "detected"
    )
    fn_count = sum(
        1 for c in detection_cases if c["detection_result"] == "missed"
    )
    tp = tp_count if detection_measured else None
    fn = fn_count if detection_measured else None
    fp = (
        sum(1 for c in controls if c["alert_raised"])
        if controls_measured else None
    )
    precision = (
        tp_count / (tp_count + fp) if tp_count + fp else 0.0
    ) if detection_measured and fp is not None else None
    recall = (
        tp_count / (tp_count + fn_count)
    ) if detection_measured else None

    detected_cases = [
        case for case in detection_cases
        if case["detection_result"] == "detected"
    ]

    def detection_duration(case: dict) -> float:
        if (
            live_lab
            and case.get("injected_monotonic_ns") is not None
            and case.get("detected_monotonic_ns") is not None
        ):
            return (
                case["detected_monotonic_ns"]
                - case["injected_monotonic_ns"]
            ) / 1_000_000_000
        return _duration(
            _timestamp(case, "injected_at", required=True),
            _timestamp(case, "detected_at", required=True),
            case_id=case.get("scenario_id"),
            end_field="detected_at",
        )

    mttd = [detection_duration(case) for case in detected_cases]
    mttr = [
        _duration(
            _timestamp(c, "injected_at", required=True),
            _timestamp(c, "recovered_at", required=True),
            case_id=c.get("scenario_id"),
            end_field="recovered_at",
        )
        for c in cases if c.get("verification_outcome") == "verified"
    ]

    labelled = [c for c in cases if c.get("observed_category") is not None]
    correct = sum(1 for c in labelled
                  if c["observed_category"] == c["expected_category"])
    per_class_f1 = {}
    for t in TYPES:
        tpc = sum(1 for c in cases if c["expected_category"] == t
                  and c.get("observed_category") == t)
        fpc = sum(1 for c in cases if c["expected_category"] != t
                  and c.get("observed_category") == t)
        fnc = sum(1 for c in cases if c["expected_category"] == t
                  and c.get("observed_category") != t)
        p = tpc / (tpc + fpc) if tpc + fpc else 0.0
        r = tpc / (tpc + fnc) if tpc + fnc else 0.0
        per_class_f1[t] = round(_f1(p, r), 4)

    remediated = [
        c for c in cases if c.get("remediation_outcome") == "executed"
    ]
    json_checked = [c for c in cases if c.get("rca_json_valid") is not None]
    lat = [c["llm_latency_ms"] for c in cases
           if c.get("llm_latency_ms") is not None]
    tok = [c["llm_tokens"] for c in cases
           if c.get("llm_tokens") is not None]

    def rate(num: int, den: int) -> float | None:
        return round(num / den, 4) if den else None

    def rate_details(num: int, den: int, *, missing: int,
                     excluded: int = 0) -> dict[str, int | float | None]:
        return {
            "rate": rate(num, den),
            "numerator": num,
            "denominator": den,
            "sample_count": den,
            "missing_count": missing,
            "excluded_count": excluded,
        }

    def mean_details(
        values: list[float], *, eligible: int, excluded: int = 0
    ) -> dict:
        ordered = sorted(values)

        def percentile(fraction: float) -> float | None:
            if not ordered:
                return None
            position = (len(ordered) - 1) * fraction
            lower = math.floor(position)
            upper = math.ceil(position)
            value = (
                ordered[lower]
                + (ordered[upper] - ordered[lower]) * (position - lower)
            )
            return round(value, 6)

        return {
            "mean": round(mean(values), 6) if values else None,
            "median": percentile(0.5),
            "p95": percentile(0.95),
            "minimum": round(min(values), 6) if values else None,
            "maximum": round(max(values), 6) if values else None,
            "numerator": round(sum(values), 6),
            "denominator": len(values),
            "sample_count": len(values),
            "missing_count": max(0, eligible - len(values) - excluded),
            "excluded_count": excluded,
        }

    executed_count = len(remediated)
    case_count = len(cases)
    scenario_mttd = {}
    for case in cases:
        scenario_id = case["scenario_id"]
        values = (
            [detection_duration(case)]
            if case in detected_cases else []
        )
        failed_injection = case.get("injection_status") == "failed"
        missed = case in detection_cases and case["detection_result"] == "missed"
        scenario_mttd[scenario_id] = {
            **mean_details(
                values,
                eligible=1 if case in detection_cases else 0,
                excluded=int(failed_injection or missed),
            ),
            "injection_status": (
                case.get("injection_status", "not_recorded")
                if live_lab else "not_applicable"
            ),
            "detection_result": case["detection_result"],
        }
    remediation_success_count = sum(
        1 for c in remediated if c.get("remediation_result") == "success"
    )
    verification_success_count = sum(
        1 for c in remediated if c.get("verification_outcome") == "verified"
    )
    json_valid_count = sum(1 for c in json_checked if c["rca_json_valid"])
    rca_missing_count = case_count - len(json_checked)
    all_evaluation_records = [*cases, *controls]

    return {
        "run_metadata": run_metadata,
        "cases": len(cases), "controls": len(controls),
        "detection": {
            "true_positives": tp, "false_negatives": fn,
            "false_positives": fp,
            "successful_injections": (
                len(detection_cases) if live_lab else None
            ),
            "failed_injections": (
                len(failed_injection_cases) if live_lab else None
            ),
            "precision": round(precision, 4) if precision is not None else None,
            "recall": round(recall, 4) if recall is not None else None,
            "f1": (
                round(_f1(precision, recall), 4)
                if precision is not None and recall is not None else None
            ),
            "status": "measured" if detection_measured else "not_measured",
            "sample_count": len(detection_cases) if detection_measured else 0,
            "excluded_count": (
                len(failed_injection_cases)
                if detection_measured else len(cases)
            ),
            "caveat": ("precision is only meaningful with healthy controls"
                       if not controls else None),
        },
        "mttd_seconds": round(mean(mttd), 2) if mttd else None,
        "mttr_seconds": round(mean(mttr), 2) if mttr else None,
        "timing": {
            "mttd": mean_details(
                mttd,
                eligible=len(detection_cases),
                excluded=(
                    fn_count + len(failed_injection_cases)
                    if live_lab else sum(
                        1 for c in cases
                        if c["detection_result"] == "missed"
                    )
                ),
            ),
            "mttd_by_scenario": scenario_mttd,
            "mttd_duration_source": (
                "monotonic_clock"
                if live_lab else "UTC_timestamp_delta"
            ),
            "percentile_method": (
                "linear_interpolation_at_(n-1)*p"
            ),
            "mttd_missed_scenarios": [
                case["scenario_id"] for case in detection_cases
                if case["detection_result"] == "missed"
            ],
            "mttd_failed_injections": [
                case["scenario_id"] for case in failed_injection_cases
            ],
            "verified_mttr": mean_details(
                mttr, eligible=case_count,
                excluded=case_count - len(mttr),
            ),
        },
        "classification": {
            "accuracy": rate(correct, len(cases)),
            "macro_f1": round(mean(per_class_f1.values()), 4),
            "per_class_f1": per_class_f1,
            "unlabelled_cases": len(cases) - len(labelled),
        },
        "classification_metrics": _category_metrics(cases, controls),
        "target_correctness": _target_correctness(cases, matrix),
        "evidence_provenance": _provenance_metrics(
            all_evaluation_records
        ),
        "policy_approval_safety": _policy_approval_metrics(cases),
        "rca": {
            "json_validity": rate(json_valid_count, len(json_checked)),
            "json_validity_counts": rate_details(
                json_valid_count, len(json_checked),
                missing=rca_missing_count,
            ),
        },
        "remediation_success": rate(
            remediation_success_count, executed_count),
        "remediation_success_counts": rate_details(
            remediation_success_count, executed_count,
            missing=0, excluded=case_count - executed_count,
        ),
        "verification_success": rate(
            verification_success_count, executed_count),
        "verification_success_counts": rate_details(
            verification_success_count, executed_count,
            missing=0, excluded=case_count - executed_count,
        ),
        "llm": {
            "mean_latency_ms": round(mean(lat), 1) if lat else None,
            "mean_tokens": round(mean(tok), 1) if tok else None,
            "latency_counts": mean_details(lat, eligible=case_count),
            "token_counts": mean_details(tok, eligible=case_count),
        },
    }
