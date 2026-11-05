"""Validated incident/evidence contract shared by response workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EvidenceStatus = Literal["available", "unavailable", "stale", "invalid"]
IncidentType = Literal[
    "cpu_spike", "memory_leak", "pod_crash", "disk_full",
    "deployment_failure", "unknown",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IncidentTarget(StrictModel):
    namespace: str = Field(min_length=1)
    resource: str = Field(min_length=1)
    resource_kind: Literal["Pod", "Deployment"]
    resource_uid: str | None = None
    deployment: str | None = None


class EvidenceSourceState(StrictModel):
    source: Literal["prometheus", "kubernetes", "telemetry"]
    status: EvidenceStatus
    observed_at: datetime | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def require_timezone_for_timestamp(self) -> "EvidenceSourceState":
        if self.observed_at is not None and self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        if self.status == "stale" and self.observed_at is None:
            raise ValueError("Stale source evidence requires observed_at")
        return self


class IncidentEvidence(StrictModel):
    evidence_id: str = Field(min_length=1)
    source: Literal["prometheus", "kubernetes", "telemetry"]
    status: EvidenceStatus
    observed_at: datetime | None = None
    target: IncidentTarget | None = None
    details: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_timezone_for_timestamp(self) -> "IncidentEvidence":
        if self.observed_at is not None and self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        if self.target is not None and (
            self.source != "kubernetes" or self.status != "available"
        ):
            raise ValueError(
                "A target requires available Kubernetes evidence"
            )
        if self.status == "stale" and self.observed_at is None:
            raise ValueError("Stale evidence requires observed_at")
        return self


class IncidentClassification(StrictModel):
    incident_type: IncidentType
    confidence: float = Field(ge=0.0, le=1.0)
    severity: Literal["low", "medium", "high", "critical"]
    target: IncidentTarget | None
    evidence: list[str] = Field(default_factory=list)
    method: Literal["rules"]


class IncidentContract(StrictModel):
    contract_version: Literal["1"] = "1"
    incident_id: str = Field(min_length=1, max_length=64)
    service: str = Field(min_length=1)
    evidence_sources: list[EvidenceSourceState] = Field(default_factory=list)
    evidence: list[IncidentEvidence] = Field(default_factory=list)
    evidence_truncated: bool = False
    classification: IncidentClassification | None = None
    diagnosis_findings: list[dict] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    proposed_action: dict | None = None
    policy_decision: dict | None = None
    approval_id: str | None = None
    approval_status: Literal[
        "pending", "approved", "rejected", "expired"
    ] | None = None
    execution: dict | None = None
    verification_status: Literal[
        "recovered", "not_recovered", "not_run", "unavailable"
    ] | None = None
    audit_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target_is_evidence_backed(self) -> "IncidentContract":
        if self.classification is not None and self.classification.target is not None:
            target = self.classification.target
            if not any(
                evidence.status == "available"
                and evidence.source == "kubernetes"
                and evidence.target == target
                for evidence in self.evidence
            ):
                raise ValueError(
                    "Classification target lacks matching available evidence"
                )

        if self.verification_status in {"recovered", "not_recovered"} and not (
            self.execution
            and self.execution.get("mode") == "live"
            and self.execution.get("executed") is True
            and self.execution.get("ok") is True
        ):
            raise ValueError(
                "Recovery requires a successfully executed live action"
            )
        if self.execution and self.execution.get("mode") == "simulated":
            if self.execution.get("executed") is not False:
                raise ValueError("A simulated action cannot be executed")
            if self.verification_status not in {None, "not_run"}:
                raise ValueError("A simulated action cannot be verified")
        if (
            self.execution
            and self.execution.get("executed") is False
            and self.verification_status in {"recovered", "not_recovered"}
        ):
            raise ValueError(
                "A non-executed action cannot have a recovery verification"
            )
        if (
            self.execution
            and self.execution.get("mode") == "dry_run"
            and self.verification_status != "not_run"
        ):
            raise ValueError("A dry-run action must have verification status not_run")
        return self
