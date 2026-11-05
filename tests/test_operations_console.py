import json
import sqlite3
from datetime import datetime

import pytest
import app.api.operations as operations
import app.database as database
from app.services import audit_repository
from app.services import demo_results
from app.services.weekly_cost_report import WeeklyRunLedger
from scripts.demo_incident import run_demo
from tests.conftest import create_operator_token


@pytest.fixture(autouse=True)
def isolate_normal_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "normal-app.sqlite3")


def auth():
    return {"Authorization": f"Bearer {create_operator_token(permissions=['read'])}"}


def test_console_static_page_and_assets_are_served(api_client):
    page = api_client.get("/console/")
    script = api_client.get("/console/console.js")
    styles = api_client.get("/console/console.css")

    assert page.status_code == script.status_code == styles.status_code == 200
    assert "Operations overview" in page.text
    assert "PERSISTED INCIDENTS" in page.text
    assert "No actions are executed here" in page.text
    assert "localStorage" not in script.text
    assert "sessionStorage" not in script.text
    assert 'method: "GET"' in script.text
    assert "/approvals/" not in script.text
    assert "remediate" not in script.text
    assert "Loading persisted incident history" in script.text
    assert "No persisted incidents are available" in script.text
    assert "Could not load ${name}" in script.text
    assert "Promise.allSettled" in script.text
    assert "authorizationRejected" in script.text
    assert "headers.Authorization" in script.text
    assert "Not estimated" in script.text
    assert "PRICING UNAVAILABLE" in script.text
    assert "realized_savings ?? 0" not in script.text
    assert 'result.reason.status === 404 && name === "cost"' in script.text
    assert "Execution may be interrupted; owner reconciliation required" in script.text
    assert "Read-only reconciliation in progress" in script.text
    assert "not established" in script.text


def test_console_cost_report_requires_authenticated_read_permission(api_client):
    response = api_client.get("/cost/report")

    assert response.status_code == 401


def test_cost_report_returns_latest_weekly_report_and_ledger_delivery(
    api_client, monkeypatch, tmp_path
):
    monkeypatch.setattr(operations, "WEEKLY_COST_REPORT_DIR", tmp_path)
    period = {
        "start": "2026-10-12T00:00:00+00:00",
        "end_exclusive": "2026-10-19T00:00:00+00:00",
    }
    report = {
        "reporting_period": period,
        "provenance": "synthetic console fixture",
        "pricing": {"is_example": True},
        "summary": {"realized_savings": 0.0},
        "recommendations": [],
    }
    (tmp_path / "weekly-sample.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
    ledger = WeeklyRunLedger(tmp_path / "weekly-runs.sqlite3")
    period_key = f"{period['start']}/{period['end_exclusive']}"
    ledger.record(
        period_key,
        json.dumps(period, sort_keys=True),
        "completed",
        "preview_only",
        "console-test-run",
    )

    response = api_client.get("/cost/report", headers=auth())

    assert response.status_code == 200
    assert response.json()["provenance"] == "synthetic console fixture"
    delivery = response.json()["delivery"]
    assert delivery["status"] == "preview_only"
    assert delivery["run_status"] == "completed"
    assert delivery["sent"] is False
    assert delivery["updated_at"]

    ledger.record(
        period_key,
        json.dumps(period, sort_keys=True),
        "completed",
        "sent",
        "console-test-run",
    )
    sent_response = api_client.get("/cost/report", headers=auth())
    assert sent_response.status_code == 200
    assert sent_response.json()["delivery"]["status"] == "sent"
    assert sent_response.json()["delivery"]["sent"] is True


def test_cost_report_falls_back_to_legacy_path(api_client, monkeypatch, tmp_path):
    legacy = tmp_path / "latest.json"
    legacy.write_text('{"legacy": true}', encoding="utf-8")
    monkeypatch.setattr(operations, "WEEKLY_COST_REPORT_DIR", tmp_path / "missing")
    monkeypatch.setattr(operations, "COST_REPORT_PATH", legacy)

    response = api_client.get("/cost/report", headers=auth())

    assert response.status_code == 200
    assert response.json() == {"legacy": True}


def test_malformed_weekly_cost_report_returns_explicit_unavailable(
    api_client, monkeypatch, tmp_path
):
    monkeypatch.setattr(operations, "WEEKLY_COST_REPORT_DIR", tmp_path)
    (tmp_path / "weekly-broken.json").write_text("{", encoding="utf-8")

    response = api_client.get("/cost/report", headers=auth())

    assert response.status_code == 503
    assert response.json()["detail"] == "Latest cost report is unreadable"


def test_demo_results_reads_isolated_demo_incident_approval_and_audit_without_writes(
    api_client, monkeypatch, tmp_path
):
    monkeypatch.setattr(demo_results, "DEMO_DATABASE_DIR", tmp_path / "demos")
    audit_repository.ensure_schema()
    with sqlite3.connect(database.DATABASE_PATH) as connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("incidents", "audit_events", "approval_requests")
        }

    demo = run_demo()
    assert demo["success"] is True
    assert demo["live_runner_calls"] == 0

    listing = api_client.get("/demo/results", headers=auth())
    assert listing.status_code == 200
    assert listing.json()["synthetic"] is True
    assert listing.json()["unavailable_count"] == 0
    assert len(listing.json()["demos"]) == 1
    assert listing.json()["demos"][0]["incident_id"] == demo["incident_id"]

    (tmp_path / "demos" / "demo-ffffffffffff.sqlite3").write_bytes(
        b"invalid demo database"
    )
    partial_listing = api_client.get("/demo/results", headers=auth())
    assert partial_listing.status_code == 200
    assert partial_listing.json()["unavailable_count"] == 1
    assert [item["incident_id"] for item in partial_listing.json()["demos"]] == [
        demo["incident_id"]
    ]

    result = api_client.get(
        f"/demo/results/{demo['incident_id']}", headers=auth()
    )
    assert result.status_code == 200
    data = result.json()
    assert data["synthetic"] is True
    assert data["incident"]["incident_id"] == demo["incident_id"]
    assert data["incident"]["incident_type"] == "memory_leak"
    assert len(data["audit_events"]) == demo["audit"]["event_count"] == 14
    assert len(data["approvals"]) == 1
    assert data["approvals"][0]["incident_ref"] == demo["incident_id"]
    assert data["approvals"][0]["status"] == "APPROVED"
    assert data["approvals"][0]["requested_by"] != data["approvals"][0]["decided_by"]
    assert all(event["incident_ref"] == demo["incident_id"] for event in data["audit_events"])

    with sqlite3.connect(database.DATABASE_PATH) as connection:
        after = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("incidents", "audit_events", "approval_requests")
        }
    assert after == before


def test_demo_results_latest_at_compares_offset_timestamps_chronologically(
    api_client, monkeypatch, tmp_path
):
    root = tmp_path / "demos"
    monkeypatch.setattr(demo_results, "DEMO_DATABASE_DIR", root)
    demo = run_demo()
    earlier_utc = "2026-10-25T12:00:00+02:00"
    later_utc = "2026-10-25T11:00:00+00:00"

    with sqlite3.connect(demo["audit"]["sqlite_path"]) as connection:
        connection.execute(
            "UPDATE audit_events SET timestamp = ?",
            ("2026-10-25T09:00:00+00:00",),
        )
        connection.execute(
            "UPDATE audit_events SET timestamp = ? WHERE id = "
            "(SELECT MIN(id) FROM audit_events)",
            (earlier_utc,),
        )
        connection.execute(
            "UPDATE audit_events SET timestamp = ? WHERE id = "
            "(SELECT MAX(id) FROM audit_events)",
            (later_utc,),
        )
        connection.execute(
            "UPDATE approval_requests SET requested_at = ?",
            ("2026-10-25T08:00:00+00:00",),
        )
        connection.execute(
            "UPDATE incidents SET updated_at = ?",
            ("2026-10-25T08:00:00+00:00",),
        )

    response = api_client.get("/demo/results", headers=auth())

    assert response.status_code == 200
    result = response.json()["demos"][0]
    assert result["latest_at"] == later_utc
    assert (
        datetime.fromisoformat(result["latest_at"])
        > datetime.fromisoformat(earlier_utc)
    )


def test_demo_results_requires_authenticated_read_permission(api_client):
    assert api_client.get("/demo/results").status_code == 401
    assert api_client.get(
        "/demo/results/DEMO-012345abcdef",
        headers={"Authorization": f"Bearer {create_operator_token(permissions=[])}"},
    ).status_code == 403


def test_missing_demo_store_is_empty_and_missing_demo_is_not_found(
    api_client, monkeypatch, tmp_path
):
    monkeypatch.setattr(
        demo_results, "DEMO_DATABASE_DIR", tmp_path / "not-created"
    )
    response = api_client.get("/demo/results", headers=auth())
    assert response.status_code == 200
    assert response.json() == {
        "synthetic": True,
        "demos": [],
        "unavailable_count": 0,
    }
    assert not (tmp_path / "not-created").exists()
    missing = api_client.get(
        "/demo/results/DEMO-012345abcdef", headers=auth()
    )
    assert missing.status_code == 404


def test_malformed_demo_database_fails_safely(api_client, monkeypatch, tmp_path):
    root = tmp_path / "demos"
    root.mkdir()
    (root / "demo-012345abcdef.sqlite3").write_bytes(b"not a sqlite database")
    monkeypatch.setattr(demo_results, "DEMO_DATABASE_DIR", root)

    listing = api_client.get("/demo/results", headers=auth())
    detail = api_client.get(
        "/demo/results/DEMO-012345abcdef", headers=auth()
    )

    assert listing.status_code == 200
    assert listing.json()["demos"] == []
    assert listing.json()["unavailable_count"] == 1
    assert detail.status_code == 503
    assert "demo-012345abcdef" not in listing.text
    assert str(root) not in detail.text


def test_legacy_demo_database_without_incident_table_still_exposes_audit_and_approval(
    api_client, monkeypatch, tmp_path
):
    root = tmp_path / "demos"
    monkeypatch.setattr(demo_results, "DEMO_DATABASE_DIR", root)
    result = run_demo()
    with sqlite3.connect(result["audit"]["sqlite_path"]) as connection:
        connection.execute("DROP TABLE incidents")

    response = api_client.get(
        f"/demo/results/{result['incident_id']}", headers=auth()
    )

    assert response.status_code == 200
    assert response.json()["incident"] is None
    assert len(response.json()["audit_events"]) == 14
    assert len(response.json()["approvals"]) == 1


def test_demo_mode_ui_is_labeled_and_read_only():
    from pathlib import Path

    static_root = Path(__file__).parents[1] / "app" / "static" / "console"
    page = (static_root / "index.html").read_text(encoding="utf-8")
    script = (static_root / "console.js").read_text(encoding="utf-8")

    assert "Synthetic demo results" in page
    assert "SYNTHETIC · READ-ONLY" in page
    assert 'request("/demo/results")' in script
    assert "renderDemoDetail" in script
    assert "/demo/results/${" not in script
    assert 'method: "GET"' in script
    assert "remediate" not in script
