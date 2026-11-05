# Deployment Architecture Decision

**Status: conditional recommendations ready; operator decisions remain
required.** This record separates repository evidence from operator
decisions. It does not select or configure a production deployment, migrate
data, or authorize live remediation.

## Executive summary

The application implements SQLite only. Repository setup instructions run
the API on the host, outside Compose; neither that command nor the Compose
definitions establish worker count, shared-file configuration, or the
topology of a deployment. No deployment manifest establishes a deployed
database backend.

The technical default, if operators choose a single-host deployment, is to
qualify the existing SQLite implementation rather than introduce a new
backend. If operators require multiple hosts or independently deployed
workers, the current application is incompatible with that requirement
until a shared-backend design is separately selected, implemented, and
tested. The available evidence cannot decide which topology is required or
set availability/recovery targets.

The repository defaults to dry-run and automatic remediation disabled, but
environment overrides can change behavior. No deployed configuration was
verified. The recommended first-deployment posture is dry-run only; an
operator must confirm it. Approval separation, audit retention, backup
ownership, recovery targets, and live-remediation intent remain operator
decisions.

## Verified repository facts

- [database.py](../app/database.py) imports Python's `sqlite3`, creates a
  connection to a filesystem path, uses a 10-second connection timeout, and
  defaults to `incidents.db` in the repository root. `KYBERNAUT_DATABASE_PATH`
  changes the path; it does not select another database backend.
- [action_claim_repository.py](../app/services/action_claim_repository.py)
  creates claim tables and indexes with SQLite SQL. Workflow uniqueness is
  `(operator_id, service, idempotency_hash)`. An active logical-action
  partial unique index prevents two active rows with the same logical key.
  Capacity is counted and reserved in application SQL under
  `BEGIN IMMEDIATE`. This depends on SQLite write serialization for the
  same database file and is not a portable database constraint.
- [audit_repository.py](../app/services/audit_repository.py) creates the
  approval and audit tables inline. Approval creation and decision update
  the corresponding action claim in the same SQLite connection transaction.
  The tables do not declare foreign keys for these references. Audit event
  writes use separate connections/transactions; claim, approval, execution,
  verification, and audit history are therefore not one atomic unit.
- [main.py](../app/main.py) initializes SQLite through
  `initialize_database()` during application lifespan. The other repository
  service tables are initialized by their repositories. No versioned
  migration runner or rollback migration was found.
- [requirements.txt](../requirements.txt) has no database client, ORM, or
  migration framework. No `pyproject.toml`, `app/config.py`, Alembic
  configuration, or migrations directory was found.
- [docker-compose.yml](../docker-compose.yml) defines target applications
  and observability services, not the Kybernaut API or a Kybernaut database.
  [docker-compose.observability.yml](../docker-compose.observability.yml)
  defines Loki and Alloy only. No root Kybernaut API Dockerfile or deployment
  topology was found in the inspected deployment files. The setup section
  in [README.md](../README.md) instructs running Uvicorn on the host, outside
  Compose. No repository deployment definition shows multiple API workers,
  hosts, or a shared SQLite volume; actual deployment topology is unknown.
- [ci.yml](../.github/workflows/ci.yml) runs tests on GitHub Actions with
  Python 3.12. [tests.yml](../.github/workflows/tests.yml) also runs tests
  with Python 3.11. Neither workflow provisions or tests another database
  backend.
- [README.md](../README.md) identifies SQLite as incident storage.
  [.env.example](../.env.example) documents only the optional SQLite path
  override. Neither establishes the topology or backend of an actual
  deployment. `.env` was not inspected. The README API startup command binds
  to `127.0.0.1:8000`; this documents local setup, not a production network
  architecture.
- [test_action_claim_repository.py](../tests/test_action_claim_repository.py)
  and [test_audit_repository.py](../tests/test_audit_repository.py) use
  temporary SQLite files, independent connections, concurrent operations,
  and an explicit SQLite write-lock conflict case. These tests establish
  behavior only for the tested SQLite setup, not a remote/shared database,
  alternate backend, or production filesystem.
- [k8s_remediation.py](../app/services/k8s_remediation.py) defaults to
  `dry_run`; setting `KYBERNAUT_REMEDIATION_MODE=live` makes the live path
  technically available only when the current context is exactly
  `kind-kybernaut`. [policy_engine.py](../app/services/policy_engine.py)
  defaults automatic remediation to disabled, with an environment
  override. [.env.example](../.env.example) shows both safer values as
  examples, not enforced values. No deployment config or runtime evidence
  establishes whether live execution is enabled anywhere.
- [operations.py](../app/api/operations.py) authenticates the operator,
  and requires remediation permission to request or decide actions. No
  separate shared approval token is used.
  [audit_repository.py](../app/services/audit_repository.py) rejects an
  approval when the stored `requested_by` identifier equals `decided_by`,
  except when the authenticated operator has the explicitly trusted `owner`
  permission. This remains identifier-string comparison; deployed identity
  uniqueness, aliasing, and effective permission policy are not verified.
- The repository contains no Kybernaut database backup/restore procedure,
  RPO/RTO, or backup-retention policy. Loki's configured log retention is
  separate and does not define application audit-event retention.
  [audit_repository.py](../app/services/audit_repository.py) has no audit
  pruning, export, or retention enforcement; audit events are not
  tamper-evident. These are repository findings, not proof that external
  deployment processes do not exist.

## Deployment options

| Dimension | A. Single host with one SQLite file | B. Multiple workers/hosts with shared transactional database |
|---|---|---|
| Repository support | Implemented using Python `sqlite3`; default path and path override are local files. | Not implemented. No backend abstraction, client dependency, deployment service, or migration framework is present. |
| Concurrency model | Claim write transactions use `BEGIN IMMEDIATE`; SQLite serializes writers to one database file. Effective safety depends on one shared file on a supported local filesystem and SQLite locking behavior. Concurrent readers/writers and lock timeout behavior still need operational qualification. | Could support concurrent writers through a transactional service, but only after choosing a backend and implementing its isolation, constraints, retry/error behavior, and connection lifecycle. No such guarantee currently exists. |
| Uniqueness and transactions | SQLite primary keys, `UNIQUE` constraints, and a partial unique index enforce specified row identities. Claim and approval updates share a transaction when implemented on one connection. | Must be re-established with backend-specific constraints and transaction semantics. Existing SQLite DDL is not evidence that the same partial-index or locking behavior is available or equivalent elsewhere. |
| Action claims and rate limit | Same-file capacity reservation is serialized by SQLite `BEGIN IMMEDIATE`; active logical-action uniqueness is also database-indexed. Legacy remediation and separate database files are outside the same global boundary. | A shared service could provide a common serialization point, but rate reservation needs a designed transaction/locking strategy and production-backend concurrency tests. Not currently supported. |
| Audit and reconciliation | Audit rows are separate from claim transactions. Partial failure can leave missing audit evidence or unresolved `EXECUTING`/`EXECUTION_UNKNOWN` claims. No reconciliation interface/procedure exists. | Shared storage alone would not make audit atomic; a transaction boundary covering records would need explicit implementation. Reconciliation remains required. |
| Backup and recovery | One file simplifies backup operations but requires a consistent SQLite-aware backup procedure, tested restore, integrity checks, retention, filesystem durability, and a decision for restoring claims after a crash. No application backup/restore process or RPO/RTO is established. | Requires service backup/PITR configuration, restore exercises, access recovery, and consistency planning. No service or procedures are present. |
| Migration/schema management | Inline `CREATE TABLE IF NOT EXISTS` and selected additive `ALTER TABLE` statements; no versioned migration history or tested downgrade. | Requires a migration framework and backend-specific migrations before data or runtime cutover. No such path exists. |
| Operational complexity | Lower service count, but requires disciplined single-host storage, monitoring, backup, restore, and process topology. SQLite on an unknown/network filesystem is not assumed safe. | Higher operational and security complexity: database service lifecycle, credentials, network policy, pooling, availability, backup, monitoring, and migration ownership. |
| Security and access control | Protect host and file permissions, backups, and any mounted volume; database access is bounded by filesystem access. No file permission/deployment policy is established here. | Requires service authentication, least-privilege database roles, secret distribution, TLS/network boundaries, and auditing. None is configured in the project. |
| Compatibility with current code | Directly compatible with the existing adapter and tested claim repositories, subject to deployment qualification. | Not compatible without application changes. Do not configure an alternate URL or claim portability until adapter and tests exist. |

### Conditional recommendation

Do not make a topology choice on behalf of operators. If the decision register
selects a single-host, low-complexity first deployment and the measured
availability/recovery needs are compatible with a local file, Option A is the
smallest current-code path, conditional on a validated local filesystem,
shared-file process model, backup/restore and recovery exercises. If the
required topology includes multiple hosts or requires a managed shared
service, Option B is the architectural direction to evaluate, but it requires
a separate implementation and backend qualification phase. Do not simulate
Option B by putting the SQLite file on a network share.

## Operator decision register

Statuses distinguish technical recommendations from operator authorization:

- `RESOLVED_BY_EVIDENCE`: repository evidence itself resolves the technical
  fact; it does not substitute for an operational approval.
- `RECOMMENDATION_READY`: a defensible default is available, but an operator
  must accept or reject it.
- `DECISION_REQUIRED`: requirements or authorization are not present.

Owners are not assigned because the repository does not identify accountable
operators.

| ID | Decision question and available options | Technical consequences and recommended default | Owner | Status |
|---|---|---|---|---|
| D-01 | Single API process on one host, multiple workers on one host, or multiple hosts? | Current code has one SQLite file path and no deployment manifest. Recommend one host/process only if that satisfies actual availability needs; multiple writers/hosts require an explicitly shared database and topology qualification. | Unassigned | `DECISION_REQUIRED` |
| D-02 | SQLite or a shared transactional backend? | SQLite is the only implemented backend; path override is still SQLite. Recommend SQLite only conditional on D-01 selecting a qualified single-host topology. A shared backend requires separately choosing a product/version, adding an adapter/schema migration path, and testing it. | Unassigned | `RECOMMENDATION_READY` |
| D-03 | What availability, RPO, RTO, and lock/contention behavior are required? | No targets are documented. Recommend defining numeric objectives before choosing a topology; do not infer targets from connection timeout or test timing. | Unassigned | `DECISION_REQUIRED` |
| D-04 | Who owns database access, backup, restore, integrity checks, and recovery? What are the required backup/restore procedures? | No application procedure or owner is in the repository. Define accountable owner, schedule, protected/off-host copy policy, restore validation, integrity checks, and recovery of pending/unknown claims. | Unassigned | `DECISION_REQUIRED` |
| D-05 | What audit retention, integrity, export, and independent evidence are required? | No retention/pruning/export policy exists for application audit rows; Loki's log retention is separate. Recommend setting retention and independent evidence requirements before operational use; do not equate SQLite audit rows with tamper-evident evidence. | Unassigned | `DECISION_REQUIRED` |
| D-06 | Is self-approval allowed, and which action classes require a separate approver? | The current repository rejects approval when requester and decider identifiers match, for all approval decisions. Confirm this policy and its action-class scope; separately qualify whether authenticated identifiers are unique and non-aliasing in the deployed identity system. | Unassigned | `IMPLEMENTED_LOCALLY — OWNER CONFIRMATION REQUIRED` |
| D-07 | Is the first deployment dry-run only, or is live remediation intended? | Code defaults to dry-run and auto-remediation-off, but environment values can enable live execution/automatic authorization. Recommend first deployment be dry-run only with effective settings verified; do not infer deployed state from `.env.example`. | Unassigned | `RECOMMENDATION_READY` |

## Claim and audit reconciliation decision gate

### D-06 local test configuration update

The local trusted `test-operator` is configured with the `owner` permission
and may approve its own request. Other remediation operators remain subject
to requester/approver separation unless the explicit
`KYBERNAUT_ALLOW_SELF_APPROVAL` global override is enabled. This supersedes
the earlier D-06 description of unconditional same-identifier rejection.
This is a local test-account disposition; it does not establish production
owner identities or the action classes that may permit self-approval.

**Status: design and inspection only. No implementation or state-changing
resolution is authorized by this section.** The repository has workflow and
action claims, approval records, verification payloads, handoffs, and audit
events. Their writes do not share one transaction in every path; the actual
deployment database, filesystem, process topology, ownership, and audit
retention are not established here. `RUNNING`, `EXECUTING`, an expired lease,
or a missing audit row is not proof that an action is currently running or
did not execute.

The table distinguishes repository facts from proposed requirements. Every
decision with absent evidence or approval remains `pending`. Approval
references are intentionally blank until an authorized, auditable decision
exists; role labels identify accountable functions, not assigned people.

| ID | Repository fact and required evidence or decision | Status | Accountable decision-maker role | Auditable approval reference | Dependencies | Consequence if unresolved |
|---|---|---|---|---|---|---|
| CR-01 | **Fact:** the inspected repository uses SQLite; a local path override does not establish the deployed backend or topology. **Required:** authoritative, sanitized evidence for database owner, supported backend, API instance/process/worker counts, and shared-storage boundaries; explicitly decide the supported topology. | `pending` | Application/platform architecture owner and database/platform operations owner | None recorded | None | No deployment or cross-process safety claim; prototype remains temporary-fixture-only. |
| CR-02 | **Fact:** claim operations use SQLite transactions, including `BEGIN IMMEDIATE`; this does not prove behavior on an unknown filesystem or across different files. **Required:** identify filesystem/storage, locking and durability assumptions, journal settings, and snapshot consistency; distinguish repository test facts from deployment evidence. | `pending` | Database/platform operations owner | None recorded | CR-01 | Cannot generalize fixture results to deployment or claim snapshots are consistent. |
| CR-03 | **Fact:** claim/action statuses and JSON payloads, approval rows, verification JSON, handoffs, and audit events are separate correlated records. **Required:** approve source-of-record and precedence rules for execution outcome, verification, claims, approvals, and audit history; state how contradictions and missing evidence are represented. | `pending` | Application/service owner with remediation-policy and audit-history owners | None recorded | CR-01; CR-05; CR-08 | Dispositions could overstate completion or authorization; do not resolve state. |
| CR-04 | **Fact:** the remediation service has a configured JSONL audit path; actual deployment use, authoritative status, and capture boundary are not established by SQLite rows. **Required:** decide whether it remains authoritative, supplementary, or formally deprecated, and define capture, correlation, gap/overlap handling, access, and retention if retained. | `pending` | Audit-history owner and compliance/records owner | None recorded | CR-03; CR-05 | Audit completeness cannot be assessed; absence in either source cannot establish non-execution. |
| CR-05 | **Fact:** repository audit rows have no retention/export enforcement or tamper-evident guarantee. **Required:** approve retention, export, append-only/integrity expectations, access, independent review, and evidence of review. | `pending` | Audit-history owner with compliance/security and service owner | None recorded | CR-03; CR-04 | Reconciliation evidence may be incomplete, unavailable, or not independently reviewable. |
| CR-06 | **Fact:** existing API read routes use JWT permissions, but no reconciliation-specific access or separation-of-duties policy is recorded. **Required:** define roles permitted to view reports, investigate, propose resolutions, authorize changes, execute approved resolutions, and independently review them; decide separation and emergency exceptions. | `pending` | Service owner and security/identity owner | None recorded | CR-03; CR-05 | No authenticated operator surface or state-changing resolution may be designed for use. |
| CR-07 | **Fact:** the current code does not define an external-evidence collection contract for claim reconciliation. **Required:** specify admissible sources, source identity/provenance, timestamps, retention, integrity, redaction, and safe collection/approval boundaries. Do not collect live evidence as part of this phase. | `pending` | Incident/action owner with security and audit-history owner | None recorded | CR-03; CR-05; CR-06 | External evidence cannot support a disposition; case remains insufficient or unresolved. |
| CR-08 | **Fact:** code and tests preserve or enter `EXECUTING`, `EXECUTION_UNKNOWN`, and `RECONCILIATION_REQUIRED`; these states are nonterminal/ambiguous rather than proof of current activity. **Required:** approve hold, escalation, investigation, and disposition rules for these states, contradictory evidence, stale claims, and missing linked records. Missing audit must never prove non-execution. | `pending` | Incident/action owner and remediation-policy owner | None recorded | CR-03; CR-06; CR-07 | No replay, release, success transition, or claim closure; escalate/hold under existing safeguards. |
| CR-09 | **Proposed prototype acceptance:** temporary SQLite fixtures only; read-only queries with no schema initialization, migrations, reconciliation calls, or writes; a pure classifier emits evidence-backed dispositions, evidence references, gaps, and contradictions; repeated reads leave database state unchanged; synthetic partial-persistence, missing-audit, contradictory-verification, interrupted-execution, and stale-claim cases are covered. External evidence and deployed databases are excluded. **Required:** review and accept this fixture-only test contract before implementation; unresolved deployment/audit decisions must remain explicit limitations, not silently assumed. | `pending` | Design owner and independent code/security reviewer; identities unassigned | None recorded | Prototype-specific scope acceptance; CR-03/CR-08 disposition rules for the fixture cases; no production or live-lab authorization | Prototype work does not begin until its scope and acceptance contract are accepted; no production inference. |
| CR-10 | **Required before any later state-changing resolution is considered:** separate written authorization for that scope; approved operator identity/permissions and separation of duties; accepted topology and authoritative evidence rules; approved audit retention/integrity/reviewer policy; explicit per-disposition semantics, transaction/failure behavior, rollback and escalation; isolated synthetic qualification. Prototype approval is not this authorization. | `pending` | Service owner, security approver, remediation-policy owner, and audit/recovery owner | None recorded | CR-01 through CR-08; successful CR-09 prototype review | State-changing design and use remain prohibited; production recovery remains NO-GO. |

### Read-only prototype entry criteria

The proposed prototype may be considered only after CR-09 scope and acceptance
are explicitly accepted by accountable reviewers. Its boundary is limited to
temporary SQLite fixtures and a non-mutating report. The implementation must:

1. use no production, lab, or existing application database;
2. perform no schema initialization, migrations, handoff lease reconciliation,
   claim transitions, audit writes, or remediation calls;
3. classify from a read snapshot through a pure function and preserve source
   references, evidence gaps, and contradictions in its output;
4. label dispositions conservatively; missing audit never means not executed,
   and `RUNNING`/`EXECUTING` never proves current activity;
5. include synthetic cases for partial persistence, missing audit, conflicting
   verification, interruption during execution, and stale claims;
6. prove by before/after database snapshots and repeated reads that the
   prototype changes no fixture state; and
7. make no claim about cross-process behavior, deployment topology, production
   safety, or live evidence.

Passing this gate would authorize only the accepted isolated prototype scope.
It would not authorize production access, live-lab use, status-changing
resolution, action replay/release, or production database recovery.

## Minimum architecture gates

These are evidence gates, not approvals. Passing one does not imply that
another gate has passed.

### Claiming concurrent-action safety

- Record D-01 and D-02 decisions and inspect the actual deployment's process
  topology, configured database, filesystem/service, and connection model.
- Run independent-session contention, uniqueness, duplicate-key, transaction
  rollback, lock/serialization conflict, restart, and approval-binding tests
  against the actual backend and intended topology.
- For a configured capacity of one, run at least two simultaneous distinct
  claims for the same deployment and demonstrate exactly one reservation
  succeeds; repeat across the maximum supported number of application
  instances. For idempotency, demonstrate one persisted workflow claim for
  concurrent same-scope key submissions.
- Verify configured limits and show that every supported remediation entry
  point participates in the same rate-limit boundary, or document the
  explicit exclusions.
- Document failure behavior; ambiguous execution remains unresolved and is
  not automatically retried or released.
- Do not extrapolate the current SQLite tests beyond the tested single-file
  local SQLite configuration.

### Enabling automated monitoring

- Approve a trigger contract, identity/authentication model, evidence
  freshness/source-health behavior, deduplication and retry semantics.
- Prove durable trigger acceptance, correlation, and action claims on the
  intended backend under duplicate delivery, process restart, and
  concurrency.
- Demonstrate that policy and approval gates cannot be bypassed and that
  monitoring cannot itself authorize execution.
- Provide operational telemetry, alerting, ownership, and rollback/disable
  controls. This phase does not authorize or implement a monitor.

### Running an authorized live-lab evaluation

- Obtain explicit scenario-specific authorization and use only the approved
  lab context/namespace and existing simulator procedure.
- Verify context, node health, namespace/workload baseline, and dry-run
  settings before each injection; capture timestamps and evidence.
- Observe, classify, and record the outcome before supported cleanup; verify
  cleanup and lab health afterward. Stop on unexpected pressure, cleanup
  failure, or scope uncertainty.
- Treat live remediation as a separate authorization. A live failure
  injection alone does not authorize remediation.

### Enabling any live remediation

- Confirm the implemented D-06 same-identifier rejection and its action-class
  scope; resolve the D-07 dry-run recommendation;
  verify least-privilege credentials, exact
  cluster/namespace scope, effective runtime configuration, and safety
  policy.
- Validate the separation rule against the intended identity provider and
  database, including identifier uniqueness, expiry, denial, concurrency,
  and replay.
- Demonstrate pre-action authorization, durable claim reservation, execution
  outcome handling, recovery/verification, and operator reconciliation.
- Keep live execution disabled until a separately approved rollout and
  rollback plan is exercised.

### Claiming production readiness

- Resolve all decision-register entries and establish a supported deployment
  artifact/topology and versioned schema/migration/rollback procedure.
- Complete backend-specific concurrency, security, backup/restore,
  failure-recovery, capacity, and observability validation.
- Define audit retention, tamper-evidence or independent export, access
  control, and review ownership.
- Demonstrate tested operational procedures for incidents, upgrades,
  restore, unresolved claims, disablement, and rollback.
- Obtain documented operational/security approval. Repository tests alone
  are not production-readiness evidence.

## Migration and rollout prerequisites

No migration or rollout is authorized by this record. Before any backend or
topology change:

1. Resolve D-01 and D-03 through D-07. D-02 follows from D-01: retain SQLite
   only for an accepted single-host deployment, otherwise select a shared
   transactional backend in a separate architecture decision. Do not select
   a database product or numeric recovery target by inference.
2. Inventory existing incident, approval, claim, execution, verification,
   and audit data without modifying it; decide data-retention and migration
   requirements.
3. Design a versioned schema/migration plan with backups, compatibility
   windows, rollback/forward-repair behavior, and tests on disposable
   instances of the actual backends.
4. Implement the selected adapter and transaction boundaries in a separately
   approved phase. Preserve fail-closed behavior for migration errors,
   lock/serialization conflicts, and ambiguous execution outcomes.
5. Validate restored data and claim/approval/audit links before routing
   traffic. Do not enable multiple writers until backend-specific
   concurrency and operational gates pass.
6. Stage rollout with monitoring and an explicit disable/rollback procedure;
   do not discard claim or audit history as a rollback shortcut.

## Concrete next-step path

The smallest operator input needed before implementation planning can become
backend-specific is:

1. Confirm whether the initial API deployment is one process on one host or
   requires multiple concurrent workers/hosts.
2. Confirm dry-run-only as the first-deployment posture or explicitly request
   a separate live-remediation design review.
3. Assign owners and define availability/recovery, database backup/restore,
   audit retention, and approval-separation requirements.

If the first answer is single host and the dry-run recommendation is accepted,
the next engineering phase can qualify the existing SQLite code against the
selected host filesystem/process model, then design versioned schema
management and recovery procedures. If multiple workers/hosts are required,
stop before using the current SQLite code for that topology; first approve a
specific shared-backend design, then implement and test it in a separate
authorized phase. No implementation is started by this record.

## Risks, non-goals, and boundaries

- Same-file SQLite behavior is not a claim about multiple database files,
  network filesystems, containers with divergent volumes, or another backend.
- Approval and claim updates may commit together, but audit events, execution,
  and verification are not one atomic transaction. Missing audit rows and
  partial states require reconciliation.
- Approval records capture requester and decider identifiers, and the
  repository rejects approval when those strings match. This does not prove
  deployed identity uniqueness or non-aliasing; authentication and permission
  configuration still require deployment verification.
- Defaults for dry-run and automatic remediation are not proof of effective
  deployed configuration. Do not enable live execution based on repository
  defaults.
- No database migration, adapter, deployment, service change, live test,
  Kubernetes interaction, external service call, or production operation is
  performed or authorized in this phase.

## Related records

- [Repository Completion Audit](./REPOSITORY_COMPLETION_AUDIT.md)
- [Monitoring Trigger Decision](./MONITORING_TRIGGER_DECISION.md)
