import csv
import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest

import app.services.anomaly_runtime as runtime


def _baseline_text(
    *,
    columns=runtime.EXPECTED_BASELINE_COLUMNS,
    rows_per_service=20,
):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    start = datetime(2026, 10, 21, tzinfo=timezone.utc)
    for index in range(rows_per_service):
        for service in runtime.EXPECTED_SERVICES:
            row = {
                "timestamp": (start + timedelta(seconds=index * 30)).isoformat(),
                "service": service,
                "request_rate": "0.5",
                "p95_latency_seconds": "0.1",
                "memory_bytes": "50000000",
                "cpu_cores_used": "0.01",
            }
            writer.writerow([row.get(column, "extra") for column in columns])
    return output.getvalue()


def _configure_baseline(tmp_path, monkeypatch, text):
    path = tmp_path / runtime.BASELINE_FILENAME
    path.write_text(text, encoding="utf-8", newline="")
    monkeypatch.setattr(runtime, "BASELINE_PATH", path)
    monkeypatch.setattr(
        runtime,
        "EXPECTED_BASELINE_SHA256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    return path


def _duplicate_first_sample():
    rows = list(csv.reader(io.StringIO(_baseline_text())))
    rows[2] = rows[1].copy()
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    return output.getvalue()


def test_valid_baseline_is_accepted(tmp_path, monkeypatch):
    _configure_baseline(tmp_path, monkeypatch, _baseline_text())

    runtime.initialize_anomaly_policies()

    assert set(runtime._policies) == set(runtime.EXPECTED_SERVICES)
    assert runtime._initialization_error is None


def test_checked_in_baseline_has_pinned_integrity_identity(monkeypatch):
    monkeypatch.setattr(
        runtime,
        "BASELINE_PATH",
        runtime.PROJECT_ROOT / "data" / runtime.BASELINE_FILENAME,
    )
    monkeypatch.setattr(
        runtime,
        "EXPECTED_BASELINE_SHA256",
        "777724eb6aae20da2104fef327dd691ab31a542be06f0c45fc5f66d8cfcac1a1",
    )

    runtime.initialize_anomaly_policies()

    assert set(runtime._policies) == set(runtime.EXPECTED_SERVICES)
    assert runtime._initialization_error is None


def test_digest_mismatch_fails_closed(tmp_path, monkeypatch):
    _configure_baseline(tmp_path, monkeypatch, _baseline_text())
    monkeypatch.setattr(runtime, "EXPECTED_BASELINE_SHA256", "0" * 64)

    runtime.initialize_anomaly_policies()

    assert runtime._policies == {}
    assert runtime._initialization_error == "baseline_digest_mismatch"


def test_baseline_version_mismatch_fails_closed(tmp_path, monkeypatch):
    path = tmp_path / "telemetry_baseline_v5.csv"
    path.write_text(_baseline_text(), encoding="utf-8")
    monkeypatch.setattr(runtime, "BASELINE_PATH", path)

    runtime.initialize_anomaly_policies()

    assert runtime._policies == {}
    assert runtime._initialization_error == "baseline_version_mismatch"


@pytest.mark.parametrize(
    ("text_factory", "reason"),
    [
        (
            lambda: _baseline_text(columns=runtime.EXPECTED_BASELINE_COLUMNS[:-1]),
            "baseline_schema_mismatch",
        ),
        (
            lambda: _baseline_text(
                columns=(*runtime.EXPECTED_BASELINE_COLUMNS, "unexpected")
            ),
            "baseline_schema_mismatch",
        ),
        (
            lambda: _baseline_text().replace(
                "order-service", "unknown-service", 1
            ),
            "baseline_unknown_service",
        ),
        (
            lambda: _baseline_text().replace(
                "2026-10-21T00:00:00+00:00,order-service,0.5,0.1,"
                "50000000,0.01",
                "2026-10-21T00:00:00+00:00,order-service,0.5,0.1,"
                "50000000",
                1,
            ),
            "baseline_row_shape_invalid",
        ),
        (
            lambda: _baseline_text().replace(
                "2026-10-21T00:00:00+00:00,order-service,0.5,0.1,"
                "50000000,0.01",
                "2026-10-21T00:00:00+00:00,order-service,0.5,0.1,"
                "50000000,0.01,extra",
                1,
            ),
            "baseline_row_shape_invalid",
        ),
        (
            _duplicate_first_sample,
            "baseline_duplicate_service_timestamp",
        ),
    ],
)
def test_baseline_validation_rejects_invalid_schema_or_service(
    tmp_path, monkeypatch, text_factory, reason
):
    text = text_factory()
    _configure_baseline(tmp_path, monkeypatch, text)

    runtime.initialize_anomaly_policies()

    assert runtime._policies == {}
    assert runtime._initialization_error == reason


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda rows: rows.__setitem__(2, rows[1].copy()),
            "baseline_duplicate_service_timestamp",
        ),
        (
            lambda rows: rows.__setitem__(2, [
                "2026-10-20T23:59:00+00:00", *rows[2][1:]
            ]),
            "baseline_timestamps_unordered",
        ),
        (
            lambda rows: rows.__setitem__(1, ["not-a-timestamp", *rows[1][1:]]),
            "baseline_timestamp_invalid",
        ),
        (
            lambda rows: rows.__setitem__(
                1, ["2026-10-21T00:00:00", *rows[1][1:]]
            ),
            "baseline_timestamp_timezone_missing",
        ),
        (
            lambda rows: rows.__setitem__(1, [*rows[1][:-1], ""]),
            "baseline_feature_invalid",
        ),
        (
            lambda rows: rows.__setitem__(1, [*rows[1][:-1], "nan"]),
            "baseline_feature_non_finite",
        ),
        (
            lambda rows: rows.__setitem__(1, [*rows[1][:-1], "-0.1"]),
            "baseline_feature_negative",
        ),
        (
            lambda rows: rows.__setitem__(1, [*rows[1][:-1], "invalid"]),
            "baseline_feature_invalid",
        ),
    ],
)
def test_baseline_validation_rejects_invalid_rows(
    tmp_path, monkeypatch, mutate, reason
):
    rows = list(csv.reader(io.StringIO(_baseline_text())))
    mutate(rows)
    output = io.StringIO(newline="")
    csv.writer(output).writerows(rows)
    _configure_baseline(tmp_path, monkeypatch, output.getvalue())

    runtime.initialize_anomaly_policies()

    assert runtime._policies == {}
    assert runtime._initialization_error == reason


def test_insufficient_service_coverage_fails_closed(tmp_path, monkeypatch):
    _configure_baseline(
        tmp_path, monkeypatch, _baseline_text(rows_per_service=19)
    )

    runtime.initialize_anomaly_policies()

    assert runtime._policies == {}
    assert (
        runtime._initialization_error
        == "baseline_service_coverage_insufficient"
    )


def test_file_read_failure_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runtime, "BASELINE_PATH", tmp_path / runtime.BASELINE_FILENAME
    )

    runtime.initialize_anomaly_policies()

    assert runtime._policies == {}
    assert runtime._initialization_error == "baseline_read_failed"


@pytest.mark.asyncio
async def test_failed_reinitialization_clears_policies_and_evaluation_is_unavailable(
    tmp_path, monkeypatch
):
    path = _configure_baseline(tmp_path, monkeypatch, _baseline_text())
    runtime.initialize_anomaly_policies()
    assert set(runtime._policies) == set(runtime.EXPECTED_SERVICES)

    path.write_text(_baseline_text().replace("0.5", "0.6", 1), encoding="utf-8")
    runtime.initialize_anomaly_policies()
    result = await runtime.evaluate_service_anomaly("order-service")

    assert runtime._policies == {}
    assert runtime._initialization_error == "baseline_digest_mismatch"
    assert result["status"] == "unavailable"
    assert result["reason"] == "baseline_digest_mismatch"
    assert result["collection_status"] == "not_applicable"
    assert result["evaluation_status"] == "unavailable"
    assert result["evaluation_reason"] == "baseline_digest_mismatch"
