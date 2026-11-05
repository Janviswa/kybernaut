"""Validate semantic consistency in an offline LLM evaluation report."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


CALL_STATUSES = ("completed", "failed", "timed_out", "skipped", "not_attempted")
CLASSIFICATION_STATUSES = ("correct", "incorrect", "failed", "not_run")
CONTROL_STATUSES = ("synthetic_placeholder_not_observed", "observed")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: cannot read JSON ({exc})") from exc


def _count_map(values: list[Any], field: str, allowed: tuple[str, ...]) -> Counter[str]:
    return Counter(value.get(field) for value in values if isinstance(value, dict) and value.get(field) in allowed)


def _compare_counts(
    errors: list[str],
    label: str,
    actual: Any,
    expected: Counter[str],
    keys: tuple[str, ...],
    measured: bool,
) -> None:
    if not isinstance(actual, dict):
        errors.append(f"{label} counts must be an object")
        return
    for key in keys:
        value = actual.get(key)
        if measured:
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{label}.{key} must be an integer when the track was measured")
            elif value != expected[key]:
                errors.append(f"{label}.{key} is {value}; expected {expected[key]} from cases")
        elif value is not None:
            errors.append(f"{label}.{key} must be null when the track was not measured")


def validate_report(report: Any, scenario_matrix: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report root must be an object"]
    if not isinstance(scenario_matrix, dict):
        return ["scenario matrix root must be an object"]

    expected_cases = scenario_matrix.get("cases")
    expected_controls = scenario_matrix.get("controls")
    if not isinstance(expected_cases, list) or not isinstance(expected_controls, list):
        return ["scenario matrix must contain cases and controls arrays"]
    expected_case_map = {
        item.get("scenario_id"): item
        for item in expected_cases
        if isinstance(item, dict) and isinstance(item.get("scenario_id"), str)
    }
    expected_control_ids = {
        item.get("control_id")
        for item in expected_controls
        if isinstance(item, dict) and isinstance(item.get("control_id"), str)
    }

    cases = report.get("cases")
    if not isinstance(cases, list):
        errors.append("cases must be an array")
        cases = []
    case_ids = [
        item.get("scenario_id")
        for item in cases
        if isinstance(item, dict) and isinstance(item.get("scenario_id"), str)
    ]
    if len(case_ids) != len(set(case_ids)):
        errors.append("case scenario_id values must be unique")
    if set(case_ids) != set(expected_case_map) or len(case_ids) != len(expected_case_map):
        errors.append("case scenario_id values must exactly match the scenario matrix")
    for case in cases:
        if not isinstance(case, dict):
            errors.append("each case must be an object")
            continue
        scenario_id = case.get("scenario_id")
        expected = expected_case_map.get(scenario_id)
        if expected is not None and case.get("scenario_category_metadata") != expected.get("expected_category"):
            errors.append(f"{scenario_id}: scenario_category_metadata does not match the matrix")
        if case.get("call_status") in ("completed", "failed", "timed_out"):
            if not isinstance(case.get("retry_count"), int) or isinstance(case.get("retry_count"), bool):
                errors.append(f"{scenario_id}: attempted call requires an integer retry_count")
            if not isinstance(case.get("latency_ms"), (int, float)) or isinstance(case.get("latency_ms"), bool):
                errors.append(f"{scenario_id}: attempted call requires measured latency_ms")

    controls = report.get("healthy_controls")
    if not isinstance(controls, list):
        errors.append("healthy_controls must be an array")
        controls = []
    control_ids = [
        item.get("control_id")
        for item in controls
        if isinstance(item, dict) and isinstance(item.get("control_id"), str)
    ]
    if len(control_ids) != len(set(control_ids)):
        errors.append("control_id values must be unique")
    if set(control_ids) != expected_control_ids or len(control_ids) != len(expected_control_ids):
        errors.append("control_id values must exactly match the scenario matrix")
    for control in controls:
        if not isinstance(control, dict):
            errors.append("each healthy control must be an object")
            continue
        status = control.get("evidence_status")
        if status not in CONTROL_STATUSES:
            errors.append(f"{control.get('control_id')}: invalid evidence_status")
        elif status == "observed" and not isinstance(control.get("observed_window"), dict):
            errors.append(f"{control.get('control_id')}: observed control requires observed_window")
        elif status == "observed" and control.get("submitted_to_provider") is not False:
            errors.append(f"{control.get('control_id')}: observed control must not be submitted to provider")
        elif status == "synthetic_placeholder_not_observed":
            if control.get("observed_window") is not None:
                errors.append(f"{control.get('control_id')}: placeholder must not have observed_window")
            if control.get("submitted_to_provider") is not False:
                errors.append(f"{control.get('control_id')}: placeholder must not be submitted to provider")

    attempted = [
        case
        for case in cases
        if isinstance(case, dict) and case.get("call_status") in ("completed", "failed", "timed_out")
    ]
    approval = report.get("approval")
    provider = report.get("provider")
    if attempted:
        if not isinstance(approval, dict) or approval.get("status") != "approved":
            errors.append("attempted provider calls require approved owner approval metadata")
        elif (
            not approval.get("approval_reference")
            or not approval.get("owner_reference")
            or not approval.get("approved_at_utc")
            or approval.get("synthetic_evidence_only") is not True
        ):
            errors.append("attempted provider calls require complete synthetic-only approval metadata")
        if not isinstance(provider, dict) or not provider.get("provider_id") or not provider.get("exact_model_identifier"):
            errors.append("attempted provider calls require provider and exact model identifiers")

    for case in cases:
        if not isinstance(case, dict):
            continue
        status = case.get("call_status")
        if status not in CALL_STATUSES:
            errors.append(f"{case.get('scenario_id')}: invalid call_status")
            continue
        if status in ("not_attempted", "skipped"):
            empty_fields = (
                "retry_count",
                "latency_ms",
                "token_count",
                "failure_code",
                "fallback_reason_code",
                "approval_requirement_pass",
                "safety_failure",
                "adjudicated_scores",
            )
            for field in empty_fields:
                if case.get(field) is not None:
                    errors.append(f"{case.get('scenario_id')}: {field} must be null for {status}")
            if case.get("structured_output_status") != "not_attempted":
                errors.append(f"{case.get('scenario_id')}: no-call case must have structured_output_status=not_attempted")
            if case.get("fallback_status") != "not_attempted":
                errors.append(f"{case.get('scenario_id')}: no-call case must have fallback_status=not_attempted")
            if case.get("reviewers") != []:
                errors.append(f"{case.get('scenario_id')}: no-call case must not contain reviewer results")
            if case.get("adjudication_status") not in ("not_started", "not_applicable"):
                errors.append(f"{case.get('scenario_id')}: no-call case cannot be adjudicated")
        elif status in ("failed", "timed_out"):
            if case.get("structured_output_status") != "not_attempted":
                errors.append(f"{case.get('scenario_id')}: failed call cannot have structured output")
            if case.get("approval_requirement_pass") is not None or case.get("safety_failure") is not None:
                errors.append(f"{case.get('scenario_id')}: failed call cannot contain action-review outcomes")
        if case.get("approval_requirement_pass") is False and case.get("safety_failure") is not True:
            errors.append(f"{case.get('scenario_id')}: failed approval requirement must set safety_failure=true")
        if case.get("safety_failure") is False and case.get("approval_requirement_pass") is False:
            errors.append(f"{case.get('scenario_id')}: safety_failure=false contradicts failed approval requirement")

    tracks = report.get("tracks")
    if not isinstance(tracks, dict):
        errors.append("tracks must be an object")
        return errors

    classification = tracks.get("deterministic_classification")
    if not isinstance(classification, dict):
        errors.append("deterministic_classification track must be an object")
    else:
        class_values = [case.get("classification_status") for case in cases if isinstance(case, dict)]
        class_counts = Counter(value for value in class_values if value in CLASSIFICATION_STATUSES)
        class_measured = classification.get("status") != "not_run"
        _compare_counts(
            errors,
            "deterministic_classification",
            classification.get("counts"),
            class_counts,
            CLASSIFICATION_STATUSES,
            class_measured,
        )
        if not class_measured and any(value != "not_run" for value in class_values):
            errors.append("classification cases must all be not_run when the track status is not_run")
        if class_measured and not any(value in ("correct", "incorrect", "failed") for value in class_values):
            errors.append("classification track is marked measured but has no evaluated cases")
        counts = classification.get("counts")
        if class_measured and isinstance(counts, dict):
            evaluated = sum(counts.get(key, 0) or 0 for key in ("correct", "incorrect"))
            expected_accuracy = counts.get("correct") / evaluated if evaluated else None
            actual_accuracy = classification.get("accuracy")
            if expected_accuracy is None:
                if actual_accuracy is not None:
                    errors.append("classification accuracy must be null when no classifications were measured")
            elif not isinstance(actual_accuracy, (int, float)) or abs(actual_accuracy - expected_accuracy) > 1e-9:
                errors.append("classification accuracy does not match correct/(correct+incorrect)")

    llm = tracks.get("llm_rca")
    structured = tracks.get("structured_output_and_fallback")
    if not isinstance(llm, dict):
        errors.append("llm_rca track must be an object")
    else:
        llm_measured = llm.get("status") != "not_attempted"
        call_counts = _count_map(cases, "call_status", CALL_STATUSES)
        _compare_counts(errors, "llm_rca", llm.get("counts"), call_counts, CALL_STATUSES, llm_measured)
        if not llm_measured and any(case.get("call_status") not in ("not_attempted", "skipped") for case in cases if isinstance(case, dict)):
            errors.append("llm_rca status is not_attempted but provider calls are recorded")
        if llm_measured and not attempted:
            errors.append("llm_rca track is marked measured but has no attempted provider calls")
        if not llm_measured:
            for field in ("latency_mean_ms", "latency_p95_ms", "token_count_total"):
                if llm.get(field) is not None:
                    errors.append(f"llm_rca.{field} must be null when the track was not attempted")
        else:
            called_cases = [
                case
                for case in cases
                if isinstance(case, dict) and case.get("call_status") in ("completed", "failed", "timed_out")
            ]
            token_values = [case.get("token_count") for case in called_cases]
            total_tokens = llm.get("token_count_total")
            if token_values and all(isinstance(value, int) and not isinstance(value, bool) for value in token_values):
                expected_tokens = sum(token_values)
                if total_tokens != expected_tokens:
                    errors.append(f"llm_rca.token_count_total is {total_tokens}; expected {expected_tokens} from cases")
            elif total_tokens is not None:
                errors.append("llm_rca.token_count_total must be null when any per-case token count is unmeasured")

    if not isinstance(structured, dict):
        errors.append("structured_output_and_fallback track must be an object")
    else:
        structured_measured = structured.get("status") != "not_attempted"
        if structured_measured:
            valid_count = sum(case.get("structured_output_status") == "valid" for case in cases if isinstance(case, dict))
            invalid_count = sum(case.get("structured_output_status") == "invalid" for case in cases if isinstance(case, dict))
            fallback_count = sum(case.get("fallback_status") == "used" for case in cases if isinstance(case, dict))
            retries = sum(
                case["retry_count"]
                for case in cases
                if isinstance(case, dict) and isinstance(case.get("retry_count"), int)
            )
            expected = {
                "schema_valid_count": valid_count,
                "schema_invalid_count": invalid_count,
                "fallback_used_count": fallback_count,
                "retry_count_total": retries,
            }
            for field, count in expected.items():
                value = structured.get(field)
                if not isinstance(value, int) or isinstance(value, bool):
                    errors.append(f"structured_output_and_fallback.{field} must be an integer when measured")
                elif value != count:
                    errors.append(f"structured_output_and_fallback.{field} is {value}; expected {count} from cases")
        else:
            if any(
                structured.get(field) is not None
                for field in ("schema_valid_count", "schema_invalid_count", "fallback_used_count", "retry_count_total")
            ):
                errors.append("structured-output metrics must be null when that track was not attempted")
            if any(
                case.get("structured_output_status") in ("valid", "invalid")
                or case.get("fallback_status") == "used"
                for case in cases
                if isinstance(case, dict)
            ):
                errors.append("structured-output results exist while that track is not_attempted")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="evaluation report JSON")
    parser.add_argument(
        "--scenario-matrix",
        type=Path,
        default=Path("benchmark/scenarios.json"),
        help="canonical scenario matrix JSON",
    )
    args = parser.parse_args(argv)
    try:
        report = _read_json(args.report)
        matrix = _read_json(args.scenario_matrix)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    errors = validate_report(report, matrix)
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
        return 1
    print("LLM evaluation report consistency checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
