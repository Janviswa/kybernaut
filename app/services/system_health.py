"""Read-only, bounded dependency connectivity checks for the system status API."""

import asyncio
import logging
import sqlite3
import subprocess
from contextlib import closing
from datetime import datetime, timezone

import httpx

from app import database
from app.services.k8s_observer import (
    LAB_CONTEXT,
    LAB_NAMESPACE,
    _kubectl_json,
)
from app.services.prometheus_features import PROMETHEUS_URL

logger = logging.getLogger(__name__)
PROMETHEUS_TIMEOUT_SECONDS = 3.0
KUBERNETES_TIMEOUT_SECONDS = 3
DATABASE_TIMEOUT_SECONDS = 3


def _result(status: str, reason: str) -> dict:
    return {
        "status": status,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }


async def check_prometheus() -> dict:
    try:
        async with httpx.AsyncClient(
            timeout=PROMETHEUS_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(f"{PROMETHEUS_URL}/-/healthy")
    except httpx.TimeoutException:
        return _result("unknown", "Prometheus health check timed out.")
    except httpx.HTTPError as exc:
        logger.warning(
            "Prometheus connectivity check failed (exception_type=%s)",
            type(exc).__name__,
        )
        return _result("unknown", "Prometheus connectivity could not be established.")
    except Exception as exc:
        logger.warning(
            "Prometheus connectivity check failed (exception_type=%s)",
            type(exc).__name__,
        )
        return _result("unknown", "Prometheus connectivity could not be established.")

    if 200 <= response.status_code < 300:
        return _result("healthy", "Prometheus health endpoint responded.")
    if response.status_code >= 500:
        return _result("unhealthy", "Prometheus health endpoint reported an error.")
    return _result(
        "degraded",
        "Prometheus is reachable but its health endpoint did not return success.",
    )


def _check_kubernetes_lab() -> dict:
    try:
        pods = _kubectl_json(
            "pods",
            timeout=KUBERNETES_TIMEOUT_SECONDS,
            context=LAB_CONTEXT,
        )
    except subprocess.TimeoutExpired:
        return _result("unknown", "Lab namespace access check timed out.")
    except subprocess.CalledProcessError:
        return _result(
            "unhealthy",
            "Lab namespace is unavailable or read access was denied.",
        )
    except Exception as exc:
        logger.warning(
            "Kubernetes lab connectivity check failed (exception_type=%s)",
            type(exc).__name__,
        )
        return _result("unknown", "Lab namespace connectivity could not be established.")

    items = pods.get("items") if isinstance(pods, dict) else None
    if not isinstance(items, list):
        return _result(
            "degraded",
            "Lab namespace responded with an incomplete read-only result.",
        )
    return _result(
        "healthy",
        f"Read access to {LAB_NAMESPACE} was confirmed.",
    )


def _check_database() -> dict:
    try:
        if not database.get_database_path().is_file():
            return _result("unhealthy", "Configured database file is unavailable.")
        with closing(database.get_connection()) as connection:
            row = connection.execute("SELECT 1").fetchone()
        if row is None or row[0] != 1:
            return _result("unhealthy", "Database connectivity query failed.")
    except sqlite3.Error as exc:
        logger.warning(
            "Database connectivity check failed (exception_type=%s)",
            type(exc).__name__,
        )
        return _result("unhealthy", "Database connectivity query failed.")
    except Exception as exc:
        logger.warning(
            "Database connectivity check failed (exception_type=%s)",
            type(exc).__name__,
        )
        return _result("unknown", "Database connectivity could not be established.")
    return _result("healthy", "Read-only database query succeeded.")


async def _check_database_bounded() -> dict:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_check_database),
            timeout=DATABASE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return _result("unknown", "Database connectivity check timed out.")


async def check_system_dependencies() -> dict:
    prometheus, kubernetes, database_status = await asyncio.gather(
        check_prometheus(),
        asyncio.to_thread(_check_kubernetes_lab),
        _check_database_bounded(),
    )
    return {
        "prometheus": prometheus,
        "kubernetes_lab": kubernetes,
        "database": database_status,
    }
