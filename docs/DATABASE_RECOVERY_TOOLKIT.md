# Disposable SQLite Recovery Toolkit

The recovery utility in `scripts/database_recovery.py` is an engineering
tool for **disposable databases beneath the system temporary directory**.
It is not a production backup system. It does not select a production source,
destination, schedule, retention policy, encryption method, or owner.

The repository does not establish the effective production database path,
application process topology, or filesystem. Confirm those facts and obtain
separate operational approval before designing production use. The utility
refuses the repository-default database and the database path supplied in its
current process environment, but it does not load or infer the API's `.env`
configuration and cannot discover a separate service's private configuration.
Its temporary-directory restriction is an additional safety boundary, not
proof of production configuration.

## Commands

Create the source as a synthetic test database under the temporary directory.
Its parent directory must already exist. Then explicitly authorize the backup:

```bash
mkdir -p /tmp/kybernaut-recovery-test
python scripts/database_recovery.py backup \
  --source /tmp/kybernaut-recovery-test/source.sqlite3 \
  --destination /tmp/kybernaut-recovery-test/snapshot.sqlite3 \
  --confirm-disposable-source \
  --create-backup
```

The source and destination are required arguments. Both confirmations are
required. The destination and its `.metadata.json` sidecar must not already
exist; neither is overwritten. The backup uses Python's SQLite online backup
API, which captures a consistent SQLite snapshot while a database connection
may be open and accounts for SQLite-managed journal/WAL state. The tool does
not change journal mode or durability settings.
The versioned sidecar contains the capture timestamp, SQLite version,
repository HEAD revision when available, selected backup filename, capture
method, and SHA-256 digest of the exact snapshot bytes; it contains no
database path or row data. Validation and restore require a supported sidecar
whose filename and digest match the selected snapshot. Legacy snapshots
without this metadata are rejected; no implicit legacy acceptance or
migration path is provided.

Validate without starting the application:

```bash
python scripts/database_recovery.py validate \
  --database /tmp/kybernaut-recovery-test/snapshot.sqlite3
```

Validation opens the database read-only. It runs `PRAGMA integrity_check`,
lists table names and aggregate row counts, reports declared foreign-key
check results, and checks supported lifecycle links. Its JSON result contains
identifiers/findings and counts, not incident or audit row payloads. A failed
integrity, foreign-key, or lifecycle validation exits nonzero. Lifecycle
results include required tables and checks that are missing or unavailable;
an empty or unsupported schema is not considered valid merely because its
SQLite structure is sound.

Materialize a snapshot at a fresh temporary path:

```bash
python scripts/database_recovery.py restore \
  --backup /tmp/kybernaut-recovery-test/snapshot.sqlite3 \
  --destination /tmp/kybernaut-recovery-test/restored.sqlite3 \
  --confirm-disposable-destination
```

Restore uses SQLite's online backup API to create a new database file; it
does not overwrite an existing destination, alter the selected backup, start
Kybernaut, or invoke remediation. It publishes a matching metadata sidecar
for the restored snapshot. Validate the restored file with the `validate`
command separately. Parent directories must already exist.

## Operation effects

- `validate` is read-only and opens its selected database in SQLite read-only
  mode after validating the required metadata sidecar and snapshot digest.
- `backup` writes a new snapshot and provenance sidecar at the explicit
  destination. It does not alter the source and refuses existing files.
- `restore` writes a new database file at the explicit destination. It does
  not alter the backup and refuses existing database or metadata files.
- The utility has no delete, repair, migration, application-start, or
  cleanup command. Test-owned temporary files must be managed separately by
  the disposable test environment.

## What validation proves—and does not prove

- `integrity_check` evaluates SQLite's internal database structure. It does
  not establish that application data is semantically correct.
- Foreign-key checking is reported as not applicable when the database has no
  declared foreign keys. It cannot validate relationships that the schema
  does not express.
- Lifecycle checks inspect links among incidents, handoffs, workflows,
  action claims, approvals, and audit events when the necessary tables and
  columns exist. Required lifecycle tables/checks and unavailable checks are
  listed in the structured result. Missing coverage makes `valid` false.
- Verified action records are checked for live execution and successful
  recovery evidence. Incidents marked resolved specifically for verified
  Kubernetes recovery are checked for a linked verified action and matching
  recovery audit event.
- Executing, execution-unknown, workflow-reconciliation, and dispatching
  states are counted and reported as unresolved. Validation never changes
  state, retries work, or infers successful recovery from a generic completed
  status.
- Schema/table counts and lifecycle checks are read-only. They do not start
  application initialization or apply migrations.
- The separate legacy JSONL remediation audit file is not part of the SQLite
  snapshot. If it remains operationally relevant, it needs a separately
  approved capture, retention, access-control, and recovery procedure.

The tool rejects paths outside the system temporary directory, symlinks,
known active application database paths, missing sources, and existing
destinations. Validation and restore fail closed on missing, malformed,
unsupported, or digest-mismatched metadata. A SHA-256 digest binds metadata
to the snapshot bytes but is not a signature and does not prove who created
either artifact. The pair must still be protected from unauthorized
replacement. It cannot prove that a caller-selected temporary database is
synthetic; the explicit confirmations remain required. Treat all test data and
the generated backup as potentially sensitive and clean them up only through
the test environment's approved disposable-workspace lifecycle.

Backup snapshot and sidecar publication are separate filesystem operations.
If sidecar publication fails, the tool attempts to remove only the snapshot
link it just created while the staging link is still available for identity
checking. If that rollback cannot be confirmed, the tool reports that partial
cleanup is uncertain; inspect the selected disposable destination before
retrying. An abrupt process interruption can leave an orphan snapshot and a
staging directory; the required matching sidecar makes validation and restore
reject that snapshot, and retry refuses to overwrite it. This is not atomic
two-file publication and does not establish power-loss or filesystem
crash-durability guarantees. The CLI reports operational filesystem errors
without echoing raw paths or filesystem exception details.

## Disposable restore exercise

Use synthetic lifecycle records only. Create a snapshot, validate it, restore
it to a distinct fresh temporary path, and validate the restored copy. Include
an unresolved `EXECUTION_UNKNOWN` claim and verify the report continues to
identify it as unresolved after restore. Include a verified recovery only
when its linked incident, handoff, workflow, action, verification evidence,
and audit event are all present. Test corrupted input and interrupted backup
failure without using a live database. Do not run application startup against
anything except an explicitly disposable restored copy.

## Operational decisions still required

No production values are selected by this toolkit. The planning assumptions
for discussion are **RPO 24 hours** and **RTO 8 hours**, both pending operator
approval. These are not service commitments or validated targets.

Before any production implementation, assign an accountable backup and
recovery owner; confirm the effective database path, topology, filesystem,
SQLite/journal configuration, and application version; approve RPO/RTO,
backup frequency and retention, destination, encryption and access controls,
audit-history retention, and recovery/validation authority. Then design and
exercise a production-appropriate procedure in an approved isolated
environment. Do not treat this disposable-only tool as an operational backup
service.
