from scripts.run_reproducible_evaluation import (
    compose_report,
    render_markdown,
    render_resume_metrics,
)


def _anomaly_artifact():
    confusion = {
        "true_positives": 4,
        "false_positives": 2,
        "true_negatives": 8,
        "false_negatives": 6,
        "precision": 0.666667,
        "recall": 0.4,
        "f1": 0.5,
        "false_positive_rate": 0.2,
        "eligible_observation_count": 20,
    }
    return {
        "benchmark": "test-synthetic-comparison",
        "provenance": {
            "baseline_sha256": "a" * 64,
            "dataset_sha256": "b" * 64,
            "features": ["cpu"],
            "services": ["service-a"],
        },
        "reproducibility": {
            "label_provenance": {"synthetic": True},
            "configuration": {"threshold": 5.0},
            "random_seeds": {"test": 7},
        },
        "metrics": {
            "total_observation_count": 22,
            "eligible_observation_count": 20,
            "unavailable_observation_count": 2,
            "point_anomaly": {
                "isolation_forest": confusion,
                "direction_aware_mad": confusion,
                "hybrid": confusion,
            },
            "per_service": {
                "service-a": {
                    "point_anomaly": {
                        "direction_aware_mad": confusion,
                    },
                },
            },
        },
        "samples": [{
            "mad_actionable_features": ["cpu"],
            "mad_informational_features": [],
        }],
    }


def _synthetic_artifact():
    return {
        "summary": {
            "classification": {
                "correct": 20,
                "denominator": 20,
                "accuracy": 1.0,
            },
            "diagnosis_availability": {"status_counts": {"available": 20}},
            "policy_contract_match": {
                "correct": 16,
                "denominator": 16,
                "decision_counts": {"require_approval": 16},
            },
            "remediation": {
                "actions_executed": 0,
                "verified_recoveries": 0,
                "status": "not_exercised",
            },
        },
        "metrics": {"detection": {"status": "not_measured"}},
        "cases": [
            {
                "scenario_id": f"case-{index}",
                "expected_category": "cpu_spike",
                "observed_category": "cpu_spike",
                "detection_result": "not_measured",
                "diagnosis_status": "available",
                "policy_evaluation": {"actual_decision": "not_applicable"},
                "remediation_outcome": "not_executed",
                "failure_reason": None,
            }
            for index in range(20)
        ],
        "controls": [{}] * 5,
    }


def test_report_preserves_measured_and_unmeasured_statuses():
    report = compose_report(
        run_id="evaluation-test",
        started_at="2026-10-28T15:00:00+00:00",
        completed_at="2026-10-28T15:01:00+00:00",
        environment={"git_revision": "a" * 40, "worktree": {"state": "dirty"}},
        anomaly_artifact=_anomaly_artifact(),
        synthetic_artifact=_synthetic_artifact(),
    )

    mad = report["anomaly_detection"]["methods"]["direction_aware_mad"]
    assert mad["status"] == "measured"
    assert mad["precision"] == {
        "value": 0.666667,
        "numerator": 4,
        "denominator": 6,
        "formula": "TP / (TP + FP)",
    }
    assert report["anomaly_detection"]["per_feature_classification_metrics"][
        "status"
    ] == "not_measured"
    assert report["operational_incident_workflow"]["metrics"][
        "live_detection_mttd"
    ]["status"] == "not_measured"
    assert report["remediation_safety"]["safety_rejection_rate"] is None
    assert report["rca_quality"]["status"] == "NOT_MEASURED"
    assert report["api_latency"]["status"] == "not_measured"
    assert "synthetic" in render_markdown(report).lower()
    resume = render_resume_metrics(report)
    assert "20 eligible synthetic/assumed-labeled telemetry points" in resume
    assert "not production" in resume
