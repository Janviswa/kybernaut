import json
from pathlib import Path

from scripts.validate_llm_evaluation_report import validate_report


ROOT = Path(__file__).resolve().parents[1]
MATRIX = json.loads((ROOT / "benchmark/scenarios.json").read_text(encoding="utf-8"))


def _not_attempted_report():
    cases = [
        {
            "scenario_id": item["scenario_id"],
            "scenario_category_metadata": item["expected_category"],
            "rca_ground_truth_verified": False,
            "classification_status": "not_run",
            "call_status": "not_attempted",
            "structured_output_status": "not_attempted",
            "fallback_status": "not_attempted",
            "failure_code": None,
            "fallback_reason_code": None,
            "retry_count": None,
            "latency_ms": None,
            "token_count": None,
            "reviewers": [],
            "adjudication_status": "not_started",
            "adjudicated_scores": None,
            "approval_requirement_pass": None,
            "safety_failure": None,
        }
        for item in MATRIX["cases"]
    ]
    controls = [
        {
            "control_id": item["control_id"],
            "evidence_status": "synthetic_placeholder_not_observed",
            "submitted_to_provider": False,
            "observed_window": None,
        }
        for item in MATRIX["controls"]
    ]
    return {
        "cases": cases,
        "healthy_controls": controls,
        "provider": {"provider_id": None, "exact_model_identifier": None},
        "approval": {
            "status": "not_requested",
            "approval_reference": None,
            "owner_reference": None,
            "approved_at_utc": None,
            "synthetic_evidence_only": False,
        },
        "tracks": {
            "deterministic_classification": {
                "status": "not_run",
                "counts": {
                    "correct": None,
                    "incorrect": None,
                    "failed": None,
                    "not_run": None,
                },
                "accuracy": None,
            },
            "llm_rca": {
                "status": "not_attempted",
                "counts": {
                    "completed": None,
                    "failed": None,
                    "timed_out": None,
                    "skipped": None,
                    "not_attempted": None,
                },
                "latency_mean_ms": None,
                "latency_p95_ms": None,
                "token_count_total": None,
            },
            "structured_output_and_fallback": {
                "status": "not_attempted",
                "schema_valid_count": None,
                "schema_invalid_count": None,
                "fallback_used_count": None,
                "retry_count_total": None,
            },
        },
    }


def test_accepts_explicit_unmeasured_report():
    assert validate_report(_not_attempted_report(), MATRIX) == []


def test_rejects_duplicate_and_unexpected_case_ids():
    report = _not_attempted_report()
    report["cases"][1]["scenario_id"] = report["cases"][0]["scenario_id"]
    errors = validate_report(report, MATRIX)
    assert any("must be unique" in error for error in errors)
    assert any("exactly match" in error for error in errors)


def test_rejects_mismatched_aggregate_and_zero_for_unmeasured_track():
    report = _not_attempted_report()
    report["tracks"]["llm_rca"]["counts"]["completed"] = 0
    report["tracks"]["deterministic_classification"]["counts"]["correct"] = 0
    errors = validate_report(report, MATRIX)
    assert any("must be null when the track was not measured" in error for error in errors)


def test_rejects_skipped_case_with_fabricated_model_results():
    report = _not_attempted_report()
    case = report["cases"][0]
    case["call_status"] = "skipped"
    case["token_count"] = 5
    errors = validate_report(report, MATRIX)
    assert any("token_count must be null for skipped" in error for error in errors)


def test_rejects_attempted_call_without_approval():
    report = _not_attempted_report()
    case = report["cases"][0]
    case.update(
        call_status="timed_out",
        structured_output_status="not_attempted",
        fallback_status="not_used",
        failure_code="timeout",
        retry_count=0,
        latency_ms=1000,
    )
    errors = validate_report(report, MATRIX)
    assert any("require approved owner approval metadata" in error for error in errors)
    assert any("require provider and exact model identifiers" in error for error in errors)


def test_rejects_token_total_when_case_usage_is_unmeasured():
    report = _not_attempted_report()
    report["provider"] = {"provider_id": "provider-x", "exact_model_identifier": "model-y"}
    report["approval"] = {
        "status": "approved",
        "approval_reference": "approval-1",
        "owner_reference": "owner-1",
        "approved_at_utc": "2026-10-26T00:00:00Z",
        "synthetic_evidence_only": True,
    }
    report["cases"][0].update(
        call_status="timed_out",
        failure_code="timeout",
        fallback_status="not_used",
        retry_count=0,
        latency_ms=25,
    )
    report["tracks"]["llm_rca"].update(
        status="partial",
        counts={
            "completed": 0,
            "failed": 0,
            "timed_out": 1,
            "skipped": 0,
            "not_attempted": 19,
        },
        token_count_total=0,
    )
    errors = validate_report(report, MATRIX)
    assert any("must be null when any per-case token count is unmeasured" in error for error in errors)


def test_rejects_control_and_approval_safety_contradictions():
    report = _not_attempted_report()
    report["healthy_controls"][0]["control_id"] = report["healthy_controls"][1]["control_id"]
    report["healthy_controls"][1].update(
        evidence_status="observed",
        observed_window=None,
    )
    report["cases"][0]["approval_requirement_pass"] = False
    report["cases"][0]["safety_failure"] = False
    errors = validate_report(report, MATRIX)
    assert any("control_id values must be unique" in error for error in errors)
    assert any("observed control requires observed_window" in error for error in errors)
    assert any("failed approval requirement" in error for error in errors)
    assert any("contradicts" in error for error in errors)


def test_rejects_aggregate_classification_mismatch():
    report = _not_attempted_report()
    report["tracks"]["deterministic_classification"]["status"] = "completed"
    report["tracks"]["deterministic_classification"]["counts"] = {
        "correct": 0,
        "incorrect": 0,
        "failed": 0,
        "not_run": 20,
    }
    report["tracks"]["deterministic_classification"]["accuracy"] = None
    errors = validate_report(report, MATRIX)
    assert any("measured but has no evaluated cases" in error for error in errors)
