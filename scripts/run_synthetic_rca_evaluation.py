#!/usr/bin/env python3
"""Prepare and validate synthetic RCA inputs without provider calls.

Provider execution is deliberately not implemented. Future inference requires
owner approval of the provider and exact model, synthetic-only input scope,
provider retention/logging, report custodian and access, output location, and
explicit authorization to make calls. No environment variable or CLI option
is sufficient evidence of those approvals.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_synthetic_benchmark import _evidence_for  # noqa: E402
from scripts.validate_llm_evaluation_report import validate_report  # noqa: E402

DEFAULT_MATRIX = PROJECT_ROOT / "benchmark" / "scenarios.json"
DEFAULT_REFERENCE_NOTES = (
    PROJECT_ROOT / "benchmark" / "llm_evaluation" / "reference_notes_v1.json"
)
DEFAULT_SCHEMA = (
    PROJECT_ROOT / "benchmark" / "llm_evaluation" / "report_schema_v2.json"
)


class PreparationError(ValueError):
    """Raised when source fixtures or report preparation are inconsistent."""


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreparationError(f"Cannot load JSON fixture {path}: {exc}") from exc


def _validate_source_data(matrix: Any, notes: Any) -> tuple[list[dict], list[dict]]:
    if not isinstance(matrix, dict) or not isinstance(notes, dict):
        raise PreparationError("Scenario matrix and reference notes must be JSON objects")
    scenarios = matrix.get("cases")
    controls = matrix.get("controls")
    note_cases = notes.get("cases")
    if not all(isinstance(value, list) for value in (scenarios, controls, note_cases)):
        raise PreparationError("Matrix cases, controls, and reference cases must be arrays")
    scenario_ids = [
        item.get("scenario_id") for item in scenarios if isinstance(item, dict)
    ]
    note_ids = [
        item.get("scenario_id") for item in note_cases if isinstance(item, dict)
    ]
    if len(scenario_ids) != len(scenarios) or len(set(scenario_ids)) != len(scenario_ids):
        raise PreparationError("Scenario matrix must have unique scenario IDs")
    if len(note_ids) != len(note_cases) or len(set(note_ids)) != len(note_ids):
        raise PreparationError("Reference notes must have unique scenario IDs")
    if set(note_ids) != set(scenario_ids) or len(note_ids) != len(scenario_ids):
        raise PreparationError("Reference-note IDs must exactly match positive matrix IDs")
    if len(controls) != 5 or any(not isinstance(item, dict) for item in controls):
        raise PreparationError("Scenario matrix must contain five valid controls")
    control_ids = [item.get("control_id") for item in controls]
    if any(not isinstance(item, str) for item in control_ids) or len(set(control_ids)) != 5:
        raise PreparationError("Scenario matrix controls must have unique string IDs")

    notes_by_id = {item["scenario_id"]: item for item in note_cases}
    for scenario in scenarios:
        scenario_id = scenario["scenario_id"]
        note = notes_by_id[scenario_id]
        if scenario.get("expected_category") not in {
            "cpu_spike",
            "memory_leak",
            "pod_crash",
            "disk_full",
            "deployment_failure",
        }:
            raise PreparationError(f"{scenario_id}: unsupported expected category")
        if not isinstance(scenario.get("expected_outcome"), str):
            raise PreparationError(f"{scenario_id}: expected outcome must be a string")
        if not isinstance(scenario.get("variant"), str):
            raise PreparationError(f"{scenario_id}: variant metadata must be a string")
        if note.get("scenario_category_metadata") != scenario.get("expected_category"):
            raise PreparationError(f"{scenario_id}: reference category does not match matrix")
        if note.get("expected_outcome_metadata") != scenario.get("expected_outcome"):
            raise PreparationError(f"{scenario_id}: reference outcome does not match matrix")
        if note.get("variant_metadata") != scenario.get("variant"):
            raise PreparationError(f"{scenario_id}: reference variant does not match matrix")
        if note.get("rca_ground_truth") is not False:
            raise PreparationError(f"{scenario_id}: RCA ground truth must remain unverified")
    return scenarios, controls


def _validate_requested_ids(
    requested_ids: Sequence[str] | None,
    scenarios: list[dict],
) -> list[str]:
    canonical_ids = [item["scenario_id"] for item in scenarios]
    chosen = canonical_ids if requested_ids is None else list(requested_ids)
    if any(not isinstance(item, str) for item in chosen):
        raise PreparationError("Inputs must be scenario ID strings, not evidence payloads")
    if len(chosen) != len(set(chosen)):
        raise PreparationError("Requested scenario IDs must be unique")
    if set(chosen) != set(canonical_ids) or len(chosen) != len(canonical_ids):
        unexpected = sorted(set(chosen) - set(canonical_ids))
        missing = sorted(set(canonical_ids) - set(chosen))
        raise PreparationError(
            f"Requested IDs must exactly cover positive scenarios; missing={missing}, "
            f"unexpected={unexpected}"
        )
    return chosen


def _rca_input_from_fixture(category: str, scenario_id: str) -> dict[str, Any]:
    """Adapt only deterministic fixture output to the bounded RCA input shape."""
    if not isinstance(category, str) or category not in {
        "cpu_spike",
        "memory_leak",
        "pod_crash",
        "disk_full",
        "deployment_failure",
    }:
        raise PreparationError(f"{scenario_id}: unsupported fixture category")
    kubernetes, telemetry = _evidence_for(category, scenario_id)
    if not isinstance(kubernetes, dict) or not isinstance(telemetry, dict):
        raise PreparationError(f"{scenario_id}: fixture builder returned invalid data")

    namespace = kubernetes.get("namespace")
    deployments = kubernetes.get("deployments")
    incidents = kubernetes.get("incidents")
    if namespace != "kybernaut-lab" or not isinstance(deployments, list) or not isinstance(incidents, list):
        raise PreparationError(f"{scenario_id}: fixture builder returned malformed Kubernetes evidence")
    if deployments != [{"name": "demo-web"}]:
        raise PreparationError(f"{scenario_id}: fixture deployment does not match the synthetic fixture")
    if category == "cpu_spike":
        if incidents:
            raise PreparationError(f"{scenario_id}: CPU fixture unexpectedly contains incidents")
    elif len(incidents) != 1 or not isinstance(incidents[0], dict):
        raise PreparationError(f"{scenario_id}: expected one synthetic Kubernetes incident")

    # Only approved fixture fields are copied. No workflow state or collector
    # result is accepted by this function.
    return {
        "service_diagnosis": {
            "service": "demo-web",
            "status": "synthetic_fixture",
            "evidence_status": "synthetic_fixture",
            "missing_evidence": [],
        },
        "kubernetes_observations": {
            "namespace": namespace,
            "incidents": incidents,
        },
        "telemetry_anomaly": {
            "status": telemetry.get("status"),
            "actionable_candidates": telemetry.get("actionable_candidates"),
        },
    }


def _not_attempted_case(scenario: dict) -> dict[str, Any]:
    return {
        "scenario_id": scenario["scenario_id"],
        "scenario_category_metadata": scenario["expected_category"],
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


def _repository_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreparationError("Could not determine the repository revision") from exc
    revision = result.stdout.strip()
    if len(revision) not in (40, 64) or any(char not in "0123456789abcdef" for char in revision.lower()):
        raise PreparationError("Git returned an invalid repository revision")
    return revision


def build_report(
    *,
    scenario_ids: Sequence[str] | None = None,
    matrix_path: Path = DEFAULT_MATRIX,
    reference_notes_path: Path = DEFAULT_REFERENCE_NOTES,
    repository_revision: str | None = None,
    started_at: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a not-attempted report and allowlisted synthetic RCA inputs."""
    matrix_bytes = matrix_path.read_bytes()
    try:
        matrix = json.loads(matrix_bytes)
    except json.JSONDecodeError as exc:
        raise PreparationError(f"Invalid scenario matrix JSON: {exc}") from exc
    notes = _load_json(reference_notes_path)
    scenarios, controls = _validate_source_data(matrix, notes)
    requested = _validate_requested_ids(scenario_ids, scenarios)
    scenarios_by_id = {item["scenario_id"]: item for item in scenarios}
    cases = [scenarios_by_id[scenario_id] for scenario_id in requested]
    if len(cases) != 20:
        raise PreparationError("The positive RCA evaluation requires exactly 20 scenarios")

    inputs = [
        {
            "scenario_id": scenario["scenario_id"],
            "diagnosis": _rca_input_from_fixture(
                scenario["expected_category"], scenario["scenario_id"]
            ),
        }
        for scenario in cases
    ]

    now = started_at or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise PreparationError("Report timestamp must be timezone-aware")
    timestamp = now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    report = {
        "protocol_version": "2.0",
        "run": {
            "run_id": f"offline-preparation-{timestamp}",
            "started_at_utc": timestamp,
            "completed_at_utc": timestamp,
            "repository_revision": repository_revision or _repository_revision(),
            "scenario_matrix_sha256": hashlib.sha256(matrix_bytes).hexdigest(),
        },
        "provider": {
            "provider_id": None,
            "exact_model_identifier": None,
        },
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
        "cases": [_not_attempted_case(scenario) for scenario in cases],
        "healthy_controls": [
            {
                "control_id": control["control_id"],
                "evidence_status": "synthetic_placeholder_not_observed",
                "submitted_to_provider": False,
                "observed_window": None,
            }
            for control in controls
        ],
        "limitations": [
            "Offline fixture preparation only; no model call or RCA-quality result was measured.",
            "Expected categories are scenario metadata, not verified RCA ground truth.",
            "Retry and token counts are unavailable until a verified provider source exists.",
        ],
    }
    return report, inputs


def _schema_errors(report: dict[str, Any], schema_path: Path) -> list[str]:
    schema = _load_json(schema_path)
    try:
        from jsonschema import Draft202012Validator, FormatChecker
    except ImportError:
        return [
            "Draft 2020-12 validator is unavailable in this Python interpreter; "
            "install project dependencies with `python -m pip install -r requirements.txt`."
        ]

    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        return [f"Invalid Draft 2020-12 report schema: {type(exc).__name__}"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return [error.message for error in validator.iter_errors(report)]


def validate_prepared_report(
    report: dict[str, Any],
    *,
    matrix_path: Path = DEFAULT_MATRIX,
    schema_path: Path = DEFAULT_SCHEMA,
) -> list[str]:
    """Validate the report with JSON Schema and semantic consistency checks."""
    matrix = _load_json(matrix_path)
    errors = [f"schema: {item}" for item in _schema_errors(report, schema_path)]
    errors.extend(
        f"semantic: {item}" for item in validate_report(report, matrix)
    )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare and validate synthetic RCA inputs offline. Provider execution "
            "is not implemented; future calls require explicit owner approvals for "
            "provider/model, synthetic scope, retention, custody, output location, "
            "and call authorization."
        )
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=DEFAULT_MATRIX,
        help="scenario matrix JSON (default: canonical repository matrix)",
    )
    parser.add_argument(
        "--reference-notes",
        type=Path,
        default=DEFAULT_REFERENCE_NOTES,
        help="versioned synthetic evidence notes",
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
        help="Draft 2020-12 report schema",
    )
    args = parser.parse_args(argv)
    try:
        report, inputs = build_report(
            matrix_path=args.matrix,
            reference_notes_path=args.reference_notes,
        )
        errors = validate_prepared_report(
            report,
            matrix_path=args.matrix,
            schema_path=args.schema,
        )
    except (OSError, PreparationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("\n".join(f"ERROR: {error}" for error in errors), file=sys.stderr)
        return 1
    print(json.dumps({
        "status": "prepared_not_attempted",
        "positive_scenario_count": len(inputs),
        "control_count": len(report["healthy_controls"]),
        "provider_calls": 0,
        "report": report,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
