#!/usr/bin/env python3
"""Run the canonical 20-case matrix against deterministic local fixtures."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.benchmark_metrics import (  # noqa: E402
    BenchmarkIncomplete,
    compute_metrics,
    load_scenario_matrix,
)
from app.services.incident_classifier import classify_incident  # noqa: E402
from app.services import incident_diagnoser  # noqa: E402
from app.services.policy_engine import (  # noqa: E402
    PolicyConfig,
    ProposedAction,
    evaluate_policy,
)

DEFAULT_OUTPUT = PROJECT_ROOT / "benchmark" / "results" / (
    "synthetic-benchmark.json"
)
DIAGNOSIS_STATES = {"available", "missing_evidence", "unavailable"}
POLICY_DECISIONS = {"allow", "deny", "require_approval", "not_applicable"}


class SyntheticDiagnosisUnavailable(Exception):
    pass


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Benchmark timestamps must be timezone-aware.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _diagnose_with_synthetic_prometheus(
    scenario_id: str,
    at: datetime,
) -> dict[str, Any]:
    async def query_fixture(query: str) -> list[dict[str, Any]]:
        if scenario_id == "cpu-spike-4":
            raise SyntheticDiagnosisUnavailable(
                "synthetic Prometheus fixture unavailable"
            )
        if "http_requests_total" in query:
            if scenario_id == "memory-leak-4":
                return []
            value = "0"
        elif 'up{' in query:
            value = "1"
        else:
            value = "0.1"
        return [{"metric": {}, "value": [at.timestamp(), value]}]

    with patch.object(
        incident_diagnoser, "query_prometheus", query_fixture
    ):
        try:
            return asyncio.run(incident_diagnoser.diagnose_service("demo-web"))
        except SyntheticDiagnosisUnavailable as exc:
            return {
                "status": "unknown",
                "evidence_status": "unavailable",
                "missing_evidence": ["prometheus_query"],
                "failure_reason": str(exc),
            }


def _evidence_for(category: str, scenario_id: str) -> tuple[dict, dict]:
    slug = scenario_id.replace("-", "")
    if category == "cpu_spike":
        return (
            {
                "namespace": "kybernaut-lab",
                "deployments": [{"name": "demo-web"}],
                "incidents": [],
            },
            {
                "status": "evaluated",
                "actionable_candidates": ["cpu_cores_used"],
            },
        )

    evidence_by_category = {
        "memory_leak": (
            "pod_container_failure",
            ["Container previously terminated with reason OOMKilled."],
        ),
        "pod_crash": (
            "pod_container_failure",
            ["Container is waiting with reason CrashLoopBackOff."],
        ),
        "disk_full": (
            "kubernetes_warning_event",
            [
                'Evicted: Usage of EmptyDir volume "scratch" exceeds '
                'the limit "40Mi".'
            ],
        ),
        "deployment_failure": (
            "pod_container_failure",
            ["Container is waiting with reason ImagePullBackOff."],
        ),
    }
    incident_type, evidence = evidence_by_category[category]
    return (
        {
            "namespace": "kybernaut-lab",
            "deployments": [{"name": "demo-web"}],
            "incidents": [{
                "type": incident_type,
                "resource": f"demo-web-{slug}",
                "resource_kind": "Pod",
                "resource_uid": f"synthetic-{slug}",
                "severity": "high",
                "source_timestamp": None,
                "evidence": evidence,
            }],
        },
        {"status": "unavailable", "actionable_candidates": []},
    )


def _policy_evaluation(
    classification: dict[str, Any],
    scenario_id: str,
    observed_at: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    target = classification.get("target")
    if not isinstance(target, dict):
        return (
            {
                "status": "not_evaluated",
                "expected_decision": "not_applicable",
                "actual_decision": "not_applicable",
                "matches_expected": True,
                "reason": "Classifier did not provide an actionable target.",
                "rules": [],
            },
            None,
        )

    proposal = ProposedAction(
        action="restart_pod",
        namespace=target["namespace"],
        resource=target["resource"],
        deployment=target["deployment"],
        confidence=classification["confidence"],
        severity=classification["severity"],
        rationale=f"Synthetic policy fixture for {scenario_id}.",
        source="rules",
    )
    context: dict[str, Any] = {
        "recent_action_count": 0,
        "deployment": {"desired_replicas": 2, "available_replicas": 2},
    }
    policy_config = PolicyConfig()
    profile = "default"
    expected = "require_approval"

    if scenario_id == "memory-leak-2":
        context.pop("deployment")
        expected = "deny"
    elif scenario_id == "deployment-failure-1":
        policy_config = PolicyConfig(
            auto_remediation_enabled=True,
            auto_confidence=0.9,
            auto_max_severity="high",
        )
        profile = "synthetic_auto_eligibility_probe"
        expected = "allow"

    decision = evaluate_policy(proposal, context=context, config=policy_config)
    actual = (
        "deny" if not decision.allowed
        else "allow" if decision.auto_execute
        else "require_approval"
    )
    record = {
        "status": "evaluated",
        "expected_decision": expected,
        "actual_decision": actual,
        "matches_expected": actual == expected,
        "profile": profile,
        "rules": decision.rules,
        "reasons": decision.reasons,
    }
    policy_approval = {
        "policy_decision": actual,
        "approval_required": decision.requires_approval,
        "approval_outcome": (
            "pending" if decision.requires_approval else "not_required"
        ),
        "execution_status": "not_executed",
        "verification_result": "not_run",
        "decision_at": observed_at,
    }
    return record, policy_approval


def _summary(cases: list[dict], metrics: dict[str, Any]) -> dict[str, Any]:
    classification_correct = sum(
        case["observed_category"] == case["expected_category"]
        for case in cases
    )
    policy_records = [
        case["policy_evaluation"]
        for case in cases
        if case["policy_evaluation"]["status"] == "evaluated"
    ]
    policy_matches = sum(
        record["matches_expected"] for record in policy_records
    )
    diagnosis_counts = Counter(case["diagnosis_status"] for case in cases)
    policy_counts = Counter(record["actual_decision"] for record in (
        case["policy_evaluation"] for case in cases
    ))
    classification_failures = [
        case["scenario_id"]
        for case in cases
        if case["observed_category"] != case["expected_category"]
    ]
    policy_failures = [
        case["scenario_id"]
        for case in cases
        if not case["policy_evaluation"]["matches_expected"]
    ]
    all_expectations_met = not classification_failures and not policy_failures
    return {
        "status": "complete" if all_expectations_met else "completed_with_failures",
        "all_expectations_met": all_expectations_met,
        "scenario_count": len(cases),
        "classification": {
            "correct": classification_correct,
            "denominator": len(cases),
            "accuracy": round(classification_correct / len(cases), 4)
            if cases else None,
            "failed_scenario_ids": classification_failures,
        },
        "diagnosis_availability": {
            "evaluation_scope": "synthetic fixtures only",
            "status_counts": dict(sorted(diagnosis_counts.items())),
            "available_count": diagnosis_counts["available"],
            "denominator": len(cases),
            "availability_rate": round(
                diagnosis_counts["available"] / len(cases), 4
            ) if cases else None,
        },
        "policy_contract_match": {
            "evaluation_scope": "pure deterministic policy function",
            "matched": policy_matches,
            "denominator": len(policy_records),
            "match_rate": round(policy_matches / len(policy_records), 4)
            if policy_records else None,
            "decision_counts": dict(sorted(policy_counts.items())),
            "failed_scenario_ids": policy_failures,
            "runtime_safety_status": "not_measured",
        },
        "detection": {
            "status": metrics["detection"]["status"],
            "precision": metrics["detection"]["precision"],
            "recall": metrics["detection"]["recall"],
            "f1": metrics["detection"]["f1"],
        },
        "remediation": {
            "actions_executed": sum(
                case["remediation_outcome"] == "executed" for case in cases
            ),
            "verified_recoveries": sum(
                case["verification_outcome"] == "verified" for case in cases
            ),
            "status": (
                "not_exercised"
                if all(
                    case["remediation_outcome"] == "not_executed"
                    and case["verification_outcome"] == "not_run"
                    for case in cases
                )
                else "unexpected_execution_or_verification"
            ),
        },
        "live_integrations": "not_exercised",
    }


def build_synthetic_benchmark(
    *,
    scenario_matrix_path: str | Path | None = None,
    run_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    matrix_path = (
        Path(scenario_matrix_path)
        if scenario_matrix_path
        else PROJECT_ROOT / "benchmark" / "scenarios.json"
    )
    matrix_source = (
        str(scenario_matrix_path)
        if scenario_matrix_path
        else "benchmark/scenarios.json"
    )
    matrix = load_scenario_matrix(matrix_path)
    started = now or datetime.now(timezone.utc)
    if started.tzinfo is None or started.utcoffset() is None:
        raise ValueError("Benchmark timestamps must be timezone-aware.")
    started = started.astimezone(timezone.utc)
    at = _timestamp(started)
    run_id = run_id or f"synthetic-{uuid.uuid4().hex}"
    cases = []

    for scenario in matrix["cases"]:
        scenario_id = scenario["scenario_id"]
        kubernetes, telemetry = _evidence_for(
            scenario["expected_category"], scenario_id
        )
        diagnosis = _diagnose_with_synthetic_prometheus(
            scenario_id, started
        )
        if diagnosis.get("failure_reason"):
            diagnosis_status = "unavailable"
        elif diagnosis.get("missing_evidence"):
            diagnosis_status = "missing_evidence"
        elif diagnosis.get("evidence_status") == "available":
            diagnosis_status = "available"
        else:
            diagnosis_status = "unavailable"
        classification = classify_incident(
            diagnosis,
            kubernetes,
            telemetry,
        )
        policy_evaluation, policy_approval = _policy_evaluation(
            classification, scenario_id, at
        )
        evidence_provenance = [
            {
                "evidence_ref": f"fixture://kubernetes/{scenario_id}",
                "source_identity": f"synthetic-kubernetes-fixture:{scenario_id}",
                "status": "available",
                "source_sample_timestamp": None,
                "query_evaluation_timestamp": None,
                "observation_time": at,
            },
            {
                "evidence_ref": f"fixture://telemetry/{scenario_id}",
                "source_identity": f"synthetic-telemetry-fixture:{scenario_id}",
                "status": (
                    "available"
                    if scenario["expected_category"] == "cpu_spike"
                    else "unavailable"
                ),
                "source_sample_timestamp": None,
                "query_evaluation_timestamp": None,
                "observation_time": at,
            },
            {
                "evidence_ref": f"fixture://prometheus/{scenario_id}",
                "source_identity": f"synthetic-prometheus-fixture:{scenario_id}",
                "status": (
                    "available"
                    if diagnosis_status == "available" else "unavailable"
                ),
                "source_sample_timestamp": None,
                "query_evaluation_timestamp": None,
                "observation_time": at,
            },
        ]
        case = {
            "scenario_id": scenario_id,
            "run_id": run_id,
            "expected_category": scenario["expected_category"],
            "observed_category": classification["incident_type"],
            "expected_outcome": scenario["expected_outcome"],
            "diagnosis_status": diagnosis_status,
            "detection_result": "not_measured",
            "injected_at": at,
            "detected_at": None,
            "evidence": classification["evidence"],
            "evidence_provenance": evidence_provenance,
            "remediation_outcome": "not_executed",
            "remediation_result": None,
            "verification_outcome": "not_run",
            "recovered_at": None,
            "failure_reason": (
                None
                if classification["incident_type"]
                == scenario["expected_category"]
                else "Synthetic classifier output differed from matrix label."
            ),
            "rca_json_valid": None,
            "llm_latency_ms": None,
            "llm_tokens": None,
            "expected_policy_decision": policy_evaluation[
                "expected_decision"
            ],
            "policy_evaluation": policy_evaluation,
        }
        if policy_approval is not None:
            case["policy_approval"] = policy_approval
        cases.append(case)

    controls = [{
        "control_id": control["control_id"],
        "run_id": run_id,
        "alert_raised": None,
        "observed_category": "unknown",
        "started_at": at,
        "ended_at": at,
        "evidence": [
            "Synthetic healthy fixture; no alert source or time window "
            "was observed."
        ],
        "failure_reason": None,
    } for control in matrix["controls"]]

    started_text = _timestamp(started)
    completed = now or datetime.now(timezone.utc)
    completed_text = _timestamp(completed)
    if completed < started:
        raise ValueError("Benchmark completed_at precedes started_at.")
    result: dict[str, Any] = {
        "schema_version": 2,
        "run": {
            "run_id": run_id,
            "evaluation_mode": "synthetic",
            "scenario_matrix_sha256": hashlib.sha256(
                matrix_path.read_bytes()
            ).hexdigest(),
            "started_at": started_text,
            "completed_at": completed_text,
        },
        "evaluation_provenance": {
            "runner": "scripts/run_synthetic_benchmark.py",
            "fixture_source": "deterministic in-process synthetic evidence",
            "scenario_matrix": matrix_source,
            "live_failure_injection": False,
            "external_monitoring_or_cluster": False,
            "live_remediation_invoked": False,
        },
        "cases": cases,
        "controls": controls,
    }
    result["metrics"] = compute_metrics(
        cases,
        controls,
        scenario_matrix_path=scenario_matrix_path,
        schema_version=2,
        run_envelope=result["run"],
    )
    result["summary"] = _summary(cases, result["metrics"])
    return result


def validate_synthetic_artifact(
    artifact: Any,
    *,
    scenario_matrix_path: str | Path | None = None,
) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise BenchmarkIncomplete("Result artifact must be a JSON object.")
    required = {
        "schema_version", "run", "evaluation_provenance", "cases",
        "controls", "metrics", "summary",
    }
    missing = required - artifact.keys()
    if missing:
        raise BenchmarkIncomplete(
            f"Result artifact is missing {', '.join(sorted(missing))}.")
    if artifact["schema_version"] != 2:
        raise BenchmarkIncomplete("Synthetic result artifact requires schema 2.")
    if (
        not isinstance(artifact["run"], dict)
        or artifact["run"].get("evaluation_mode") != "synthetic"
    ):
        raise BenchmarkIncomplete(
            "Synthetic result artifact must declare synthetic evaluation mode.")
    provenance = artifact["evaluation_provenance"]
    if not isinstance(provenance, dict) or any(
        provenance.get(key) is not False
        for key in (
            "live_failure_injection",
            "external_monitoring_or_cluster",
            "live_remediation_invoked",
        )
    ):
        raise BenchmarkIncomplete(
            "Synthetic provenance must explicitly rule out live execution.")

    cases = artifact["cases"]
    if not isinstance(cases, list):
        raise BenchmarkIncomplete("Result artifact cases must be a list.")
    controls = artifact["controls"]
    if not isinstance(controls, list) or any(
        not isinstance(control, dict)
        or control.get("alert_raised") is not None
        for control in controls
    ):
        raise BenchmarkIncomplete(
            "Synthetic controls must explicitly mark alert_raised null.")
    for case in cases:
        if not isinstance(case, dict):
            raise BenchmarkIncomplete("Every case must be an object.")
        if case.get("detection_result") != "not_measured":
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} cannot claim alert "
                "detection in the synthetic benchmark.")
        if case.get("diagnosis_status") not in DIAGNOSIS_STATES:
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has invalid "
                "diagnosis_status.")
        evaluation = case.get("policy_evaluation")
        if not isinstance(evaluation, dict):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} needs "
                "policy_evaluation.")
        expected = evaluation.get("expected_decision")
        actual = evaluation.get("actual_decision")
        if expected not in POLICY_DECISIONS or actual not in POLICY_DECISIONS:
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has invalid policy "
                "decision values.")
        if evaluation.get("matches_expected") is not (expected == actual):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has inconsistent "
                "policy match result.")
        if expected != case.get("expected_policy_decision"):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} expected policy "
                "decision fields disagree.")
        if evaluation.get("status") == "evaluated":
            approval = case.get("policy_approval")
            if not isinstance(approval, dict) or (
                approval.get("policy_decision") != actual
            ):
                raise BenchmarkIncomplete(
                    f"Scenario {case.get('scenario_id')!r} policy result "
                    "disagrees with policy_approval.")
        elif evaluation.get("status") == "not_evaluated":
            if actual != "not_applicable" or "policy_approval" in case:
                raise BenchmarkIncomplete(
                    f"Scenario {case.get('scenario_id')!r} has inconsistent "
                    "not_evaluated policy data.")
        else:
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has invalid policy "
                "evaluation status.")
        provenance_entries = case.get("evidence_provenance")
        if not isinstance(provenance_entries, list) or not provenance_entries:
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} needs evidence "
                "provenance.")
        if any(
            not isinstance(entry, dict)
            or not isinstance(entry.get("source_identity"), str)
            or not isinstance(entry.get("status"), str)
            or "source_sample_timestamp" not in entry
            or "query_evaluation_timestamp" not in entry
            or "observation_time" not in entry
            for entry in provenance_entries
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} has incomplete "
                "evidence provenance.")
        if (
            case.get("remediation_outcome") != "not_executed"
            or case.get("verification_outcome") != "not_run"
            or case.get("recovered_at") is not None
        ):
            raise BenchmarkIncomplete(
                f"Scenario {case.get('scenario_id')!r} claims execution or "
                "recovery in a non-executing synthetic benchmark.")

    metrics = compute_metrics(
        cases,
        controls,
        scenario_matrix_path=scenario_matrix_path,
        schema_version=artifact["schema_version"],
        run_envelope=artifact["run"],
    )
    if artifact["metrics"] != metrics:
        raise BenchmarkIncomplete("Stored metrics do not match raw result data.")
    expected_summary = _summary(cases, metrics)
    if artifact["summary"] != expected_summary:
        raise BenchmarkIncomplete("Stored summary does not match raw results.")
    return metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios",
        help="Scenario matrix JSON (defaults to benchmark/scenarios.json).",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Result artifact destination (default: {DEFAULT_OUTPUT}).",
    )
    args = parser.parse_args()
    try:
        result = build_synthetic_benchmark(
            scenario_matrix_path=args.scenarios
        )
        validate_synthetic_artifact(
            result, scenario_matrix_path=args.scenarios
        )
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, indent=2) + "\n", encoding="utf-8"
        )
    except (BenchmarkIncomplete, OSError, ValueError) as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 2

    summary = result["summary"]
    print(f"Synthetic benchmark: {summary['status']}")
    print(
        "Classification: "
        f"{summary['classification']['correct']}/"
        f"{summary['classification']['denominator']} "
        f"({summary['classification']['accuracy']:.1%})"
    )
    print(
        "Policy contract: "
        f"{summary['policy_contract_match']['matched']}/"
        f"{summary['policy_contract_match']['denominator']} expected decisions"
    )
    print(
        "Diagnosis fixture availability: "
        f"{summary['diagnosis_availability']['status_counts']}"
    )
    print("Detection: not measured; no alerting source was exercised.")
    print("Live remediation: not invoked; verification/recovery not measured.")
    print(f"Raw result artifact: {output_path}")
    return 0 if summary["all_expectations_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
