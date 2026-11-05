# Detection-to-Handoff Routing Decision

**Status: decision preparation only. All decisions below are `PENDING OWNER
DECISION`.** This record presents the unresolved routing choices; it does not
select trigger classes, authorize dispatch, or change runtime behavior.

## Scope and existing evidence

This decision covers whether each detection source may create a durable
incident-workflow handoff, requires additional evidence first, or remains
observation-only; the side effects of the advisory `GET /workflow/{service}`;
and ownership of automatic handoff dispatch.

The existing [Monitoring Trigger Architecture and Decision](./MONITORING_TRIGGER_DECISION.md)
records the Phase 4.18 decision to retain both the background anomaly monitor
and operator-initiated response workflows. It does not decide every detection
class or authorize new dispatch paths. The
[Handoff Retry Policy Decision](./HANDOFF_RETRY_POLICY_DECISION.md) remains a
proposal; its retry and reconciliation items are not resolved by this record.

Relevant current implementation paths:

- [`GET /incidents`](../app/main.py) invokes
  `detect_unhealthy_services()` in
  [`incident_detector.py`](../app/services/incident_detector.py). It records
  Prometheus outage, scrape-failure, and missing-target observations but does
  not enqueue a handoff.
- [`GET /kubernetes/incidents`](../app/main.py) calls `observe_namespace()`
  and `persist_observation()` in
  [`k8s_incident_sync.py`](../app/services/k8s_incident_sync.py). A complete
  successful observation persists/refreshes or resolves Kubernetes
  occurrences; it does not enqueue a handoff.
- [`GET /workflow/{service}`](../app/main.py) calls
  `run_incident_workflow()` in
  [`incident_workflow.py`](../app/services/incident_workflow.py). Its
  `validate_contract` node persists supported, timestamp-valid actionable
  CPU/memory anomalies via `record_incident(..., enqueue_handoff=True)`.
  This happens before contract validation finishes. The GET runs the advisory
  graph and does not itself dispatch the handoff.
- The background
  [`ApplicationAnomalyMonitor.poll_service`](../app/services/application_anomaly_monitor.py)
  evaluates telemetry, persists supported CPU/memory anomaly occurrences and
  handoffs, then calls `dispatch_pending_handoffs()`.
- Handoff claims, eligibility, and durable dispatch state are maintained by
  [`incident_handoff_repository.py`](../app/services/incident_handoff_repository.py).

These are repository facts, not recommendations about which source ought to
be actionable or proof of any deployed runtime configuration.

## Decision 1 — Actionable detection classes

For each row, the owner must choose one disposition:

- **May create a workflow handoff:** this source/class is approved to enqueue
  work under separately specified evidence, authorization, idempotency, and
  audit conditions.
- **Requires further evidence:** it may create a handoff only after specified
  independent evidence and qualification conditions are met.
- **Observation-only:** it may be persisted or displayed but must not create a
  workflow handoff.

No disposition is preselected. “Current behavior” describes the inspected
code and is not a recommendation.

| Detection source/class | Current code behavior | Owner disposition | Operational and safety implications to decide |
|---|---|---|---|
| Prometheus service outage (`service_unavailable`) | `GET /incidents` persists/refreshes the observation and may resolve it after a healthy observation; that path does not enqueue a handoff. | `PENDING OWNER DECISION` | Dispatch may shorten response latency but can turn a service-health signal into an action workflow. Observation-only avoids automated work from this signal but leaves response to another trigger/operator. Requiring corroborating evidence can reduce action on incomplete monitoring data, at the cost of delay and added evidence-source dependencies. |
| Prometheus scrape failure (`monitoring_scrape_failed`) | `GET /incidents` records the warning and does not enqueue a handoff. Detector semantics distinguish scrape failure from confirmed application outage. | `PENDING OWNER DECISION` | Dispatch risks treating loss of observability as workload failure. Additional independent health evidence may distinguish telemetry-path failure from service failure but adds latency and dependencies. Observation-only preserves the distinction and leaves triage to operators/other evidence. |
| Prometheus missing target (`monitoring_target_missing`) | `GET /incidents` records a monitoring warning when a non-empty health vector exists but an expected service lacks an active target; it does not enqueue a handoff. | `PENDING OWNER DECISION` | Dispatch could surface a real deployment/monitoring configuration failure sooner, but missing telemetry does not establish workload health or safe remediation target. Requiring workload/deployment evidence can improve attribution; observation-only avoids acting without a confirmed target. |
| Kubernetes findings | `GET /kubernetes/incidents` persists findings from a complete successful namespace observation and resolves absent fingerprints; it does not enqueue a handoff. | `PENDING OWNER DECISION` | Dispatch can shorten response for identified resource symptoms but requires an approved finding taxonomy, target identity/freshness requirements, and action mapping. Requiring further evidence can constrain false positives and target drift but delays triage. Observation-only preserves the current read/record boundary. |
| Actionable CPU/memory telemetry anomalies | The background anomaly monitor persists supported timestamp-valid candidates and enqueues occurrence-scoped handoffs. The advisory GET can also persist/enqueue them before contract validation. | `PENDING OWNER DECISION` | Retaining dispatch uses existing dedupe and human approval controls but exposes a read-named advisory GET to persistence/enqueue side effects. Requiring Kubernetes or other corroboration can reduce telemetry-only actionability but risks delay or missing incidents. Observation-only would preserve anomaly evidence without entering the response workflow. |
| High-latency telemetry signals | The direction-aware policy can identify p95 latency as a statistical actionable candidate and separately emits a `high_latency` operational finding above the existing 1-second threshold. Neither signal is in the current CPU/memory persistence mapping, so neither currently creates a persisted telemetry incident or handoff through that path. | `PENDING OWNER DECISION` | The owner must choose whether to persist and enqueue, keep both signals policy-only, or specify separate routing for each signal. The choice must identify the applicable entry points and whether persistence also enqueues work. |

For every disposition that permits handoffs, the decision must state the
qualifying evidence, timestamp/freshness rules, occurrence identity and
deduplication expectations, and how unavailable or contradictory sources are
handled. This record does not establish any of those values.

### High-latency routing choice

The owner must decide the treatment of two distinct policy outputs:

1. **Statistical actionable candidate:** p95 latency identified by the
   direction-aware anomaly policy as an actionable candidate.
2. **Operational finding:** `high_latency`, emitted when p95 latency exceeds
   the existing 1-second threshold.

Select one alternative; none is preselected:

| Alternative | Proposed treatment |
|---|---|
| **Option A — Persist and enqueue** | Approved high-latency signals create persisted incidents and handoffs. |
| **Option B — Policy-only** | High-latency signals remain findings and do not create persisted incidents or handoffs. |
| **Option C — Different treatment by signal** | The owner explicitly defines separate routing for the statistical actionable candidate and the operational finding. |

For the selected behavior, the decision must explicitly state whether it
applies to the background telemetry monitor, the advisory workflow path, or
both; whether handoffs are enqueued automatically or only incidents are
persisted; and whether the existing fingerprint, deduplication, and resolution
semantics remain unchanged. No threshold, detector, or current routing
behavior is changed by this proposal.

### Decision record — D-ROUTE-04

- **Status:** `PENDING OWNER DECISION`
- **High-latency option:** Pending — Option A, Option B, or Option C
- **Statistical actionable candidate treatment:** Pending
- **Operational finding treatment:** Pending
- **Applicable paths:** Pending — background telemetry monitor, advisory workflow path, or both
- **Persistence and handoff behavior:** Pending
- **Fingerprint, deduplication, and resolution semantics:** Pending
- **Accountable approver identity:** Unassigned
- **Accountable approver role:** Unassigned
- **Auditable decision reference:** None recorded
- **Decision date:** None recorded
- **Rationale:** Pending
- **Conditions or exceptions:** Pending

### Decision record — D-ROUTE-01

- **Status:** `PENDING OWNER DECISION`
- **Accountable approver identity:** Unassigned
- **Accountable approver role:** Unassigned
- **Auditable decision reference:** None recorded
- **Decision date:** None recorded
- **Per-source dispositions:** Pending for all listed rows above
- **Rationale:** Pending
- **Scope:** Pending
- **Conditions or exceptions:** Pending

## Decision 2 — Advisory GET side effects

Choose one alternative for `GET /workflow/{service}`:

| Alternative | Trade-offs |
|---|---|
| **Retain current persistence/enqueue side effect** | Keeps existing advisory behavior and occurrence-scoped idempotent handoff creation. A GET can change durable incident/handoff/audit state before contract validation completes; dispatch is not immediate in that request and depends on a dispatcher. Clients and operators must understand that this nominally advisory route has write side effects. |
| **Make the GET observational only and move persistence/enqueue to an explicitly designated workflow path** | Makes GET behavior easier to treat as read-only and separates evidence inspection from durable work creation. Requires selecting an explicit path/owner for persistence and dispatch; until then, detections observed only by this GET may not create durable actionable work. The move must preserve deduplication and avoid silently losing existing queued work. |

The owner should also state whether contract-validation failure is allowed to
leave persistence that occurred earlier in the current call path. No route
change or suppression is authorized by this proposal.

### Decision record — D-ROUTE-02

- **Status:** `PENDING OWNER DECISION`
- **Decision:** Pending — retain side effect or make GET observational only
- **Accountable approver identity:** Unassigned
- **Accountable approver role:** Unassigned
- **Auditable decision reference:** None recorded
- **Decision date:** None recorded
- **Rationale:** Pending
- **Scope:** Pending
- **Conditions or exceptions:** Pending

### Approved advisory persistence failure behavior

The project owner approved persistence Option A on 2026-10-27: an incident
persistence exception aborts the advisory request. The endpoint must not
return a successful partial advisory response. Its existing generic sanitized
HTTP 503 error contract is retained for persistence failures, including
underlying `ValueError` exceptions; internal logging records the service and
exception class without logging exception contents.

Each incident write remains an independent transaction. If a later write
fails, earlier committed incidents and handoffs may remain; this behavior is
not an all-or-nothing transaction. The advisory request does not retry and
does not dispatch or execute remediation as a consequence of persistence
failure. Authorization still runs before the workflow.

**Implementation status:** implemented and covered by isolated offline
regression tests. This is not database-topology, deployment, or operational
qualification. The approval is limited to failure behavior: D-ROUTE-02's
separate choice to retain or remove the GET persistence/enqueue side effect,
and dispatcher ownership, remain pending.

## Decision 3 — Dispatcher ownership and queued work

The current background anomaly monitor invokes the handoff dispatcher during
its service poll. The advisory GET can create a handoff but does not dispatch
it in-request. The inspected operations routes do not establish another
automatic dispatcher. This does not establish whether a monitor is enabled or
healthy in any deployment.

| Alternative | Trade-offs |
|---|---|
| **Background monitor is the sole automatic dispatcher** | Keeps dispatch ownership aligned with the existing monitor. Queued work can remain pending while it is disabled, stopped, unable to acquire leadership, or otherwise not polling; the owner must decide how that condition is surfaced and who responds. This choice does not itself authorize replay or recovery. |
| **Another explicitly designated dispatcher may operate** | Could provide dispatch when the monitor is unavailable, but introduces another worker/identity, concurrency, lease, deployment, observability, and duplicate-work boundary. Requires separate design and approval; no additional dispatcher is authorized here. |
| **No automatic dispatch while the monitor is disabled/unavailable; operator review only** | Avoids implicit fallback dispatch and makes the pause explicit. Requires a documented operator queue/visibility and disposition process; pending work can wait until reviewed. |
| **Queued work remains pending until the monitor resumes** | Preserves existing durable queue state without assigning fallback behavior. Work may be delayed indefinitely if monitoring does not resume, so visibility and ownership remain necessary decisions. |

The owner must select whether the monitor is the sole automatic dispatcher
and explicitly state the expected treatment and visibility of queued handoffs
while it is disabled or unavailable. This record does not authorize automatic
recovery, another dispatcher, or changes to retry/reconciliation behavior.

### Decision record — D-ROUTE-03

- **Status:** `PENDING OWNER DECISION`
- **Sole automatic dispatcher:** Pending
- **Queued-work treatment while disabled/unavailable:** Pending
- **Accountable approver identity:** Unassigned
- **Accountable approver role:** Unassigned
- **Auditable decision reference:** None recorded
- **Decision date:** None recorded
- **Rationale:** Pending
- **Scope:** Pending
- **Conditions or exceptions:** Pending

## Approval and implementation gate

No routing, advisory GET side-effect, or dispatcher-ownership decision is
recorded here. Before changing those behaviors, record each decision with:

1. An explicit outcome for every source class and each of D-ROUTE-02 and
   D-ROUTE-03.
2. The accountable approver's identity and role.
3. An auditable decision reference and decision date.
4. Rationale, scope, conditions, and exceptions.
5. The evidence, freshness, idempotency, and audit requirements applicable
   to any approved dispatching class.

Pending, absent, or ambiguous decision fields do not authorize an
implementation. Existing approval gates, durable workflow/action claims,
verification requirements, and ambiguous-execution reconciliation remain
unchanged. The pending retry-policy decisions in
[HANDOFF_RETRY_POLICY_DECISION.md](./HANDOFF_RETRY_POLICY_DECISION.md) remain
separate and unresolved.

## Non-authorization statement

This document prepares owner decisions only. It does not authorize new
automatic dispatch, change the advisory route, change retry policy, choose
detector behavior, relax authorization or approval requirements, or authorize
live remediation or production operation.
