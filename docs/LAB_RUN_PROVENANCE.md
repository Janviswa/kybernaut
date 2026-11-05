# Lab-run provenance persistence

This module provides opt-in persistence for future lab-run provenance. It is
not initialized by application startup and has no default database path.
`LabRunRepository` requires an explicitly supplied SQLite path and explicit
schema initialization. Its default authorizer denies run creation,
finalization, and occurrence association. No production, ordinary-monitoring,
or live-lab caller is wired to this repository.

## Data model

- `lab_runs` stores a stable run ID, declared environment, UTC run bounds,
  capture method, optional source revision, optional artifact SHA-256 or
  controlled evidence reference, lifecycle state, and actor metadata.
- `lab_run_scenarios` allows a run to identify multiple scenarios.
- `lab_run_occurrences` associates a run-scoped event key with an incident
  summary row. It does not assert that the summary itself originated in that
  run.
- `lab_provenance_schema_version` records the opt-in schema version; it does
  not alter the application's existing schema initialization.

Historical incident rows are not backfilled. Existing incident summaries
remain unlinked unless a future, separately authorized lab operation creates
an explicit occurrence association. Ordinary Prometheus, telemetry, and
Kubernetes persistence continues using the existing incident repository and
does not create lab-run links.

## Lifecycle and atomicity

Runs begin as `running` and may be finalized once as `completed` or `aborted`.
Run creation and finalization require an injected authorizer; because no
project authorization mechanism or roles have been approved, the default
authorizer rejects these operations. The repository's `record_occurrence`
method is unconditionally blocked with `LabRunStateError`, even if an
allowing authorizer is injected. It performs no incident write and creates no
association. Occurrence-to-summary identity remains unresolved and must be
approved before any application path can record associations.

The run interval is inclusive at both ends. `observed_at` is when the
observation was recorded and must fall within the run interval.
`source_timestamp`, when supplied, means the source event time and must also
fall within that interval. A missing source timestamp remains missing; it is
never replaced with observation time. The repository normalizes both to
fixed-width UTC timestamps so the database bounds triggers can compare them.

The occurrence schema retains run-scoped event keys and caller-provided
digests for a possible later design, but the repository does not currently
write occurrence associations. Event-key generation, retry identity, and
canonical digest input remain unresolved. Ordinary incident summary
deduplication is unchanged and is not a provenance identity policy.

Completed runs without an artifact digest or controlled evidence reference
are marked `ineligible_missing_evidence` for later provenance review. A
locator makes a run `eligible_for_later_review`, not verified: a digest alone
does not authenticate the artifact or establish its origin.

Schema creation is an explicit operation and is never part of
`initialize_database()` or application startup. Tests use temporary SQLite
files only. Do not call the schema initializer on an existing or deployed
database without separate migration authorization and an approved backup and
rollback procedure. Schema initialization itself also requires an injected
authorizer; the default rejects it. Before accepting a version marker, the
initializer checks the complete set of expected provenance tables and
triggers and compares their stored definitions. A partial, changed, or
unknown schema is rejected without repair. The existing `incidents` table
must expose exactly one primary-key column named `id`, declared `INTEGER`,
consistent with SQLite's rowid primary-key shape. `TEXT PRIMARY KEY`,
composite keys, and missing primary keys are rejected. This is an explicit
schema compatibility check; initialization does not alter the incident
table.

## Verified guarantees and limitations

- Repository/API validation requires timezone-aware timestamps and normalizes
them to fixed-width UTC with microsecond precision. The database schema also
checks timestamp shape, calendar day including leap years, and clock-field
  bounds for run start/completion, manifest creation, and occurrence time
columns. Only validated normalized UTC values are used for lexicographic
inclusive run-bound comparisons. Direct-SQL tests cover invalid dates and
boundary behavior.
- Database triggers enforce initial `running` state, one terminal transition,
scenario and occurrence immutability, deletion guards, evidence-locator
eligibility, and completed-run/time-bound association conditions.
- Foreign keys are enabled and verified on every connection created by
`LabRunRepository` and by the central application connection factory.
Direct SQLite connections outside those helpers must enable and verify
foreign keys independently.
- Direct SQLite writers can create and finalize structurally valid runs with
fabricated actor metadata. Triggers are integrity constraints, not an
authorization system; file-level write access remains a separate trust
boundary. The default repository authorizer denies operations, but an
injected permissive authorizer can enable them.
- The repository API blocks occurrence recording regardless of the injected
authorizer. This does not authenticate or prevent a direct SQLite writer from
inserting an association that satisfies schema constraints; such access
remains outside the API authorization boundary and is not an approved
integration mechanism.
- Digests only identify caller-provided values relative to a trusted digest;
they do not authenticate an artifact, capture environment, operator, or
scenario. Evidence references are opaque locators and are not fetched or
validated by this module.
- The initializer checks the incident key columns needed by its foreign key:
exactly one primary-key column, named `id`, with declared type `INTEGER`.
It does not require `AUTOINCREMENT` or compare unrelated `incidents` DDL.
- SQLite deployment-version support, migration execution, backup/restore,
rollback, and operational ownership have not been established. No real
database migration is authorized by this design.

## Unresolved owner decisions

Occurrence-to-summary identity, authorization roles and workflow,
evidence access and retention, event-key generation and canonical digest
input, clock-source trust and acceptable skew, supported SQLite deployment
versions, migration/backup/restore/rollback procedures, and operational
ownership remain unresolved. Occurrence recording and all application
integration must remain blocked until the identity design is separately
approved. No production migration, dataset creation, label adjudication,
benchmark evaluation, or operational qualification is authorized by this
isolated implementation.
