
"""Deterministic, weighted keyword retrieval for Kybernaut runbooks."""

import re
from pathlib import Path

RUNBOOK_DIR = Path(__file__).resolve().parents[2] / "runbooks"

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by",
    "for", "from", "has", "have", "how", "i", "in", "is",
    "it", "of", "on", "or", "our", "that", "the", "this",
    "to", "was", "were", "what", "when", "where", "which",
    "why", "with", "you", "your",
    "service", "issue", "problem", "investigate",
    "evidence", "observed", "check", "checks", "using",
    "used", "use", "ensure", "verify", "appropriate",
}

# These terms are more diagnostic than generic keyword matches.
IMPORTANT_TERMS = {
    "memory",
    "cpu",
    "timeout",
    "crash",
    "deployment",
    "database",
    "latency",
    "oom",
    "restart",
    "replica",
    "pod",
    "connection",
    "failure",
    "failed",
    "unavailable",
    "5xx",
}


def _tokens(text: str) -> set[str]:
    """Normalize text into searchable lowercase terms."""
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if token not in STOP_WORDS
    }


def _score(query_tokens: set[str], content_tokens: set[str]) -> tuple[float, list[str]]:
    """Score keyword coverage, giving important operational terms more weight."""
    matched = query_tokens & content_tokens

    if not matched:
        return 0.0, []

    total_weight = sum(
        3 if token in IMPORTANT_TERMS else 1
        for token in query_tokens
    )
    matched_weight = sum(
        3 if token in IMPORTANT_TERMS else 1
        for token in matched
    )

    score = matched_weight / total_weight if total_weight else 0.0
    return score, sorted(matched)


def retrieve_runbooks(query: str, top_k: int = 3) -> list[dict]:
    """Return runbooks ranked by weighted keyword coverage."""
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1:
        raise ValueError("top_k must be an integer greater than or equal to 1")

    if not isinstance(query, str) or not query.strip():
        return []

    if not RUNBOOK_DIR.is_dir():
        return []

    query_tokens = _tokens(query)

    if not query_tokens:
        return []

    results = []

    for path in sorted(RUNBOOK_DIR.glob("*.md")):
        content = path.read_text(encoding="utf-8")
        content_tokens = _tokens(content)

        score, matched_terms = _score(query_tokens, content_tokens)

        if not matched_terms:
            continue

        results.append({
            "source": path.name,
            "score": score,
            "content": content,
            "matched_terms": matched_terms,
        })

    results.sort(key=lambda item: (-item["score"], item["source"]))
    return results[:top_k]
