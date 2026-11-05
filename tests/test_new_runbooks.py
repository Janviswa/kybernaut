import pytest

from app.services.runbook_retriever import RUNBOOK_DIR, retrieve_runbooks

GUIDE_RUNBOOKS = {
    "oom-killed.md", "crashloopbackoff.md", "high-cpu.md", "disk-full.md",
    "pod-not-ready.md", "database-timeout.md", "deployment-failure.md",
    "network-failure.md",
}


def test_all_guide_runbooks_exist_and_have_safe_response():
    present = {p.name for p in RUNBOOK_DIR.glob("*.md")}
    assert GUIDE_RUNBOOKS <= present
    for name in GUIDE_RUNBOOKS:
        assert "## Safe response" in (RUNBOOK_DIR / name).read_text()


@pytest.mark.parametrize("query, expected", [
    ("pod evicted ephemeral storage disk", "disk-full.md"),
    ("readiness probe failing endpoints missing", "pod-not-ready.md"),
    ("dns resolution connection refused connectivity", "network-failure.md"),
])
def test_new_runbooks_are_retrievable(query, expected):
    assert retrieve_runbooks(query, top_k=1)[0]["source"] == expected
