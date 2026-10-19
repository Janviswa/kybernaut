"""LangChain-based root cause analysis for Kybernaut incidents."""

import os
from typing import Literal

from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate


class RCAResult(BaseModel):
    """Validated, advisory root cause analysis."""

    root_cause: str = Field(
        description="Most likely cause supported by the supplied evidence."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence justified by the available evidence."
    )
    evidence: list[str] = Field(
        min_length=1,
        description="Evidence taken from the supplied diagnosis."
    )
    recommended_action: Literal[
        "continue_monitoring",
        "investigate_monitoring",
        "investigate_errors",
        "investigate_latency",
        "request_approval",
        "investigate_further",
    ]
    requires_human_approval: bool = Field(
        description="Whether a human must approve an operational change."
    )


RCA_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """You are Kybernaut, an infrastructure reliability analyst.

Analyze only the supplied monitoring diagnosis.
Do not invent metrics, logs, events, or infrastructure facts.
Distinguish confirmed evidence from hypotheses.
If evidence is insufficient, state that in root_cause and use
a low confidence score.
Recommend investigation before recovery when the cause is uncertain.
Any restart, scaling, rollback, or other infrastructure change requires
human approval. Never claim that you executed an action.
Return only data matching the requested schema.""",
        ),
        (
            "human",
            "Analyze this monitoring diagnosis:\n{diagnosis}",
        ),
    ]
)


async def generate_rca(diagnosis: dict) -> dict:
    """Generate structured RCA from existing monitoring evidence."""

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY is not configured; RCA generation is unavailable."
        )

    from langchain_groq import ChatGroq

    model_name = os.getenv(
        "KYBERNAUT_LLM_MODEL",
        "openai/gpt-oss-120b",
    )

    llm = ChatGroq(
        model=model_name,
        api_key=api_key,
        temperature=0,
    )

    chain = RCA_PROMPT | llm.with_structured_output(RCAResult)

    result = await chain.ainvoke(
        {"diagnosis": str(diagnosis)}
    )

    return result.model_dump()
