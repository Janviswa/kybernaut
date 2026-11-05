# Monitoring Trigger Architecture and Decision

**Status: design decision, not implementation authorization.** This record
compares trigger options and records the earlier operator-API recommendation.
Phase F.21 hardened request validation and error handling. Phase F.22 added
SQLite-backed idempotency and action claims to this route and the existing
response graph. The analysis and repository facts below are historical to
that review and are superseded where stated in the Phase 4.18 addendum.

**Phase 4.18 decision (2026-10-24):** retain both trigger modes. The
application anomaly monitor remains enabled by default unless explicitly
disabled, and operator-initiated API workflows remain supported alongside
it. The monitor persists incident state and dispatches authorized handoffs;
remediation remains subject to existing approval and authorization
safeguards. Authenticated `GET /monitor/status` is a read-only status surface,
not a trigger. `/health` reports API liveness only, and monitor status is not
proof of production reliability. This decision does not authorize live
operations or alter remediation policy.

## Decision summary

**Historical recommendation:** use the operator-initiated API request as the
first supported trigger. The current approved design retains this entry point
alongside background monitoring. Use the existing authenticated
`POST /workflow/{service}/respond`
entry point as an explicitly initiated *response-workflow request*, not as a
claim that Kybernaut continuously detects incidents. It is the smallest
increment because API authentication, evidence collection, incident
classification, deterministic policy, approval records, and a dry-run-first
remediation boundary already exist. It introduces no new delivery service,
worker, scheduler, sender identity, or external network path.

This recommendation is conditional: before operational use, the deployment
must verify that automatic remediation is disabled and review SQLite
single-file assumptions, claim reconciliation, and operator authorization.
The route now requires an idempotency key and uses durable claims to prevent
duplicate accepted requests, duplicate active logical actions, and
oversubscription of the configured response-graph action limit within the
SQLite database. These guarantees do not extend to another database,
another SQLite file, the separate legacy remediation endpoint, or unreviewed
deployment topology. They are not proof of production safety.

### Database compatibility boundary (Phase F.23)

Repository configuration establishes SQLite as the implemented local
backend: [database.py](../app/database.py) uses Python's `sqlite3` module,
defaults to `incidents.db` at the repository root, and allows only a file
path override through `KYBERNAUT_DATABASE_PATH`. There is no `app/config.py`,
database URL abstraction, ORM/alternate driver, or versioned migration
framework in the inspected tree and dependencies. The inline initializers
create tables/indexes and add selected columns. Neither Compose file defines
a Kybernaut API database service.

The claim and audit tests below exercise SQLite using temporary database
files. They do not identify the actual deployment backend, file-sharing
topology, or filesystem; those remain operator/deployment verification
blockers. Claim identity and active-logical-action uniqueness are backed by
SQLite primary/unique constraints and a partial unique index.
Rate-capacity reservation is enforced by application SQL inside
`BEGIN IMMEDIATE` transactions, which serialize writers to the same SQLite
database file; it is not a portable database constraint. Legal action-state
transitions are application-checked conditional updates, not database CHECK
constraints. Approval creation/decision and the associated claim transition
are committed in one SQLite connection transaction, but no foreign-key
constraints enforce the association. Audit-event writes remain separate.
None of these properties should be generalized to another backend or
deployment topology without dedicated verification.

The repository does not establish a deployed topology or backend. The
[Deployment Architecture Decision](./DEPLOYMENT_ARCHITECTURE_DECISION.md)
compares the current-code single-host SQLite option with a not-yet-supported
shared transactional database option, records unresolved operator decisions,
and defines readiness gates. No production choice or migration is authorized.

**Not selected for the first milestone as additional trigger modes:** an
authenticated alert webhook or a separate scheduled availability poll for
the `up`-based outage detector. The existing lifespan anomaly monitor polls
Prometheus features at its configurable interval; it is distinct from a
webhook and does not schedule `detect_unhealthy_services()`. A new availability
poll would need an owner-approved cadence and scope. A webhook would introduce
delivery/replay/concurrency behavior and alert-to-incident mapping. Either
additional trigger can be reconsidered after operator decisions and a trigger
contract are approved.

## Verified current state

- At the time of this historical review, [main.py](../app/main.py) defined
  request-driven routes. `GET /incidents`
  invokes `detect_unhealthy_services()` and does not require operator
  authentication in this route. `GET /diagnose/{service}` and
  `GET /decide/{service}` were also request-driven. The process lifespan then
  initialized the database and anomaly policies; no monitoring loop was
  present in that reviewed version.
- [incident_detector.py](../app/services/incident_detector.py) queries
  Prometheus `up{job=~".*-service"}` and `/api/v1/targets`, then creates
  service outage, scrape-failure, and missing-target records. It returns
  inconclusive on an empty health vector, uses a 10-second HTTP-client
  timeout, and is called by the endpoint rather than by a background worker.
- [incident_diagnoser.py](../app/services/incident_diagnoser.py) executes
  three sequential instant queries (availability, five-minute 5xx rate, and
  P95 latency) with a 10-second HTTP-client timeout per query. It retains
  timestamps and marks missing evidence, but it does not use the feature
  collector's sample-age/skew policy. These per-request limits are not a
  measured end-to-end trigger latency.
- [prometheus_features.py](../app/services/prometheus_features.py) queries
  request rate, latency, memory, and CPU in sequence and rejects incomplete,
  stale, future, or excessively skewed feature rows using configurable
  defaults. It is called on demand; instant-query timestamps do not establish
  freshness of the underlying raw scrapes.
- [operations.py](../app/api/operations.py) defines
  `POST /workflow/{service}/respond`. It requires a valid operator JWT with
  `remediation` permission, accepts an optional caller-supplied
  `incident_id` constrained to 1-64 ASCII letters, digits, `.`, `_`, `:`,
  or `-`, and requires an `Idempotency-Key` header constrained to 1-128
  ASCII letters, digits, `.`, `_`, `:`, or `-`. Unsupported services are
  rejected before graph execution. Workflow exceptions return a generic
  503 response; exception details are not returned to the caller. It does
  not accept evidence as request input. Claims are scoped to authenticated
  operator, service, and key; reusing a key with a different service/caller
  correlation conflicts rather than starting a new graph. A caller
  correlation ID still does not establish ownership or act as an
  idempotency key.
- [jwt_auth.py](../app/core/jwt_auth.py) validates signed JWT issuer,
  audience, expiry, subject, and permission claims. It authenticates
  operators, not a webhook sender or background worker.
- [incident_response_graph.py](../app/services/incident_response_graph.py)
  collects evidence, classifies, plans, applies deterministic policy, and
  either closes without action, requests approval, or routes to remediation
  if policy configuration permits automatic execution. A missing supplied
  incident ID is replaced with a random ID. Diagnosis, anomaly, and
  Kubernetes collection failures are represented as unknown/unavailable in
  several paths and the graph can continue without complete observations.
- [policy_engine.py](../app/services/policy_engine.py) defaults automatic
  remediation to disabled, but `KYBERNAUT_POLICY_AUTO_REMEDIATION` can enable
  it. `POST /workflow/{service}/respond` does not itself force manual
  approval. [k8s_remediation.py](../app/services/k8s_remediation.py) defaults
  to dry-run and its live mode checks for `kind-kybernaut`, but this design
  phase did not verify deployed configuration or Kubernetes permissions.
- [audit_repository.py](../app/services/audit_repository.py) persists
  approvals and events in SQLite. The integrated graph writes many events
  best-effort; its pre-execution audit write is mandatory and fails closed.
  Phase F.22 adds additive claim-link columns and binds each approval to a
  reserved action claim, exact proposal hash, policy record, deployment, and
  incident reference. Claim transitions and approval decisions share a
  SQLite transaction; audit events remain separately committed.
  [resume_after_decision()](../app/services/incident_response_graph.py)
  records a decision, then an approved decision rechecks policy and resumes
  the graph. The approval API also requires the remediation permission.
- [incident_repository.py](../app/services/incident_repository.py) refreshes
  an active record by fingerprint or incident ID. The Prometheus detector
  creates stable IDs from service and type, but this is not a unique trigger
  ID. Its select-then-insert flow has no demonstrated atomic deduplication
  constraint for concurrent requests.
- No periodic scheduler, Prometheus polling worker, Alertmanager integration,
  or alert-webhook receiver was found in the inspected application.
  [MONITORING_AND_EVALUATION_DESIGN.md](./MONITORING_AND_EVALUATION_DESIGN.md)
  describes these as design alternatives, not implemented behavior.

## Option comparison

Latency entries below describe dependencies, not measured service-level
performance. The repository contains no comparative trigger-latency
measurement.

| Dimension | A. Operator-initiated API request | B. Authenticated alert webhook | C. Periodic Prometheus polling |
| --- | --- | --- | --- |
| Current implementation | Partially available: authenticated `/workflow/{service}/respond` starts the response graph. `/incidents` detects and records Prometheus availability issues, but does not start that graph and is unauthenticated in the route. | No receiver or Alertmanager integration found. | No polling worker found. An operator request can call the existing `up` detector; there is no periodic execution. |
| Complexity | Lowest initial complexity: reuse FastAPI, JWT authorization, SQLite audit/approval, and the existing graph. F.22 adds durable claims; deployment mode and reconciliation remain unresolved. | Higher: sender identity and transport security, payload validation/versioning, alert-to-service mapping, rate limits, replay protection, durable acceptance/deduplication, delivery retries, sender operations, and alert-rule ownership. | Higher than the first operator path: worker lifecycle/leader election or single-owner deployment, cadence/jitter, query watermarks, overlap control, backlog/retry behavior, health metrics, and deduplication. |
| Detection latency and dependencies | Human initiation delay is unbounded by code. After receipt, latency depends on sequential Prometheus calls, feature collection, Kubernetes observation, graph work, and optional RCA. No end-to-end latency was measured. | Depends on rule evaluation, sender scheduling, network delivery, receiver validation, and queueing. Those components and timings are not configured here. | Detection is delayed by polling interval plus collection and processing time. No interval is selected. |
| Authentication/authorization | JWT subject and permission checks exist. The current response endpoint requires the broad `remediation` permission even to start a workflow; whether a narrower trigger permission is desired is an operator/security decision. | Must authenticate a machine sender with narrowly scoped identity; existing operator JWT code is not a webhook contract. Mechanism (mTLS, signed request, or equivalent), rotation, and trust owner are undecided. | Requires a least-privilege workload identity for Prometheus access and a secured path to the API/database or in-process worker. Worker identity and permissions are not defined. |
| Duplicates/idempotency | F.22 requires a key scoped to operator/service; completed responses replay, unresolved claims conflict, and key reuse with another correlation conflicts. The correlation ID itself is not an idempotency key. | Delivery retries, grouped notifications, and repeated firing/resolved updates are expected integration cases. No stable source alert ID or dedupe storage exists in Kybernaut. | Repeated observations of one continuing condition are normal; polling must refresh one incident rather than create duplicate events. Stable incident IDs exist in the basic Prometheus detector, but no trigger lease/atomic claim or general dedupe window is implemented. |
| Concurrency/races | SQLite `BEGIN IMMEDIATE` and uniqueness constraints serialize same-file workflow and capacity claims. Active logical action duplicates conflict. These guarantees do not apply across different database files/backends or the separate legacy route. | Concurrent sender retries and multiple alerts can race on delivery claims and incident state. Need atomic idempotency and per-incident coordination. | Multiple API replicas/workers could poll simultaneously; leader election/lease and query cursor semantics would be required. |
| Source health and stale telemetry | Workflow collection can return unavailable/unknown; feature rows are filtered by configured age/skew. Diagnosis does not apply the same age guard. No input evidence is accepted from caller; missing source values must not be represented as healthy. | Alert payload may be old, malformed, duplicated, or disconnected from current source health. Need retain source event time separately from receipt time and define how to recheck the current source state. | Direct Prometheus access can observe source/query failure, but existing detector's empty-vector behavior is inconclusive, not a healthy result. Polling freshness, lookback, and target disappearance semantics need explicit tests and policy. |
| Audit/correlation | Workflow/action claim IDs are persisted and linked to approvals and graph audit events. Audit writes are separate transactions and many remain best-effort. | Can preserve provider alert identity and original timestamps if included in an authenticated, bounded envelope. Needs receipt/rejection audit and linkage to provider evidence. | Can record poll cycle, query window, cursor, and derived incident identity. Requires durable cycle/lease records and evidence references not currently present. |
| Restart/recovery | Completed responses replay. Running workflow and executing/unknown action claims are not automatically retried or released; operator reconciliation is required. Persisted pending approvals can be decided and revalidated. | Requires durable inbox/outbox or equivalent acceptance state to survive receiver restart and safely handle sender retry. No such mechanism exists. | Requires durable scheduler ownership/cursor/lease to avoid missed or duplicated intervals after restart or replica changes. No such mechanism exists. |
| Operational cost/dependencies | Uses the existing API and operator; cost is request-driven and depends on operator activity. Human availability and request handling are required. | Adds alerting rules/sender, service credentials/network path, receiver operations, and delivery monitoring. | Adds always-on worker or scheduler, regular Prometheus queries, and operational ownership. Query rate depends on the unselected cadence. |
| Risk of unintended remediation | Bounded by current policy and approval design only if deployed settings preserve manual approval. Environment-controlled auto mode means an authenticated trigger may route to remediation without a new approval when enabled. | Automated event delivery can repeatedly initiate response; sender authentication is not approval to remediate. Must always stop at policy/approval boundary. | Continuous trigger may create repeated workflows or approvals at cadence. It must not equate persistent alerts with permission to execute. |

### Facts, assumptions, and recommendations

**Historical repository facts:** only request-driven entry points were found;
`POST /workflow/{service}/respond` has operator JWT authorization; the basic
Prometheus detector handles availability/target observations; response
workflow behavior depends on policy configuration; there is no trigger
idempotency key, webhook receiver, or poller.

**Assumptions not verified:** operators can reliably initiate/triage requests;
the deployed API is reachable only by intended operators; environment
settings preserve manual approval; SQLite persists through the relevant
restart/failure modes; and the current Prometheus evidence is suitable for
each intended incident class.

### Verified operator endpoint contract

The following request and workflow-boundary behavior is implemented and
covered by local API/workflow tests; it is not a live deployment guarantee:

- Missing/invalid JWTs are rejected; a valid operator must have the
  `remediation` permission to start the response workflow and submit approval
  decisions. Approval decisions do not require a separate shared token.
- Only the graph's supported service identifiers are accepted. A supplied
  correlation ID must be 1-64 ASCII letters/digits or `.`, `_`, `:`, `-`;
  malformed values receive request-validation errors. A valid
  `Idempotency-Key` header is required. The authenticated JWT subject is
  passed as `requested_by`.
- The API creates a durable `workflow_claims` row before graph execution.
  Uniqueness is enforced by SQLite on `(operator_id, service,
  SHA-256(idempotency_key))`; the raw key is not persisted. Repeated keys with
  the same service/correlation return the saved response when complete or
  return 409 with the original claim reference while unresolved. Reusing the
  key for a different correlation returns 409. The incident correlation ID
  is not treated as a key.
- On process restart, completed responses remain replayable. A `RUNNING`
  workflow claim is not automatically restarted; it returns unresolved/409
  for review. Its record intentionally does not expire automatically.
- Each actionable graph proposal receives a durable action claim. Its
  logical identity derives from service, deterministic incident category,
  namespace, resource UID when available (otherwise resource name),
  deployment, action, and target. A partial unique SQLite index prevents a
  second active claim for that logical action, including requests with
  different idempotency keys. An active duplicate is policy-denied and
  references the existing action claim; it creates no second approval or
  execution.
- Capacity reservation and counting occur under SQLite `BEGIN IMMEDIATE`.
  Reservations are deployment-scoped, include all active/unresolved claims
  plus terminal claims inside the configured policy window, and include
  legacy unclaimed executed audit rows when present. A claim counts before
  human approval, so pending approvals reserve capacity. Rejected/expired or
  completed attempts continue to consume the configured window capacity.
  Unresolved claims remain counted regardless of age. The guarantee is
  scoped to response-graph claims sharing the same SQLite database file.
- Claim identities are carried on workflow audit events and on approval,
  pre-execution, execution, and verification records. The action claim keeps
  the exact action and policy hashes and links to one approval ID. Approval
  creation and decision verify that the approval's incident, action,
  policy, and claim binding match. A changed/mismatched approval cannot be
  approved.
- Legal action-claim transitions are enforced by the repository. Execution
  is claimed before the pre-execution audit event. A failure before the
  executor is called becomes `FAILED_BEFORE_EXECUTION`; an exception or
  uncertain live outcome remains `EXECUTION_UNKNOWN` and is not released.
  A successful dry-run becomes `DRY_RUN_COMPLETE`, never live execution or
  recovery. Executed actions are persisted as `COMPLETED`; recovery becomes
  `VERIFIED` only after existing verification evidence is also persisted.
- If action outcome persistence fails after the executor returns, the claim
  stays `EXECUTING`, the workflow escalates, and verification is skipped.
  If verification persistence fails, the workflow cannot report recovery.
  These states require operator reconciliation; there is no blind replay or
  automatic claim expiry/release.
- Workflow exceptions return a generic service-unavailable response rather
  than raw exception/provider content. Unsupported service input retains a
  concise 400 response.
- The approval repository atomically transitions only a pending, unexpired
  approval, so competing decisions on the same approval can commit at most
  once. An expired approval is persisted as expired and cannot enter
  execution. Separate action claims for one logical action are serialized by
  the SQLite reservation transaction and unique index. The repository does
  not prohibit the requesting operator from approving their own request;
  separation of duties is not enforced and remains an operator/security
  decision.
- Deterministic policy remains the action gate; the workflow records an
  approval request when policy requires it. An approved request rechecks
  policy and fresh Kubernetes state before execution. The deployed
  auto-remediation setting is not verified, so an approval requirement is
  not guaranteed for every deployment configuration.
- Dry-run results are represented as not executed, produce
  `dry_run_complete`, and explicitly do not claim recovery. Verification runs
  only after execution reports success and requires consecutive healthy
  observations; unavailable observations cannot establish recovery.
- Evidence collection failures are represented as unavailable in covered
  paths and cannot by themselves establish a target or recovery. The
  diagnosis and Kubernetes collectors do not share a universal freshness
  guarantee; stale-source behavior remains source-specific and is not live
  verified.

SQLite tests use independent connections to a temporary database and
concurrent threads to exercise the actual `BEGIN IMMEDIATE`, unique indexes,
and atomic approval update. They establish same-file SQLite serialization,
not safety on another database backend or across replicas configured with
different database files. The action-claim ledger is authoritative for
reserved and completed response-graph actions; audit rows are separately
written and are not transactionally consistent with the ledger. Legacy
executions are included only when their audit rows are available in this
SQLite audit table.

The `/remediate/{service}` legacy HTTP route is deprecated and returns HTTP
410; it no longer invokes the separate legacy executor. The unused legacy
service still has its own `remediation_attempts` ledger and is not serialized
by the response-graph logical-action index. Do not call that service as a
compatibility fallback; use the persisted workflow path instead.

**Recommendation:** start with a manually initiated, authenticated
response-workflow request, because it avoids unattended trigger delivery
while reusing existing components. Do not describe it as continuous
monitoring or automatic incident detection. Keep automatic remediation
disabled and require the existing approval path unless an explicit future
operator decision authorizes a separate mode.

## Additional trigger contract requirements

SQLite claim semantics from Phase F.22 are described above. The following
items remain requirements for any future expansion of the trigger contract;
the listed sources and deployment are not yet independently validated.

Do not accept caller assertions as trusted Prometheus/Kubernetes evidence
and do not treat trigger receipt as an incident classification or
remediation decision.

### Identity and authorization

- `trigger_source`: fixed to `operator_api` for the selected first trigger.
- `trigger_id`: the persisted server-generated workflow claim ID (`wfc-*`).
- `idempotency_key`: required client-generated key, scoped to authenticated
  operator and service. Only its SHA-256 hash is stored. Completed requests
  replay their latest saved response; unresolved requests return a conflict
  referencing the original claim; mismatched request correlation conflicts.
- `operator_id`: from verified JWT `sub`; never trust a body-supplied actor.
- `service`: required allowlisted service or the explicitly supported
  Kubernetes-lab workflow target. Do not permit arbitrary service/resource
  names.
- Start authorization: require a least-privilege operator permission. Whether
  to retain the current `remediation` permission or introduce a narrower
  trigger permission is an operator/security decision.
- A trigger does not carry approval. Approval remains a distinct authorized
  decision using the existing approval route and token controls. Action
  approval binding is implemented for the response graph; self-approval is
  still permitted and separation of duties is unresolved.

### Evidence and state

The trigger input contains only the requested service and correlation /
idempotency metadata. Kybernaut collects evidence itself. The durable trigger
record should minimally identify:

- trigger ID, idempotency key reference (not secret), source, authenticated
  actor, service, received time, current state, and correlation incident ID;
- evidence-source status, source identity, source sample timestamp when
  supplied, query/evaluation time, and evaluator observation time;
- classification and target only as derived outputs tied to evidence;
- policy result, approval requirement/status, execution status, verification
  outcome, and audit event references as separate stages.

Do not store credentials, full prompts, or unnecessary personal data. Keep
source timestamps distinct; a parseable timestamp is not proof of freshness.
Freshness limits for diagnosis evidence, source lookback, and acceptable clock
skew remain operator decisions.

### Deduplication, correlation, and concurrency

- Reuse the same idempotency key for a transport retry; atomically return the
  original trigger state and never start another workflow.
- Correlate all trigger, evidence, classification, policy, approval,
  execution, verification, and audit events with one server-owned run/trigger
  ID and an incident ID. Do not treat the existing optional `incident_id` as
  an idempotency token.
- Distinguish a repeated observation of an active incident from a new
  occurrence after resolution. The key inputs, dedupe window, retention, and
  whether a new trigger attaches to an existing open incident need operator
  agreement. Do not invent a time window or silently merge unlike incidents.
- Claim an in-flight trigger atomically before workflow execution. A second
  request for the same key must not create duplicate approvals or actions.
  Concurrent distinct incidents need a documented isolation and rate-limit
  policy; no global serialization assumption is made here.
- A delayed trigger must retain both source event/sample time and receipt
  time. If outside the approved window or too old to evaluate, mark it
  `stale`/`not_actionable` and require fresh collection or human review; the
  age threshold is unresolved.

### Evidence failure and response boundaries

- Missing, stale, malformed, contradictory, or unavailable evidence is
  explicit `unknown`/`incomplete`/`invalid`; it must never become a healthy
  value or a confident actionable target.
- On source failure, record the collection failure and audit reference. Do
  not interpret a Prometheus error, empty vector, missing target, or absent
  Kubernetes observation as a confirmed healthy state. Continue only into a
  non-actionable monitoring-investigation state if the approved policy
  explicitly permits it.
- Classification and diagnosis are separate from response planning. RCA
  stays advisory. The deterministic classifier and strict evidence-backed
  target contract remain authoritative.
- Policy evaluates the proposed action against fresh state. A trigger must
  not call remediation directly, skip policy, fabricate approval, resume an
  approval, or change execution mode. Default first-release behavior should
  require explicit approval and keep remediation dry-run; changing that
  boundary requires separate authorization.
- Verification occurs only after an action is actually executed. A trigger,
  proposal, approval, or successful dry-run is not recovery.

### Audit, errors, and retries

- Persist a trigger claim with actor, service, claim ID, idempotency hash,
  timestamps, and correlation before processing. Source health and evidence
  references remain workflow audit events; audit and claim writes are not
  one transaction. Do not put secrets or full prompts in events.
- Audit each transition: accepted/rejected/deduplicated, evidence collection
  result, classification, plan, policy decision, approval requested/decided,
  execution started/result, verification result, and terminal outcome.
  Identify which writes must fail closed. The first executable contract
  should make trigger acceptance and pre-action authorization auditable
  before any action can be initiated.
- Return a stable, concise client error for invalid service/request fields;
  report source unavailability as unavailable rather than healthy. Do not
  retry side-effecting workflow execution automatically without idempotent
  claims. Retry policy, timeouts, backoff, and terminal status for transient
  source errors require operator approval.
- On process restart, recover accepted work only from durable state with a
  safe claim/lease and idempotency guarantee. Until then, expose an
  interrupted/unknown state for operator review; never blindly replay
  remediation.

## Remaining implementation and qualification plan

| Stage | Scope | Measurable acceptance criteria | Dependencies and authorization |
| --- | --- | --- | --- |
| 1. Review SQLite deployment constraints and reconciliation | Confirm the supported deployment uses one shared SQLite file on storage with documented locking/durability behavior; define operator procedure for `RUNNING`, `EXECUTING`, and `EXECUTION_UNKNOWN` claims. | Deployment review proves all API workers share the same database file; restart/backup procedures preserve claims; every abandoned state has a documented operator reconciliation decision; no automatic re-execution or expiry is introduced. | Operator, database, and security review. No live remediation authorization. |
| 2. Decide approval roles and effective execution mode | Decide whether self-approval is allowed and verify effective auto-remediation/dry-run settings for the intended environment. | Approved role policy and environment-specific configuration evidence establish who may start/approve and whether any live execution path is disabled. | Operator/security authorization; reading deployed configuration is separately scoped and must not expose secrets. |
| 3. Add durable audit reconciliation and observability | Define handling for separate audit/claim transactions and provide operator-visible query/report for claim, approval, execution, and verification references. | Synthetic partial-failure tests and operator procedure reconcile missing audit rows without replay; reports distinguish `EXECUTING`/unknown from completed/verified. | Depends on Stage 1; audit retention and reviewer ownership decisions. |
| 4. Controlled lab qualification | Validate only after the approved lab plan and a separate scenario-level authorization. | Timestamped, retained synthetic/local database evidence plus separately authorized lab results demonstrate accepted duplicates, capacity contention, approval binding, cleanup, and claim reconciliation. Do not infer live safety from SQLite unit tests. | Depends on Stages 1-3 and explicit F.17 preflight approval. Any live remediation needs separate authorization. |

### Rollback considerations

- Before deployment, define how to disable the trigger entry point or route
  new requests to an operator-visible unavailable state without deleting
  audit history.
- For any persistent idempotency/claim schema, rollback must preserve
  existing incidents, approvals, and audit records. Do not drop or rewrite
  data as a rollback shortcut.
- For webhook or polling work reconsidered later, rollback must disable
  intake/scheduling before reverting code, preserve accepted event IDs and
  cursors, and prevent retries from replaying actions. These options are not
  in the selected first milestone.
- No rollback was run or tested in this phase. Production rollout, database
  changes, network ingress, credential creation, and lab execution each
  require separate authorization.

## Security and operational risks

1. `GET /incidents` currently performs Prometheus detection/persistence
   without route-level operator authentication. Decide whether it must be
   restricted before exposing it; this audit does not change it.
2. The integrated response endpoint uses the `remediation` permission to
   start the graph. Confirm least-privilege role semantics; a trigger is not
   equivalent to approval.
3. Automatic remediation is environment-configurable. Code defaults to
   disabled but does not prove the deployed setting. The first supported
   operator trigger must not be treated as safe until the effective setting
   and execution path are verified.
4. Caller-supplied `incident_id` values receive a syntax/length check but
   are not checked for ownership or uniqueness. The Idempotency-Key prevents
   repeat submission only within authenticated operator/service scope;
   different keys may request the same incident. The SQLite action-claim
   index blocks a duplicate while its logical action is active; after a
   terminal state, a distinct key can create a new attempt subject to the
   configured capacity window.
5. Claim transactions serialize within one SQLite database file only.
   Multiple replicas with different files, alternate backends, or the
   separate legacy remediation endpoint are outside this guarantee. The
   deployment storage/topology has not been verified.
6. Source collection is request-driven and may be incomplete. Diagnosis
   sample age and underlying scrape freshness are not uniformly established.
   A future trigger must preserve unavailable/unknown states and avoid action
   on ambiguous evidence.
7. SQLite audit is useful for workflow traceability but not tamper-evident.
   Claim and approval binding is transactional, but audit events are written
   separately and many workflow audit writes remain best-effort. Audit
   durability, export, retention, and independent verification need an owner.
8. An automated webhook or poller can amplify duplicates and actions if
   delivery/restart behavior is not durable and idempotent. Authentication
   alone does not authorize remediation.

## Adjacent repository issues

### CI dependency reference

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) sets
`cache-dependency-path: requirements.txt` and installs that manifest.
[requirements.txt](../requirements.txt) is also the documented setup
manifest and includes `pytest`, `pytest-asyncio`, and `pyyaml`, required by
the test and YAML-validation steps. Phase F.20 corrected the stale
`requirements-dev.txt` references without adding a duplicate dependency
manifest or changing package versions. Local tests were used to validate the
relevant behavior; the remote GitHub Actions workflow was not run.

### Runbook test fixtures

[test_runbook_retriever.py](../tests/test_runbook_retriever.py) expects
`memory-pressure.md`, `pod-crash.md`, `database-timeout.md`,
`deployment-failure.md`, and `high-cpu.md`. All five files exist in the
current [runbooks/](../runbooks/) directory. Therefore the prior
repository-audit statement that these expected filenames were absent is
incorrect for this checkout; this decision record corrects it. Phase F.20
inspected the fixture contents and ran the focused retrieval tests; see its
completion report for exact results. No runbook fixture or assertion needed
changing.

## Unresolved operator decisions

- Who owns trigger operation, on-call response, audit review, and escalation?
- Is `remediation` permission acceptable for starting a response workflow, or
  should a separate permission be approved?
- Must the future operator trigger enforce approval-only behavior regardless
  of environment configuration? This record recommends yes for the first
  supported mode but does not change implementation.
- What are the idempotency key scope, retention, conflict behavior,
  concurrent-incident policy, incident fingerprint semantics, and dedupe
  window?
- Which services and evidence sources are in the supported first scope?
  What freshness and clock-skew policy applies to each?
- What timeouts, retry/backoff policy, and interrupted-run recovery behavior
  are acceptable?
- Which audit fields must be durable before accepting a trigger, and what are
  the evidence and audit retention/export requirements?
- If automated polling or a webhook is later selected, who owns sender/rule
  configuration, workload identity, network boundaries, worker health, and
  delivery/replay operations?

## Phase 4.23 diagnosis evidence freshness decision record

**Status:** D-FRESH-01 and D-FRESH-02 remain pending. D-FRESH-03 is approved
with the behavior and implementation status recorded below. This approval
does not establish an evidence-age or clock-skew threshold.

For each decision, the recorded outcome must be exactly `approved`,
`rejected`, or `pending`. An approval record must include the accountable
approver's identity and role, an auditable approval reference, the decision
date, the approved value or wording, and all conditions or exceptions.
For D-FRESH-01 and D-FRESH-02, no approver, reference, date, or approval has
been recorded. A `pending` or `rejected` outcome blocks any corresponding
age- or skew-gate implementation.

| ID | Decision | Minimum evidence and unresolved decision details | Approval record (outcome / approver identity and role / reference / date / approved value or wording / conditions or exceptions) |
|---|---|---|---|
| D-FRESH-01 | Maximum diagnosis evidence age: **60 seconds proposed, explicitly unapproved**. The proposal applies per trustworthy source observation; any query-, feature-, or diagnosis-level summary must retain its per-input result. | The evidence package must establish authoritative source-observation cadence, scrape and query intervals, jitter, and acceptable evidence age for each required input. `diagnose_service` makes three sequential Prometheus instant queries, each with a 10-second HTTP timeout. Its 5xx-rate and P95 expressions cover five-minute windows. An instant-query evaluation timestamp does **not** establish the observation freshness of the underlying data in a range expression. The decision must explicitly state what to do when trustworthy source-observation timestamps are unavailable. No authoritative diagnosis cadence or operator rationale currently supports 60 seconds; the separate feature collector's 30-second limit is not a diagnosis policy. | **Outcome:** `pending` (no approval recorded). **Approver identity/role:** unassigned. **Reference:** none recorded. **Decision date:** none recorded. **Approved value/wording:** none. **Conditions/exceptions:** not decided. |
| D-FRESH-02 | Future timestamp tolerance: **0 seconds proposed, explicitly unapproved**. Missing, malformed, timezone-naive, or future-dated timestamps cannot establish freshness under this proposal. Any clock-skew exception must be separately proposed; none is assumed. | The evidence package must identify relevant clocks, synchronization guarantees, and measured or otherwise authorized permitted skew. The decision must state the tolerance and record any exception explicitly; no exception may be inferred. Diagnosis timestamps are parsed from Prometheus numeric Unix timestamps and normalized by the shared timestamp parser, but the repository defines no approved diagnosis clock-skew allowance. | **Outcome:** `pending` (no approval recorded). **Approver identity/role:** unassigned. **Reference:** none recorded. **Decision date:** none recorded. **Approved value/wording:** none. **Conditions/exceptions:** not decided; no exception approved. |
| D-FRESH-03 | **Approved fail-safe policy:** incomplete, missing, stale, unavailable, or invalid evidence is not healthy or zero; insufficient evidence triggers monitoring investigation and remains visible as a limitation. An independently valid high/critical finding may require approval despite another incomplete source. A finding may influence a target-specific decision only when the finding and target are independently valid. Contradictory evidence blocks target-specific remediation until resolved and the target verified. Preserve fresh pre-execution checks, authentication, authorization, approval, and remediation safeguards. | No new evidence-age or clock-skew threshold is approved. Prometheus diagnosis, telemetry, and Kubernetes retain their existing source-specific behavior; query evaluation timestamps are not treated as proof of raw sample freshness. The implementation uses explicit evidence status, valid source timestamps where available, incident classification, and target-backed evidence; it does not assign severity to missing data. Contradictory current Kubernetes classifications remain targetless. Cross-source service-to-workload conflict detection is not established because the approved workload map has no verified service mappings. | **Outcome:** `approved`. **Approver identity/role:** project owner (identity not supplied in the implementation request). **Reference:** explicit project-owner approval in the Workstream 7 implementation request. **Decision date:** 2026-10-27. **Approved wording:** the policy stated in this row. **Conditions/exceptions:** no age/skew threshold; target-specific action still requires fresh pre-execution evidence, authorization, and approval. |

### Evidence and implementation prerequisites

The 60-second maximum age has no approved cadence or operator rationale behind
it. The five-minute PromQL windows describe the expressions' lookback, not a
diagnosis collection cadence or a guarantee of fresh raw samples. The
10-second HTTP timeout bounds an individual request; it does not establish an
evidence-age policy. The feature collector's 30-second age and 10-second skew
settings apply to a separate collection path and are not approval for
diagnosis.

### Explicit implementation authorization gate

Age- and skew-based diagnosis freshness implementation remains **blocked**
until D-FRESH-01 and D-FRESH-02 are approved. D-FRESH-03 implementation is
authorized only within the approved evidence-quality and mixed-evidence
semantics; it does not create an age or skew policy.

1. D-FRESH-01 and D-FRESH-02 each have an explicit `approved` outcome before
   adding diagnosis age or skew gates.
2. Timestamp semantics are confirmed for each diagnosis input, including
   which timestamps represent source observation versus query evaluation.
   An instant-query evaluation timestamp remains insufficient proof of
   underlying range-expression sample freshness.
3. The deterministic test matrix and technical review criteria are
   documented for fresh, stale, unknown, incomplete, future, malformed, and
   mixed evidence, including policy precedence and safeguards.

The proposed 60-second maximum age and zero-second future tolerance remain
**unapproved proposals**, not active policy. D-FRESH-03 implementation is
tested locally; it does not establish runtime or operational qualification.

## Explicit non-goals and authorization boundaries

- This decision record covers the subsequent D-FRESH-03 implementation in
  application code and offline tests; it does not qualify a deployment.
- No poller, webhook receiver, scheduler, queue, or continuous monitoring was
  implemented.
- No Prometheus, Groq, Kubernetes, or other external service was contacted.
- No benchmark, remediation, failure injection, cleanup, rollback, or
  resilience experiment was run.
- The recommendation does not authorize trigger deployment or live
  evaluation. Any implementation, credential/network setup, database
  migration, production exposure, or state-changing lab validation requires
  separate review and authorization.
