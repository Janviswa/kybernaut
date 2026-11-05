import hashlib
import sqlite3

import pytest

import app.database as database
from app.services import lab_run_repository
from app.services.incident_repository import (
    list_incidents,
    record_incident,
)
from app.services.lab_run_repository import (
    LabRunAuthorizationError,
    LabRunRepository,
    LabRunSchemaError,
    LabRunStateError,
)


class AllowTestAuthorization:
    def authorize(self, *, action, actor_id, lab_run_id):
        return True


@pytest.fixture
def repository(tmp_path, monkeypatch):
    path = tmp_path / "isolated-provenance.sqlite3"
    monkeypatch.delenv("KYBERNAUT_DATABASE_PATH", raising=False)
    monkeypatch.setattr(database, "DATABASE_PATH", path)
    database.initialize_database()
    repo = LabRunRepository(path, authorizer=AllowTestAuthorization())
    repo.initialize_schema(actor_id="test-owner")
    return repo


def _create_run(
    repo,
    *,
    run_id="lab-run-1",
    artifact_sha256=hashlib.sha256(b"controlled test artifact").hexdigest(),
):
    return repo.create_run(
        actor_id="test-owner",
        lab_run_id=run_id,
        environment_id="isolated-lab",
        scenario_ids=["scenario-a", "scenario-b"],
        started_at="2026-10-26T10:00:00+05:30",
        capture_method="test-fixture",
        source_revision="abc123",
        artifact_sha256=artifact_sha256,
    )


def _complete_run(repo, *, run_id="lab-run-1", status="completed"):
    if not any(
        run["lab_run_id"] == run_id
        for run in _runs(repo)
    ):
        _create_run(repo, run_id=run_id)
    return repo.finalize_run(
        actor_id="test-owner",
        lab_run_id=run_id,
        status=status,
        completed_at="2026-10-26T05:00:00Z",
    )


def _runs(repo):
    with repo._connection() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM lab_runs ORDER BY lab_run_id"
            )
        ]


def _incident(message="service is unhealthy"):
    return {
        "incident_id": "inc-lab-test",
        "service": "order-service",
        "type": "service_unavailable",
        "severity": "critical",
        "message": message,
        "fingerprint": "prometheus:order-service:service_unavailable",
    }


def _record(repo, *, event_key="event-1", event_digest=None, incident=None):
    return repo.record_occurrence(
        actor_id="test-owner",
        lab_run_id="lab-run-1",
        event_key=event_key,
        event_digest=event_digest or hashlib.sha256(b"event-1").hexdigest(),
        incident=incident or _incident(),
        observed_at="2026-10-26T04:59:00Z",
        source_timestamp="2026-10-26T10:30:00+05:30",
    )


def test_schema_and_manifest_creation_allow_multiple_scenarios(repository):
    run = _create_run(repository)

    assert run["status"] == "running"
    assert run["created_by"] == "test-owner"
    assert run["started_at_utc"] == "2026-10-26T04:30:00.000000Z"
    assert run["scenario_ids"] == ["scenario-a", "scenario-b"]


def test_provenance_schema_initialization_is_versioned_and_idempotent(
    repository,
):
    repository.initialize_schema(actor_id="test-owner")

    with repository._connection() as connection:
        versions = connection.execute(
            "SELECT schema_version FROM lab_provenance_schema_version"
        ).fetchall()

    assert [row["schema_version"] for row in versions] == [1]


def test_duplicate_run_id_is_rejected(repository):
    _create_run(repository)

    with pytest.raises(sqlite3.IntegrityError):
        _create_run(repository)


@pytest.mark.parametrize(
    ("status", "expected"),
    [("completed", "completed"), ("aborted", "aborted")],
)
def test_running_run_can_be_finalized_once(repository, status, expected):
    _create_run(repository)

    run = _complete_run(repository, status=status)

    assert run["status"] == expected
    assert run["finalized_by"] == "test-owner"
    assert run["completed_at_utc"] == "2026-10-26T05:00:00.000000Z"
    with pytest.raises(LabRunStateError):
        repository.finalize_run(
            actor_id="test-owner",
            lab_run_id="lab-run-1",
            status="completed",
            completed_at="2026-10-26T05:01:00Z",
        )


def test_default_authorizer_fails_closed_before_database_access(tmp_path):
    path = tmp_path / "not-created.sqlite3"
    repo = LabRunRepository(path)

    calls = (
        lambda: repo.initialize_schema(actor_id="operator"),
        lambda: repo.create_run(
            actor_id="operator",
            lab_run_id="lab-run-1",
            environment_id="lab",
            scenario_ids=["scenario-a"],
            started_at="2026-10-26T00:00:00Z",
            capture_method="fixture",
        ),
        lambda: repo.finalize_run(
            actor_id="operator",
            lab_run_id="lab-run-1",
            status="completed",
            completed_at="2026-10-26T00:01:00Z",
        ),
    )
    for operation in calls:
        with pytest.raises(LabRunAuthorizationError):
            operation()

    assert not path.exists()


def test_occurrence_recording_is_blocked_for_all_authorizers(repository):
    denied = LabRunRepository(repository.database_path)

    with pytest.raises(LabRunAuthorizationError):
        denied.finalize_run(
            actor_id="operator",
            lab_run_id="lab-run-1",
            status="completed",
            completed_at="2026-10-26T05:00:00Z",
        )
    with pytest.raises(LabRunStateError, match="disabled pending"):
        _record(denied)

    with pytest.raises(LabRunStateError, match="disabled pending"):
        _record(repository)
    _create_run(repository)
    _complete_run(repository)
    allow_all_repository = LabRunRepository(
        repository.database_path,
        authorizer=AllowTestAuthorization(),
    )
    first = record_incident(_incident())
    with pytest.raises(LabRunStateError, match="disabled pending"):
        _record(allow_all_repository, incident=_incident("must not link"))

    incidents = list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["id"] == first["id"]
    assert incidents[0]["message"] == "service is unhealthy"
    with repository._connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM lab_run_occurrences"
        ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-26T05:00:00",
        "not-a-time",
    ],
)
def test_naive_or_invalid_timestamps_are_rejected(repository, timestamp):
    with pytest.raises(ValueError):
        repository.create_run(
            actor_id="test-owner",
            lab_run_id="bad-time",
            environment_id="lab",
            scenario_ids=["scenario-a"],
            started_at=timestamp,
            capture_method="fixture",
        )


def test_completion_before_start_is_rejected(repository):
    _create_run(repository)
    with pytest.raises(ValueError, match="must not precede"):
        repository.finalize_run(
            actor_id="test-owner",
            lab_run_id="lab-run-1",
            status="completed",
            completed_at="2026-10-26T04:29:59Z",
        )
    assert _runs(repository)[0]["status"] == "running"


def test_completed_run_without_locator_is_not_review_eligible(repository):
    _create_run(repository, artifact_sha256=None)
    _complete_run(repository)

    assert repository.provenance_review_eligibility(
        actor_id="test-owner",
        lab_run_id="lab-run-1",
    ) == "ineligible_missing_evidence"
    with pytest.raises(LabRunStateError, match="disabled pending"):
        _record(repository)


def test_completed_run_with_locator_is_only_eligible_for_later_review(
    repository,
):
    digest = hashlib.sha256(b"controlled artifact").hexdigest()
    _create_run(repository, artifact_sha256=digest)
    _complete_run(repository)

    assert repository.provenance_review_eligibility(
        actor_id="test-owner",
        lab_run_id="lab-run-1",
    ) == "eligible_for_later_review"


def test_controlled_evidence_reference_is_a_review_locator(repository):
    repository.create_run(
        actor_id="test-owner",
        lab_run_id="reference-run",
        environment_id="isolated-lab",
        scenario_ids=["scenario-a"],
        started_at="2026-10-26T04:30:00Z",
        capture_method="test-fixture",
        evidence_ref="controlled/artifact-123",
    )
    repository.finalize_run(
        actor_id="test-owner",
        lab_run_id="reference-run",
        status="completed",
        completed_at="2026-10-26T05:00:00Z",
    )

    assert repository.provenance_review_eligibility(
        actor_id="test-owner",
        lab_run_id="reference-run",
    ) == "eligible_for_later_review"


def test_provenance_review_eligibility_requires_authorization(repository):
    denied = LabRunRepository(repository.database_path)
    _create_run(repository)
    _complete_run(repository)

    with pytest.raises(LabRunAuthorizationError):
        denied.provenance_review_eligibility(
            actor_id="operator",
            lab_run_id="lab-run-1",
        )


def test_each_repository_connection_enforces_foreign_keys(repository):
    for _ in range(2):
        with repository._connection() as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    _complete_run(repository)
    with repository._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO lab_run_occurrences (
                    lab_run_id, event_key, event_digest, incident_row_id,
                    observed_at_utc
                )
                VALUES (
                    'lab-run-1', 'event-x', ?, 999,
                    '2026-10-26T04:30:00.000000Z'
                )
                """,
                (hashlib.sha256(b"x").hexdigest(),),
            )


def test_legacy_incidents_remain_unlinked(repository):
    legacy = record_incident(_incident("legacy summary"))
    _complete_run(repository)

    with repository._connection() as connection:
        linked = connection.execute(
            """
            SELECT COUNT(*) FROM lab_run_occurrences
            WHERE incident_row_id = ?
            """,
            (legacy["id"],),
        ).fetchone()[0]

    assert linked == 0


def test_legacy_schema_fixture_is_preserved_by_opt_in_schema_creation(tmp_path):
    path = tmp_path / "legacy-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE incidents (
                id INTEGER PRIMARY KEY,
                incident_id TEXT NOT NULL,
                service TEXT NOT NULL,
                incident_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO incidents VALUES (
                1, 'fixture-incident', 'fixture-service',
                'fixture-type', 'low', 'synthetic fixture',
                'OPEN', '2026-01-01T00:00:00Z',
                '2026-01-01T00:00:00Z'
            )
            """
        )

    repo = LabRunRepository(path, authorizer=AllowTestAuthorization())
    repo.initialize_schema(actor_id="test-owner")

    with repo._connection() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(incidents)")
        }
        count = connection.execute(
            "SELECT COUNT(*) FROM incidents"
        ).fetchone()[0]
        associations = connection.execute(
            "SELECT COUNT(*) FROM lab_run_occurrences"
        ).fetchone()[0]

    assert "lab_run_id" not in columns
    assert count == 1
    assert associations == 0


def test_ordinary_incident_persistence_does_not_create_lab_link(repository):
    ordinary = record_incident(_incident())

    with repository._connection() as connection:
        linked = connection.execute(
            "SELECT COUNT(*) FROM lab_run_occurrences"
        ).fetchone()[0]

    assert ordinary["id"] is not None
    assert linked == 0


def test_schema_initialization_rejects_database_without_incidents(tmp_path):
    path = tmp_path / "uninitialized.sqlite3"
    sqlite3.connect(path).close()
    repo = LabRunRepository(path, authorizer=AllowTestAuthorization())

    with pytest.raises(lab_run_repository.LabRunSchemaError):
        repo.initialize_schema(actor_id="test-owner")


@pytest.mark.parametrize(
    ("schema", "expected_error"),
    [
        ("CREATE TABLE incidents (id INTEGER)", "single INTEGER PRIMARY KEY"),
        (
            "CREATE TABLE incidents (id TEXT PRIMARY KEY)",
            "single INTEGER PRIMARY KEY",
        ),
        (
            "CREATE TABLE incidents (id INTEGER, other TEXT, "
            "PRIMARY KEY (id, other))",
            "single INTEGER PRIMARY KEY",
        ),
    ],
)
def test_schema_initialization_rejects_incompatible_incident_id(
    tmp_path, schema, expected_error
):
    path = tmp_path / "incompatible-incident-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(schema)

    repo = LabRunRepository(path, authorizer=AllowTestAuthorization())
    with pytest.raises(
        lab_run_repository.LabRunSchemaError,
        match=expected_error,
    ):
        repo.initialize_schema(actor_id="test-owner")


def test_schema_initialization_accepts_single_integer_primary_key(tmp_path):
    path = tmp_path / "compatible-incident-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY, message TEXT)"
        )

    repo = LabRunRepository(path, authorizer=AllowTestAuthorization())
    repo.initialize_schema(actor_id="test-owner")

    with repo._connection() as connection:
        assert connection.execute(
            "SELECT schema_version FROM lab_provenance_schema_version"
        ).fetchone()[0] == 1


def test_partial_schema_with_missing_columns_is_rejected(tmp_path):
    path = tmp_path / "partial-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY)"
        )
        connection.execute(
            "CREATE TABLE lab_runs (lab_run_id TEXT PRIMARY KEY)"
        )

    repo = LabRunRepository(path, authorizer=AllowTestAuthorization())
    with pytest.raises(LabRunSchemaError, match="partial or incompatible"):
        repo.initialize_schema(actor_id="test-owner")


def test_existing_schema_with_incompatible_constraint_is_rejected(tmp_path):
    path = tmp_path / "incompatible-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE incidents (id INTEGER PRIMARY KEY)"
        )
        statements = list(
            lab_run_repository._SCHEMA_STATEMENTS
        )
        run_statement_index = next(
            index for index, statement in enumerate(statements)
            if "CREATE TABLE lab_runs" in statement
        )
        incompatible = statements[run_statement_index].replace(
            "CHECK (status IN ('running', 'completed', 'aborted'))",
            "CHECK (status IN ('running'))",
        )
        assert incompatible != statements[run_statement_index]
        statements[run_statement_index] = incompatible
        for statement in statements:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO lab_provenance_schema_version
                (singleton, schema_version)
            VALUES (1, 1)
            """
        )

    repo = LabRunRepository(path, authorizer=AllowTestAuthorization())
    with pytest.raises(LabRunSchemaError, match="Incompatible"):
        repo.initialize_schema(actor_id="test-owner")


def test_mismatched_schema_version_is_rejected_without_repair(repository):
    with repository._connection() as connection, connection:
        connection.execute(
            """
            UPDATE lab_provenance_schema_version
            SET schema_version = 2 WHERE singleton = 1
            """
        )

    with pytest.raises(LabRunSchemaError, match="Unsupported"):
        repository.initialize_schema(actor_id="test-owner")


def test_direct_sql_cannot_associate_occurrence_to_running_run(repository):
    _create_run(repository)
    with repository._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="completed lab run"):
            connection.execute(
                """
                INSERT INTO lab_run_occurrences (
                    lab_run_id, event_key, event_digest, incident_row_id,
                    observed_at_utc
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "lab-run-1",
                    "direct-sql-event",
                    hashlib.sha256(b"event").hexdigest(),
                    1,
                    "2026-10-26T04:30:00.000000Z",
                ),
            )


def test_direct_sql_timestamp_checks_reject_impossible_calendar_values(
    repository,
):
    invalid_values = (
        "2026-02-30T04:30:00.000000Z",
        "2026-04-31T04:30:00.000000Z",
        "2025-02-29T04:30:00.000000Z",
        "2026-10-26T24:00:00.000000Z",
    )
    for timestamp in invalid_values:
        with repository._connection() as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO lab_runs (
                        lab_run_id, environment_id, started_at_utc,
                        capture_method, status, created_by, created_at_utc
                    )
                    VALUES (?, 'isolated-lab', ?, 'fixture', 'running',
                            'test-owner', '2026-10-26T04:30:00.000000Z')
                    """,
                    (f"invalid-{timestamp[:10]}", timestamp),
                )


@pytest.mark.parametrize(
    "created_at",
    [
        "not-a-timestamp",
        "2026-10-26",
        "2026-10-26T00:00:00Z",
        "2026-10-26T00:00:00.000000+00:00",
        "2026-02-30T00:00:00.000000Z",
        "2025-02-29T00:00:00.000000Z",
        "2026-13-01T00:00:00.000000Z",
        "2026-04-31T00:00:00.000000Z",
        "2026-10-26T24:00:00.000000Z",
        "2026-10-26T00:60:00.000000Z",
        "2026-10-26T00:00:60.000000Z",
    ],
)
def test_direct_sql_created_timestamp_rejects_invalid_values(
    repository, created_at
):
    with repository._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO lab_runs (
                    lab_run_id, environment_id, started_at_utc,
                    capture_method, status, created_by, created_at_utc
                )
                VALUES (?, 'isolated-lab',
                        '2026-10-26T04:30:00.000000Z',
                        'fixture', 'running', 'test-owner', ?)
                """,
                (f"created-at-{created_at[:10]}", created_at),
            )


def test_direct_sql_created_timestamp_accepts_valid_utc(repository):
    with repository._connection() as connection:
        connection.execute(
            """
            INSERT INTO lab_runs (
                lab_run_id, environment_id, started_at_utc,
                capture_method, status, created_by, created_at_utc
            )
            VALUES (
                'valid-created-at', 'isolated-lab',
                '2024-02-29T00:00:00.000000Z',
                'fixture', 'running', 'test-owner',
                '2024-02-29T00:00:00.000000Z'
            )
            """
        )


def test_direct_sql_occurrence_timestamp_checks_reject_impossible_dates(
    repository,
):
    incident = record_incident(_incident("synthetic timestamp fixture"))
    _complete_run(repository)

    with repository._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO lab_run_occurrences (
                    lab_run_id, event_key, event_digest, incident_row_id,
                    source_timestamp_utc, observed_at_utc
                )
                VALUES (?, ?, ?, ?, NULL, ?)
                """,
                (
                    "lab-run-1",
                    "invalid-date",
                    hashlib.sha256(b"invalid-date").hexdigest(),
                    incident["id"],
                    "2026-02-30T04:30:00.000000Z",
                ),
            )
    incidents = list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["id"] == incident["id"]


def test_direct_sql_allows_inclusive_occurrence_boundaries(repository):
    incident = record_incident(_incident("synthetic boundary fixture"))
    _complete_run(repository)

    with repository._connection() as connection, connection:
        for key, timestamp in (
            ("start-boundary", "2026-10-26T04:30:00.000000Z"),
            ("end-boundary", "2026-10-26T05:00:00.000000Z"),
        ):
            connection.execute(
                """
                INSERT INTO lab_run_occurrences (
                    lab_run_id, event_key, event_digest, incident_row_id,
                    source_timestamp_utc, observed_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "lab-run-1",
                    key,
                    hashlib.sha256(key.encode()).hexdigest(),
                    incident["id"],
                    timestamp,
                    timestamp,
                ),
            )
        assert connection.execute(
            "SELECT COUNT(*) FROM lab_run_occurrences"
        ).fetchone()[0] == 2


def test_direct_sql_triggers_restrict_lifecycle_and_guard_immutable_rows(
    repository,
):
    _create_run(repository)

    with repository._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="invalid lab run"):
            connection.execute(
                "UPDATE lab_runs SET status = 'running' "
                "WHERE lab_run_id = 'lab-run-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="invalid lab run"):
            connection.execute(
                "UPDATE lab_runs SET environment_id = 'changed' "
                "WHERE lab_run_id = 'lab-run-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE lab_run_scenarios SET scenario_id = 'changed' "
                "WHERE lab_run_id = 'lab-run-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM lab_run_scenarios "
                "WHERE lab_run_id = 'lab-run-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM lab_runs WHERE lab_run_id = 'lab-run-1'"
            )

    _complete_run(repository)
    with repository._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="invalid lab run"):
            connection.execute(
                "UPDATE lab_runs SET status = 'aborted' "
                "WHERE lab_run_id = 'lab-run-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="running runs"):
            connection.execute(
                """
                INSERT INTO lab_run_scenarios (lab_run_id, scenario_id)
                VALUES ('lab-run-1', 'scenario-new')
                """
            )


def test_direct_sql_occurrence_rows_are_immutable_and_evidence_gated(
    repository,
):
    incident = record_incident(_incident("synthetic direct SQL fixture"))
    _create_run(repository)
    _complete_run(repository)

    with repository._connection() as connection, connection:
        connection.execute(
            """
            INSERT INTO lab_run_occurrences (
                lab_run_id, event_key, event_digest, incident_row_id,
                observed_at_utc
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "lab-run-1",
                "direct-sql",
                hashlib.sha256(b"direct-sql").hexdigest(),
                incident["id"],
                "2026-10-26T04:30:00.000000Z",
            ),
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE lab_run_occurrences SET event_key = 'changed' "
                "WHERE event_key = 'direct-sql'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "DELETE FROM lab_run_occurrences WHERE event_key = 'direct-sql'"
            )

    _create_run(repository, run_id="no-evidence", artifact_sha256=None)
    _complete_run(repository, run_id="no-evidence")
    with repository._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO lab_run_occurrences (
                    lab_run_id, event_key, event_digest, incident_row_id,
                    observed_at_utc
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "no-evidence",
                    "evidence-free",
                    hashlib.sha256(b"evidence-free").hexdigest(),
                    incident["id"],
                    "2026-10-26T04:30:00.000000Z",
                ),
            )


@pytest.mark.parametrize(
    ("status", "artifact_sha256", "observed_at"),
    [
        ("aborted", hashlib.sha256(b"artifact").hexdigest(),
         "2026-10-26T04:30:00.000000Z"),
        ("completed", None, "2026-10-26T04:30:00.000000Z"),
        ("completed", hashlib.sha256(b"artifact").hexdigest(),
         "2026-10-26T05:00:00.000001Z"),
    ],
)
def test_direct_sql_cannot_associate_ineligible_or_out_of_bounds_occurrence(
    repository, status, artifact_sha256, observed_at
):
    _create_run(repository, artifact_sha256=artifact_sha256)
    _complete_run(repository, status=status)

    with repository._connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO lab_run_occurrences (
                    lab_run_id, event_key, event_digest, incident_row_id,
                    observed_at_utc
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "lab-run-1",
                    "direct-sql-event",
                    hashlib.sha256(b"event").hexdigest(),
                    1,
                    observed_at,
                ),
            )
