import asyncio
import json
import sqlite3
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from app.services import k8s_observer, system_health


class PrometheusClient:
    def __init__(self, *, status_code=None, error=None, **_kwargs):
        self.status_code = status_code
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url):
        if self.error:
            raise self.error
        return SimpleNamespace(status_code=self.status_code)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(200, "healthy"), (503, "unhealthy"), (404, "degraded")],
)
async def test_prometheus_status_reflects_health_endpoint(
    monkeypatch, status_code, expected
):
    monkeypatch.setattr(
        system_health.httpx,
        "AsyncClient",
        lambda **kwargs: PrometheusClient(status_code=status_code, **kwargs),
    )

    result = await system_health.check_prometheus()

    assert result["status"] == expected
    assert result["checked_at"]
    assert result["reason"]


@pytest.mark.asyncio
async def test_prometheus_timeout_is_unknown_and_does_not_expose_exception(
    monkeypatch,
):
    monkeypatch.setattr(
        system_health.httpx,
        "AsyncClient",
        lambda **kwargs: PrometheusClient(
            error=httpx.TimeoutException("private endpoint"), **kwargs
        ),
    )

    result = await system_health.check_prometheus()

    assert result["status"] == "unknown"
    assert "private endpoint" not in result["reason"]


def test_kubernetes_check_is_read_only_and_fixed_to_lab_namespace(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=json.dumps({"items": []}))

    monkeypatch.setattr(k8s_observer.subprocess, "run", fake_run)

    result = system_health._check_kubernetes_lab()

    assert result["status"] == "healthy"
    assert calls == [(
        [
            "kubectl", "--context", "kind-kybernaut", "get", "pods",
            "-n", "kybernaut-lab",
            "-o", "json",
        ],
        {
            "capture_output": True,
            "text": True,
            "timeout": system_health.KUBERNETES_TIMEOUT_SECONDS,
            "check": True,
        },
    )]
    assert not any(
        action in {"apply", "delete", "patch", "edit", "rollout"}
        for action in calls[0][0]
    )


def test_kubernetes_timeout_is_unknown(monkeypatch):
    monkeypatch.setattr(
        system_health,
        "_kubectl_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired("kubectl", 3)
        ),
    )

    result = system_health._check_kubernetes_lab()

    assert result["status"] == "unknown"
    assert "timed out" in result["reason"]


def test_kubernetes_incomplete_result_is_degraded(monkeypatch):
    monkeypatch.setattr(system_health, "_kubectl_json", lambda *_a, **_kw: {})

    assert system_health._check_kubernetes_lab()["status"] == "degraded"


def test_database_check_uses_existing_database_and_read_only_query(
    monkeypatch, tmp_path
):
    database_path = tmp_path / "existing.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE health_probe (value INTEGER)")
    monkeypatch.setenv("KYBERNAUT_DATABASE_PATH", str(database_path))

    result = system_health._check_database()

    assert result["status"] == "healthy"
    assert result["reason"] == "Read-only database query succeeded."


def test_missing_database_is_not_created_by_health_check(monkeypatch, tmp_path):
    database_path = tmp_path / "missing.sqlite"
    monkeypatch.setenv("KYBERNAUT_DATABASE_PATH", str(database_path))

    result = system_health._check_database()

    assert result["status"] == "unhealthy"
    assert not database_path.exists()


@pytest.mark.asyncio
async def test_dependency_failures_are_independent(monkeypatch):
    async def prometheus_unavailable():
        return system_health._result("unknown", "not reachable")

    monkeypatch.setattr(system_health, "check_prometheus", prometheus_unavailable)
    monkeypatch.setattr(
        system_health,
        "_check_kubernetes_lab",
        lambda: system_health._result("healthy", "lab read succeeded"),
    )
    monkeypatch.setattr(
        system_health,
        "_check_database",
        lambda: system_health._result("degraded", "database evidence limited"),
    )

    result = await system_health.check_system_dependencies()

    assert result["prometheus"]["status"] == "unknown"
    assert result["kubernetes_lab"]["status"] == "healthy"
    assert result["database"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_database_check_timeout_is_unknown(monkeypatch):
    async def never_finishes(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(system_health.asyncio, "to_thread", never_finishes)
    monkeypatch.setattr(system_health, "DATABASE_TIMEOUT_SECONDS", 0.001)

    result = await system_health._check_database_bounded()

    assert result["status"] == "unknown"
    assert result["reason"] == "Database connectivity check timed out."


def test_health_endpoint_returns_independent_component_statuses(
    api_client, monkeypatch
):
    import app.main as main

    async def partial_results():
        return {
            "prometheus": system_health._result("unknown", "check timed out"),
            "kubernetes_lab": system_health._result("healthy", "lab read succeeded"),
            "database": system_health._result("unhealthy", "database unavailable"),
        }

    monkeypatch.setattr(main, "check_system_dependencies", partial_results)

    response = api_client.get("/health")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "healthy"
    assert body["components"]["api"]["status"] == "healthy"
    assert body["components"]["prometheus"]["status"] == "unknown"
    assert body["components"]["kubernetes_lab"]["status"] == "healthy"
    assert body["components"]["database"]["status"] == "unhealthy"
    assert all(
        component["checked_at"] and component["reason"]
        for component in body["components"].values()
    )
    assert "check timed out" in body["components"]["prometheus"]["reason"]
