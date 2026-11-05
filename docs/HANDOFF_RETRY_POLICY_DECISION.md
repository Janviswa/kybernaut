# Incident Handoff Retry Policy Decision

**Status: proposal for owner review only. All policy items are `PENDING OWNER
DECISION`.** This record documents inspected repository behavior and presents
neutral alternatives. It does not authorize a retry-policy change, select
limits, or permit replay of ambiguous execution.

## Scope

This decision concerns retries of durable incident handoffs, particularly
confirmed workflow pre-start failures reported as `WorkflowNotStarted`. It is
separate from the response graph's remediation-attempt limit. No source
behavior is changed by this document.

Implementation references:

- [application_anomaly_monitor.py](../app/services/application_anomaly_monitor.py):
  `dispatch_pending_handoffs` and `ApplicationAnomalyMonitor.poll_service`
- [incident_handoff_repository.py](../app/services/incident_handoff_repository.py):
  `claim_next_handoff`, `finish_handoff`, and `reconcile_expired_leases`
- [incident_workflow.py](../app/services/incident_workflow.py):
  `_persist_telemetry_incidents`
- [incident_response_graph.py](../app/services/incident_response_graph.py):
  `run_response_workflow`, `WorkflowNotStarted`, and
  `WorkflowExecutionAmbiguous`
- [test_incident_handoff.py](../tests/test_incident_handoff.py):
  focused retry, ambiguity, lease, and reconciliation tests

## Current repository behavior

- `_persist_telemetry_incidents` writes supported telemetry occurrences and
  their occurrence-scoped handoff in one SQLite transaction. The handoff has
  a deterministic occurrence idempotency key.
- `claim_next_handoff` considers the requested service's `PENDING` and
  `RETRY` rows when `retry_at` is absent or due. It claims only an active
  occurrence whose fingerprint still matches. Dispatch creates or reuses
  the durable workflow claim transactionally.
- `dispatch_pending_handoffs` handles `WorkflowNotStarted` by finishing the
  handoff as `RETRY` with `last_error="workflow_not_started"`.
- `finish_handoff` schedules a fixed five-second delay using
  `RETRY_DELAY_SECONDS`. A later monitor poll must invoke the dispatcher for
  the handoff to be considered again.
- No handoff retry-count ceiling or elapsed-time ceiling was found in the
  inspected claim, dispatcher, finish, or monitor paths. `attempts` is
  incremented for a dispatch claim but is not used there to stop retries.
  The dispatcher's `max_dispatches` bounds work in one call; it is not a
  retry limit.
- `WorkflowExecutionAmbiguous` and other unexpected dispatch exceptions lead
  to `RECONCILIATION_REQUIRED`, not blind replay. Expired leases are
  reconciled from durable workflow/action/approval evidence. `EXECUTION_UNKNOWN`
  remains a reconciliation state; lease expiry by itself does not authorize
  another execution.
- A pending row can remain pending if its dispatcher does not continue
  operating. A repeatedly confirmed pre-start failure can remain in the retry
  cycle without a source-enforced maximum. These are code-path possibilities,
  not claims about any live installation.
- Handoff rows and audit events retain retry/reconciliation status and
  metadata. The inspected operations API exposes audit/timeline reads, but
  does not provide a dedicated handoff queue endpoint. Monitor status does not
  expose individual handoff attempt counts or retry ages.

## Decisions requiring explicit approval

Each item below remains **`PENDING OWNER DECISION`**. The alternatives are
options for review, not recommendations or defaults.

### D-RETRY-01 — Retry bounds

**Question:** Should repeated confirmed pre-start failures be limited by a
maximum retry count, elapsed time, both, or neither?

| Alternative | Reliability and operational burden | Risk of abandoning recoverable work |
|---|---|---|
| No automatic bound | Keeps retrying transient startup failures without requiring operator intervention; can consume repeated dispatch capacity and needs monitoring. | Does not abandon a recoverable handoff automatically, but can leave a persistent failure cycling. |
| Maximum retry count only | Gives a clear bound on repeated attempts; count semantics and manual follow-up must be defined. | A recoverable issue that needs more attempts may stop before it clears. |
| Elapsed-time limit only | Bounds how long automatic retries continue; requires a defined clock and behavior during dispatcher downtime. | A handoff may age out while the dispatcher is unavailable or while the failure remains transient. |
| Both count and elapsed-time bounds | Limits repeated attempts and total automatic retry duration; adds policy and state-accounting complexity. | Either boundary can stop a still-recoverable handoff. |

**Owner decision:** `PENDING OWNER DECISION`

### D-RETRY-02 — Delay/backoff

**Question:** Should the existing fixed delay remain, or should an approved
backoff policy replace it?

| Alternative | Reliability and operational burden | Risk of abandoning recoverable work |
|---|---|---|
| Retain fixed delay | Predictable and simple; repeated failures continue at the same cadence. | No inherent age/count stop; recovery depends on future dispatcher polls. |
| Increasing backoff | Reduces repeated dispatch pressure during a persistent failure; requires defined calculation and reset conditions. | Longer intervals can delay recovery after a transient problem clears. |
| Fixed delay with an approved cap/jitter policy | Can bound load bursts while avoiding synchronized retries; adds implementation and observability requirements. | Delayed attempts may reach an owner-selected stopping point before recovery. |

No timing values or backoff parameters are selected here.

**Owner decision:** `PENDING OWNER DECISION`

### D-RETRY-03 — Action at a retry limit

**Question:** If a future approved retry limit is reached, what durable outcome
should be recorded?

| Alternative | Reliability and operational burden | Risk of abandoning recoverable work |
|---|---|---|
| Move to a non-dispatchable operator-review state | Stops automatic attempts and makes human intervention explicit; requires ownership and a way to resume safely. | Work will not continue automatically, including if the failure would soon clear. |
| Mark a terminal retry-exhausted outcome | Makes the stop explicit and easy to report; needs a separately authorized requeue procedure. | Can make recoverable work appear complete unless the terminal meaning is clearly distinguished. |
| Continue retrying but escalate/alert | Preserves automatic recovery while notifying an operator; requires alert delivery and sustained monitoring. | Attempts continue despite escalation and can keep consuming resources. |

**Owner decision:** `PENDING OWNER DECISION`

### D-RETRY-04 — Operator visibility and escalation

**Question:** What evidence and notification should be available for old or
repeatedly failing handoffs?

| Alternative | Reliability and operational burden | Risk of abandoning recoverable work |
|---|---|---|
| Audit/timeline inspection only | Uses existing event surfaces; requires an operator to know which incident to inspect and interpret repeated events. | A stuck handoff may remain unnoticed. |
| Dedicated read-only handoff queue/status surface | Shows state, attempts, last error category, and age in one place; requires API/UI ownership and access controls. | Better visibility does not itself resume work; unowned queue items can still remain unresolved. |
| Alert/escalation on an owner-defined condition | Can bring persistent failures to an operator sooner; requires routing, alert ownership, and noise management. | Overly sensitive escalation can create alert fatigue; muted/noisy alerts may still miss work. |

Any chosen surface must distinguish pending, retrying, awaiting approval,
reconciliation required, and terminal outcomes. It must not imply that a
handoff is safe to replay merely because it is old.

**Owner decision:** `PENDING OWNER DECISION`

### D-RETRY-05 — Reconciliation ownership

**Question:** Which accountable role or team owns investigation and disposition
of ambiguous and retry-exhausted handoffs?

| Alternative | Reliability and operational burden | Risk of abandoning recoverable work |
|---|---|---|
| Named service/operator owner | Keeps decisions close to workload context; requires explicit assignment and coverage. | Unassigned or unavailable owners can delay disposition. |
| Central operations/on-call function | Can centralize queue review and escalation; requires runbook and cross-service context. | A central function may lack workload-specific evidence or capacity. |
| Shared ownership with defined escalation | Provides service context plus an escalation path; requires clear handoff, acknowledgment, and closure rules. | Ambiguous responsibility can still delay action if escalation conditions are unclear. |

Reconciliation must use durable workflow, action, approval, execution, and
verification evidence. It must not infer a safe outcome from elapsed time or
missing audit events.

**Owner decision:** `PENDING OWNER DECISION`

## Safety invariants

Any later approved implementation must preserve these invariants:

1. **Lease expiry is not replay authorization.** Do not replay ambiguous
   execution solely because a lease expires.
2. **Timestamps and absent events are not recovery evidence.** Do not infer
   safe recovery from timestamps or missing audit events.
3. **Durable claims remain authoritative.** Preserve workflow/action claims,
   their identity bindings, and reconciliation evidence.
4. **Approval remains a separate gate.** A retry policy does not grant
   approval, bypass requester/approver separation, or authorize execution.
5. **Verification remains required.** An incident is not marked recovered
   based only on dispatch completion, a successful command response, or a
   retry outcome.
6. **Retry policy is not remediation-attempt policy.** Handoff pre-start
   retry bounds and response-graph remediation attempt limits are distinct
   controls and must be documented/tested independently.
7. **Existing records are preserved.** Policy changes must not delete or
   silently rewrite existing incident, handoff, workflow, action, approval,
   verification, or audit records.

## Decision record

No approver, role, approval channel, or policy outcome is inferred or filled
in by this proposal. Complete this record only after the project owner
records an explicit decision.

| Field | Recorded value |
|---|---|
| Decision status | `PENDING OWNER DECISION` |
| Approved policy items | None recorded |
| Approver identity | Unassigned |
| Approver role | Unassigned |
| Approval reference/channel | None recorded |
| Decision date | None recorded |
| Rationale | Pending |
| Conditions or exceptions | Pending |

## Related evidence

The following existing test functions exercise parts of the current behavior;
their presence is not a test result for this proposal:

- `test_confirmed_prestart_failure_retries_same_claim`
- `test_ambiguous_workflow_start_is_reconciled_and_never_replayed`
- `test_expired_ambiguous_lease_is_not_replayed`
- `test_expired_lease_reconciles_pending_approval_without_replay`
- `test_existing_running_claim_is_not_blindly_replayed`
- `test_concurrent_dispatch_claims_are_serialized`
- `test_concurrent_dispatchers_start_only_one_workflow`
- `test_handoff_retry_audit_failure_rolls_back_state`

All are in [test_incident_handoff.py](../tests/test_incident_handoff.py).
