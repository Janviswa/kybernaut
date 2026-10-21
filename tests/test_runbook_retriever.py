import pytest

from app.services.runbook_retriever import retrieve_runbooks


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("memory pressure memory leak OOM", "memory-pressure.md"),
        ("pod crash CrashLoopBackOff restart count", "pod-crash.md"),
        ("database connection timeout", "database-timeout.md"),
        ("deployment rollout readiness failure", "deployment-failure.md"),
        ("high CPU throttling latency", "high-cpu.md"),
    ],
)
def test_retrieves_expected_runbook(query, expected):
    results = retrieve_runbooks(query, top_k=1)

    assert results
    assert results[0]["source"] == expected
    assert results[0]["content"]
    assert results[0]["matched_terms"]


def test_empty_query_returns_no_results():
    assert retrieve_runbooks("") == []
    assert retrieve_runbooks("   ") == []


def test_top_k_must_be_positive():
    with pytest.raises(ValueError):
        retrieve_runbooks("memory leak", top_k=0)


def test_results_are_limited_to_top_k():
    results = retrieve_runbooks(
        "memory CPU pod database deployment",
        top_k=2,
    )

    assert len(results) <= 2

def test_memory_query_ranks_memory_runbook_first():
    query = (
        "payment-service high_memory_usage "
        "memory usage elevated"
    )

    results = retrieve_runbooks(query, top_k=3)

    assert results
    assert results[0]["source"] == "memory-pressure.md"
