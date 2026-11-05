import pytest
from pydantic import ValidationError

from app.models.incident_contract import (
    IncidentContract,
    IncidentEvidence,
)


def evidence(status="available", source="kubernetes", target=None,
             observed_at="2026-10-22T06:00:00+00:00"):
    return IncidentEvidence(
        evidence_id="pod-uid",
        source=source,
        status=status,
        observed_at=observed_at,
        target=target,
        details=["CrashLoopBackOff"],
    )


def test_contract_accepts_evidence_backed_kubernetes_target():
    target = {
        "namespace": "kybernaut-lab",
        "resource": "demo-web-abc-1",
        "resource_kind": "Pod",
        "deployment": "demo-web",
    }

    contract = IncidentContract(
        incident_id="INC-1",
        service="kubernetes-lab",
        evidence=[
            evidence(target=target),
        ],
        classification={
            "incident_type": "pod_crash",
            "confidence": 0.85,
            "severity": "high",
            "target": target,
            "evidence": ["CrashLoopBackOff"],
            "method": "rules",
        },
    )

    assert contract.classification.target.resource == "demo-web-abc-1"


@pytest.mark.parametrize(
    "payload",
    [
        {"observed_at": "2026-10-22T06:00:00"},
        {"observed_at": "not-a-timestamp"},
        {"status": "healthy"},
    ],
)
def test_evidence_rejects_naive_timestamp_and_unsupported_status(payload):
    values = {
        "evidence_id": "pod-uid",
        "source": "kubernetes",
        "status": "available",
        "observed_at": "2026-10-22T06:00:00+00:00",
    }
    values.update(payload)

    with pytest.raises(ValidationError):
        IncidentEvidence.model_validate(values)


def test_classification_rejects_invalid_confidence():
    with pytest.raises(ValidationError):
        IncidentContract(
            incident_id="INC-2",
            service="kubernetes-lab",
            classification={
                "incident_type": "pod_crash",
                "confidence": 1.1,
                "severity": "high",
                "target": None,
                "evidence": [],
                "method": "rules",
            },
        )


def test_classification_target_requires_matching_available_kubernetes_evidence():
    target = {
        "namespace": "kybernaut-lab",
        "resource": "demo-web-abc-1",
        "resource_kind": "Pod",
        "deployment": "demo-web",
    }
    classification = {
        "incident_type": "pod_crash",
        "confidence": 0.85,
        "severity": "high",
        "target": target,
        "evidence": ["CrashLoopBackOff"],
        "method": "rules",
    }

    with pytest.raises(ValidationError, match="lacks matching available evidence"):
        IncidentContract(
            incident_id="INC-3",
            service="kubernetes-lab",
            evidence=[evidence(status="unavailable", target=None)],
            classification=classification,
        )


def test_evidence_target_requires_available_kubernetes_source():
    target = {
        "namespace": "kybernaut-lab",
        "resource": "demo-web-abc-1",
        "resource_kind": "Pod",
        "deployment": "demo-web",
    }

    with pytest.raises(ValidationError, match="target requires available"):
        evidence(status="stale", target=target)


def test_dry_run_cannot_claim_recovery():
    with pytest.raises(ValidationError, match="Recovery requires"):
        IncidentContract(
            incident_id="INC-4",
            service="kubernetes-lab",
            execution={"executed": False, "mode": "dry_run"},
            verification_status="recovered",
        )


def test_dry_run_requires_not_run_verification_status():
    with pytest.raises(ValidationError, match="must have verification status"):
        IncidentContract(
            incident_id="INC-5",
            service="kubernetes-lab",
            execution={"executed": False, "mode": "dry_run"},
        )


def test_recovery_requires_successful_execution():
    with pytest.raises(ValidationError, match="successfully executed"):
        IncidentContract(
            incident_id="INC-6",
            service="kubernetes-lab",
            execution={"executed": True, "ok": False, "mode": "live"},
            verification_status="recovered",
        )


def test_successfully_executed_action_can_be_verified_recovered():
    contract = IncidentContract(
        incident_id="INC-7",
        service="kubernetes-lab",
        execution={"executed": True, "ok": True, "mode": "live"},
        verification_status="recovered",
    )

    assert contract.verification_status == "recovered"


def test_simulated_execution_cannot_claim_recovery():
    with pytest.raises(ValidationError, match="Recovery requires"):
        IncidentContract(
            incident_id="INC-8",
            service="kubernetes-lab",
            execution={
                "executed": False,
                "ok": True,
                "mode": "simulated",
            },
            verification_status="not_recovered",
        )


def test_simulated_execution_must_not_be_marked_executed():
    with pytest.raises(ValidationError, match="simulated action cannot be executed"):
        IncidentContract(
            incident_id="INC-9",
            service="kubernetes-lab",
            execution={
                "executed": True,
                "ok": True,
                "mode": "simulated",
            },
            verification_status="not_run",
        )


def test_whitespace_only_target_identity_is_invalid():
    with pytest.raises(ValidationError):
        evidence(target={
            "namespace": " ",
            "resource": "demo-web-abc-1",
            "resource_kind": "Pod",
            "deployment": "demo-web",
        })


def test_stale_evidence_requires_an_observation_timestamp():
    with pytest.raises(ValidationError, match="requires observed_at"):
        IncidentEvidence(
            evidence_id="stale-sample",
            source="prometheus",
            status="stale",
        )
