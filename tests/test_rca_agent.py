import pytest
from pydantic import ValidationError

from app.services.rca_agent import RCAResult, generate_rca


def test_rca_result_accepts_valid_output():
    result = RCAResult(
        root_cause="Prometheus reports the service as down.",
        confidence=0.95,
        evidence=["Prometheus availability is 0."],
        recommended_action="request_approval",
        requires_human_approval=True,
    )

    assert result.confidence == 0.95
    assert result.requires_human_approval is True


def test_rca_result_rejects_invalid_confidence():
    with pytest.raises(ValidationError):
        RCAResult(
            root_cause="Unknown",
            confidence=1.5,
            evidence=["Insufficient evidence."],
            recommended_action="investigate_further",
            requires_human_approval=True,
        )


def test_rca_result_rejects_unsupported_action():
    with pytest.raises(ValidationError):
        RCAResult(
            root_cause="Unknown",
            confidence=0.2,
            evidence=["Insufficient evidence."],
            recommended_action="restart_everything",
            requires_human_approval=True,
        )


@pytest.mark.asyncio
async def test_generate_rca_requires_api_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        await generate_rca(
            {
                "service": "order-service",
                "status": "down",
                "findings": [],
            }
        )
