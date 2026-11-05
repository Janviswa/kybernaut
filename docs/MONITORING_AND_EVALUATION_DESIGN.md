# Monitoring Trigger and Evaluation Design

**Status: design and current-state record.** The trigger analysis below
records the repository state inspected on 2026-10-22. Phase 4.18 subsequently
confirmed support for both the default-enabled application anomaly monitor
and operator-initiated API workflows. The monitor status endpoint is a
read-only snapshot; it does not establish production reliability. This
document does not authorize live evaluation or operational use.

For the evidence-based comparison and selected first trigger candidate, see
[MONITORING_TRIGGER_DECISION.md](./MONITORING_TRIGGER_DECISION.md). That
document records the historical recommendation of an operator-initiated
response API; the approved current design retains it alongside background
monitoring, with existing remediation authorization and approval safeguards.

## 1. Current-state findings

### Request-driven entry points

| Entry point | Current behavior | Can start the response graph? | Persistence and access |
| --- | --- | --- | --- |
| `GET /incidents` in [main.py](../app/main.py) | Calls `detect_unhealthy_services()`. The detector queries Prometheus `up{job=~".*-service"}` and `/api/v1/targets`, classifies service-down, scrape-failure, and (when the query vector is non-empty) missing-target conditions. An empty `up` result returns early without inferring missing targets. | No. It is an outage detector, not a response-graph call. | Detection records/refreshes incidents in SQLite. Requires an authenticated operator with `read` permission. |
| `GET /diagnose/{service}` and `GET /decide/{service}` in [main.py](../app/main.py) | Run service diagnosis or diagnosis plus the older deterministic decision engine. | No. | Request/response only; diagnosis does not persist an incident. |
| `GET /workflow/{service}` in [main.py](../app/main.py) | Authenticated advisory graph: diagnosis, telemetry anomaly evaluation, Kubernetes observation, optional RCA, then deterministic policy. | No. It ends at advisory policy. | Requires an operator token with `read`; output includes legacy fields and contract status. |
| `POST /workflow/{service}/respond` in [operations.py](../app/api/operations.py) | Starts `run_response_workflow()`: evidence collection, classification, optional RCA, proposal, deterministic policy, approval request or no-action/blocked terminal. | Yes. This is the current authenticated response-graph entry. Approval is required unless explicitly configured automatic policy allows execution. | Requires `remediation` permission. Uses the audit/approval repository. |
| `POST /approvals/{approval_id}/decision` in [operations.py](../app/api/operations.py) | Records an approval or rejection. An approved decision starts the guarded workflow in the same request. | Continues the existing response graph; it is not a detection trigger. | Requires remediation permission. An explicit Execute action can approve and run the persisted plan as the same operator. No shared approval token is used; policy and evidence are rechecked before execution. |
| `GET /kubernetes/incidents` in [main.py](../app/main.py) | Reads the lab namespace and persists the complete observation using `persist_observation()`. | No. | Requires `read`; persistence occurs only after successful observation. |
| `GET /monitor/status` in [main.py](../app/main.py) | Returns the current process's configuration, lifecycle, leadership, poll activity, completed-cycle timestamps, freshness, and sanitized outcomes. | No. | Requires `read`; status reads do not initiate polling or other operational actions. |

The application anomaly monitor starts in the API lifespan by default and can
be explicitly disabled with `KYBERNAUT_ANOMALY_MONITOR_ENABLED=false`. It
sequentially evaluates configured services, persists/updates anomaly incidents,
dispatches authorized handoffs, and performs diagnosis for active incidents.
It does not execute remediation. Operator-initiated API workflows remain
available alongside this background trigger. No Prometheus alert webhook
receiver or Alertmanager integration is implemented. `/health` reports API
liveness only; `/monitor/status` reports available monitor state, not proof of
production reliability.

### Monitor status contract

`GET /monitor/status` is a process-local snapshot, not a cluster-wide
leadership or reliability view. Its response separates configuration,
lifecycle, current activity, the last completed cycle, and freshness:

- `configured_enabled`: `true` or `false` after lifespan initialization;
  `null` before configuration is known.
- `lifecycle_state`: `not_started`, `running`, `stopped`, or `disabled`.
- `poll_activity`: `idle` or `in_progress`. While a cycle is in progress,
  `last_outcome` continues to describe the previous completed cycle.
- `leadership_state`: `unknown`, `not_applicable`, `leader`, `follower`,
  `lock_unavailable`, or `not_leader`. `is_leader` is true only for `leader`,
  false for a known non-leader, and null while unknown.
- `last_poll_attempt_at` is the cycle start. `last_completed_poll_at` is set
  only after all configured services have returned; cancellation does not
  replace the previous completed outcome. `last_successful_poll_at` is updated
  only when every configured service evaluates successfully.
- `last_outcome`: `unknown` before a completed cycle; `success` when all
  configured services are `evaluated`; `error` when all service polls fail
  with exceptions; `unavailable` when all evaluations are unavailable; or
  `partial` for mixed outcomes, including skipped results.
- Each service reports its last completed `outcome` (`unknown`, `evaluated`,
  `unavailable`, or `error`), its own `poll_activity`, attempt/completion/
  successful timestamps, and nullable `status_stale`. The currently polled
  service retains its last completed outcome while `poll_activity` is
  `in_progress`.
- Overall `freshness` and nullable `status_stale` use the age of
  `last_completed_poll_at`; per-service `status_stale` uses that service's
  `last_completed_at`. Both are `unknown` (`null`) if no valid completion time
  exists or the timestamp is in the future; otherwise they are `fresh`
  (`false`) through exactly two configured polling intervals and `stale`
  (`true`) only when older than two intervals. A new attempt does not refresh
  completed-result freshness.
- `health` is `disabled`, `unknown`, `healthy`, or `degraded`. Disabled
  configuration yields `disabled`; a stopped, uninitialized, or never-completed
  monitor yields `unknown`. During an active cycle, health is `unknown` when
  the previous completed cycle succeeded and neither aggregate nor service
  freshness is stale, provided all services completed so far in the current
  cycle are `evaluated`. It is `degraded` during polling if the previous
  completed outcome was not successful, any completed result is stale, or a
  service completed in the current cycle with a non-`evaluated` result. When
  idle, health is `healthy` only when the last full cycle succeeded and both
  overall and every service freshness are fresh. Failed, partial, unavailable,
  stale, or unknown service results do not produce `healthy`.
- `health` and the legacy `last_outcome` continue to describe polling and
  anomaly evaluation. `workflow_health` and per-service `workflow_health`
  are freshness-qualified summaries of the most recently observed component
  outcomes, not guarantees about unobserved work. They are `disabled` when
  monitoring is disabled and `unknown` when the monitor is not running.
  While running, a failed, retrying, unavailable, or stale successful
  component makes workflow health `degraded`; pending, unknown, or successful
  work without a usable completion timestamp makes it `unknown`. `healthy`
  requires every applicable component to be successful and fresh or
  explicitly `not_applicable`.
- Additive `components` are available at the top level (aggregate status and
  per-service values) and inside each service. Components distinguish
  `telemetry_collection`, `anomaly_evaluation`, `incident_persistence`,
  `handoff_enqueue`, `handoff_dispatch`, `reconciliation`, `diagnosis`, and
  `diagnosis_audit`. Each reports `status`, a sanitized reason code when
  known, `last_completed_at`, `last_successful_at`, `status_stale`, and
  `freshness`. Component freshness uses the existing polling interval:
  completion remains fresh through exactly two configured intervals and is
  stale only when older. Missing or future timestamps produce unknown
  freshness. Top-level component freshness is stale if any service's
  component is stale, fresh only if all are fresh, and otherwise unknown.
  Missing source outcomes remain `unknown`; current work can be `pending` or
  `retrying`; absent applicable work is `not_applicable`.
- The anomaly runtime now additively reports collection and evaluation stage
  outcomes while retaining the existing `status` and `reason` fields.
  Collection and evaluation cannot be separated when a substituted evaluator
  omits those fields; collection is then `unknown`.
- Reconciliation status is derived from the dispatcher's reported expired
  lease count and explicit reconciliation results. Diagnosis audit failure
  is surfaced from its `audit_persistence_failed` limitation; audit success
  remains `unknown` because the diagnosis orchestrator does not return an
  explicit audit outcome. These fields do not claim that best-effort
  response-graph stage audit writes succeeded: those audit outcomes are not
  currently returned by the graph. Transactional enqueue/dispatch audit
  failures remain reflected by their corresponding stage failure.
- `last_successful_workflow_at` is distinct from
  `last_successful_poll_at`; the latter is unchanged for compatibility.
  It records the last successful workflow-health observation; it does not
  mean the monitor is currently running or that later results remain fresh.
  Component state is process-local and is lost on process restart. This
  additive visibility does not change routing, dispatch, approval, execution,
  or recovery behavior, and it does not add Alertmanager/webhook delivery.

### Existing evidence sources and persistence

- [incident_detector.py](../app/services/incident_detector.py) independently
  queries Prometheus `up` and target inventory. It uses a 10-second HTTP client
  timeout, persists service outage, scrape failure, and missing-target records,
  and resolves only service-outage records after a healthy result. An empty
  `up` vector is inconclusive and does not resolve incidents.
- [prometheus_features.py](../app/services/prometheus_features.py) runs four
  feature queries sequentially: request rate, P95 latency, process memory, and
  CPU. It preserves each instant-query sample timestamp and omits incomplete,
  stale, future, or overly skewed rows using configured defaults of 30 seconds
  maximum age and 10 seconds maximum skew. These limits validate returned
  instant-query timestamps; they do not prove freshness of all underlying
  scrapes in PromQL expression windows.
- [incident_diagnoser.py](../app/services/incident_diagnoser.py) performs
  separate sequential Prometheus queries for `up`, five-minute HTTP 5xx rate,
  and five-minute P95 latency. It preserves per-query timestamp/provenance
  metadata; expression result timestamps are not proof of underlying scrape
  freshness. Missing or invalid evidence remains incomplete/invalid.
- [anomaly_runtime.py](../app/services/anomaly_runtime.py) calls the feature
  collector for on-demand anomaly evaluation. The separate application
  anomaly monitor invokes anomaly evaluation periodically; this function's
  on-demand behavior does not imply continuous execution by itself.
- [incident_repository.py](../app/services/incident_repository.py) supports
  stable incident refresh by `fingerprint`, or legacy refresh by
  `incident_id` when no fingerprint is supplied. The Prometheus detector
  creates a stable ID from service and incident type and does not currently
  supply a fingerprint. That ID can merge recurring occurrences after prior
  resolution in historical reporting; it is not a unique event ID.
- The response graph writes detailed stage events and durable approval
  requests through [audit_repository.py](../app/services/audit_repository.py).
  It has an incident correlation ID and returns audit event references.
  Passing a caller-provided incident ID does not currently implement
  idempotent request handling.

### Existing classification and response behavior

[incident_classifier.py](../app/services/incident_classifier.py) classifies
evidence into `cpu_spike`, `memory_leak`, `pod_crash`, `disk_full`,
`deployment_failure`, or `unknown`. Kubernetes evidence may support an
actionable target; telemetry-only candidates remain targetless. The strict
[incident contract](../app/models/incident_contract.py) requires any
classification target to match available Kubernetes evidence.

The integrated [response graph](../app/services/incident_response_graph.py)
uses deterministic classification and policy. RCA remains advisory. A
proposal can be denied, require approval, or (only with explicit policy
configuration) be eligible for automatic execution. Approval is persisted,
rechecked against current state before execution, and execution has a
fail-closed pre-execution audit event. Dry-run is `not_executed` and
verification is `not_run`; only a successfully executed action can be
reported recovered. The separate advisory [incident_workflow.py](../app/services/incident_workflow.py)
does not propose executable actions, create approvals, remediate, or verify.

## 2. Trigger options

| Option | Required components and entry | Latency and ownership | Delivery, idempotency, evidence | Security and operational concerns |
| --- | --- | --- | --- | --- |
| Operator-initiated API request | Existing authenticated `POST /workflow/{service}/respond`, plus operator selection of service and incident correlation ID. | Detection starts when an authorized operator requests it. No automatic detection-latency objective is implied. The operator owns triage and repeat-request decisions. | HTTP retry or repeated clicks can start separate runs. Caller-supplied `incident_id` is correlation only; no idempotency key or duplicate suppression exists. The graph recollects evidence and applies its current missing/unavailable handling. | Uses existing JWT `remediation` permission. Keep route network exposure and role assignments constrained. Approval token/decision, policy, and dry-run safeguards continue to apply. |
| Prometheus alert webhook | Alertmanager or another configured sender, a new authenticated receiver, payload schema/version validation, alert-to-service mapping, and an explicit call into the existing response graph. None is currently implemented. | Delivery and queue latency depend on external configuration; no current service owner or response objective is encoded. Ownership for receiver, alert rules, and on-call delivery must be decided. | Alertmanager can group/repeat notifications; sender retries can duplicate deliveries. Require stable provider alert identity plus a bounded dedupe store, and retain original alert timestamps separately from receiver time. Missing/stale alert evidence must become monitoring investigation, not healthy status. | Requires authenticated service-to-service ingress, narrow network exposure, sender identity validation, replay limits, payload bounds, rate limits, and audit of receipt/rejection. Do not expose an unauthenticated webhook. |
| Bounded periodic polling | The application has a single-leader background anomaly monitor, started by the FastAPI lifespan by default unless disabled, with a configurable interval (60 seconds by default; minimum 10 seconds). This is source-level implementation evidence, not runtime verification. It does not by itself establish the full proposed design's cadence/jitter policy, durable cursor, deduplicating response-graph route, or operational ownership and recovery requirements. | The implementation's configured interval does not establish an end-to-end detection-latency objective. Cadence and acceptable latency remain operational decisions. A named owner must monitor worker health, lag, and overlap. | The monitor's in-process leadership mechanism does not establish reliable cluster-wide ownership across deployment changes. Stable event fingerprints, a watermark or explicit query window, and duplicate suppression for any response-graph routing remain design requirements. Prometheus failure/staleness must not resolve or trigger remediation as if healthy. | Background credentials and failure handling must be separate from request identity. Poll deadlines, retries, backlog, and concurrent runs need bounded operational behavior; poller health must not leak evidence. Avoid multiplying Prometheus queries through separate collectors. None of these runtime properties were verified by this document review. |

### Recommendation

Use **operator-initiated API requests as the first trigger**, through the
existing authenticated response endpoint, for controlled synthetic and
reviewed lab evaluation. This is the smallest defensible option because it
needs no new scheduler, receiver, queue, or runtime. It provides a deliberate
human initiation boundary and already enters the deterministic response graph.
The default policy requires approval; explicitly enabled automatic policy can
change that behavior and must remain separately reviewed. Treat the supplied
incident ID as correlation only until a separate idempotency contract is
designed.

This recommendation does not make `GET /incidents` an automatic trigger.
That route only detects and persists its current outage categories. Nor does
the response endpoint establish continuous monitoring or a detection-latency
guarantee. Alert-webhook integration and any additional polling guarantees
should remain deferred until an owner, latency/cadence objectives, dedupe
semantics, and source-health behavior are reviewed. The existing anomaly
monitor's source-level presence does not satisfy those governance or runtime
verification requirements.

### Proposed event/evidence lifecycle

1. An authorized operator initiates one response request for an allowlisted
   service and provides or receives a stable correlation ID.
2. The graph records evidence collection, then gathers the existing diagnosis,
   telemetry anomaly, and Kubernetes observation using current service
   boundaries. Source timestamps/provenance are preserved only where
   available. Query result timestamps are not silently upgraded into raw
   scrape-freshness guarantees.
3. The deterministic classifier returns a supported category and a target
   only when Kubernetes evidence establishes that target. Missing,
   conflicting, malformed, or unavailable inputs remain explicit.
4. The graph records classification, advisory RCA status, action proposal,
   and deterministic policy result. RCA cannot choose targets or policy.
5. A denied/no-action result ends without execution. An allowed proposal
   either remains approval-gated or follows the separately reviewed
   automatic-policy configuration.
6. Approval is persisted and bound to the proposal/correlation. On approval,
   current policy and target state are rechecked before the required
   pre-execution audit event and tool invocation.
7. Dry-run is recorded as not executed and not recovered; verification is
   skipped. Executed actions require fresh evidence-based verification.
8. The audit timeline retains stage/status references while minimizing raw
   evidence and provider content.

## 3. Idempotency and source-health requirements

Before a future automatic trigger is enabled, define:

- **Event identity:** a stable trigger event ID, distinct from incident
  correlation ID and incident fingerprint. Store provider/source, alert
  labels or service/resource identity, and original event timestamp.
- **Deduplication window and concurrency:** how duplicate webhook deliveries
  or overlapping poll runs map to one pending/active response. Use an atomic
  claim/lease so multiple API replicas cannot both start execution. Define
  how a deliberately new recurrence after resolution gets a new occurrence
  ID.
- **Collector ownership:** the response path currently calls diagnosis,
  telemetry, and Kubernetes observers; the feature collector itself executes
  four Prometheus queries, while the diagnosis and outage detector have
  independent queries. Do not add a poller that repeats all of these
  indiscriminately. Decide whether to reuse a single bounded evidence
  snapshot or keep explicit collectors with documented query ownership.
- **Source-health state:** unavailable Prometheus, missing target inventory,
  empty vectors, invalid timestamps, stale inputs, and source disagreement
  must be distinguished from a confirmed service incident and from healthy
  evidence. Do not resolve or suppress an alert based solely on missing data.
- **Bounds and operations:** explicit HTTP/query timeouts, maximum in-flight
  runs, queue/backlog cap, retry budget, poll overlap behavior, receiver
  payload size, and operator-visible health/lag indicators.
- **Authorization and audit:** exact service identity, source authentication,
  caller permissions, durable trigger receipt/dedup decision, correlation
  propagation, approval state, and audit failure behavior.

No poll interval, stale-evidence threshold for diagnosis, duplicate window,
or detection-latency objective is selected here; each requires explicit
operational review.

## 4. Evaluation design

### Evaluation layers

Keep four result classes separate; do not combine their claims:

1. **Unit/contract tests:** deterministic fixtures and temporary databases
   verify validators, query parsing, status handling, classification,
   target backing, policy, approval, audit ordering, dry-run, and verification.
   Report test command, exit code, pass/fail/skip counts, and test scope.
2. **Synthetic end-to-end scenarios:** in-memory/mocked evidence exercises the
   existing graph and captures output plus audit timeline. These results
   prove code-path behavior for fixtures only, not live collection or
   detection accuracy.
3. **Labeled benchmark:** the current 20-case matrix in
   [scenarios.json](../benchmark/scenarios.json) has four injected-case
   definitions per each of the five incident categories and recommends five
   healthy control windows. These are proposed cases, not executed results.
   Its scenario labels do not enforce contextual preconditions; verify and
   record variants such as traffic and replica count independently. A
   separate labeled negative/unknown set is needed to measure `unknown`
   classification behavior; the five healthy controls alone are too few for
   robust generalization.
4. **Separately authorized live-lab observations:** record actual context,
   namespace, simulator version/command, preflight and cleanup health,
   observation timestamps, raw evidence references, and deviations. Never
   characterize synthetic test success as live detection accuracy.

### Coverage matrix

| Area | Required test cases | Primary measurable outputs |
| --- | --- | --- |
| Five incident categories | At least one independent expected case per `cpu_spike`, `memory_leak`, `pod_crash`, `disk_full`, `deployment_failure`; retain the 20 matrix IDs for repeated contextual cases. | Expected vs observed category; evidence IDs and source; target identity exact-match result. |
| `unknown` and healthy negatives | No evidence, unsupported evidence, benign healthy controls, and ambiguous/conflicting signals with no actionable target. | `unknown` rate and false-positive count/rate with explicit denominator and control duration. |
| Classification and target | Correct category; wrong category; targetless telemetry candidate; wrong namespace/resource/UID; multiple conflicting current events. | Category confusion matrix; target precision/exact-match rate; count of unsupported targets (must be zero for acceptance). |
| Evidence quality | Missing source, malformed timestamp/value, stale/future timestamp, mixed query times, unavailable source, and conflicting source states. | Per-source status counts; incomplete/invalid cases; count incorrectly treated as healthy (must be zero). |
| RCA | Provider unavailable, timeout, invalid structured response, valid response, adversarial input, bounded/truncated input. | RCA available/invalid/unavailable counts, structured-output validity denominator, latency/token sample count if provider supplies trustworthy values. RCA/policy disagreement does not change policy. |
| Policy/approval | Allowed/denied proposals, missing deployment/action-count state, unauthorized action/namespace, expired or replayed approval, pre-execution audit failure. | Policy outcome by rule; approval-before-execution ordering violations (must be zero); calls to execution before approval (must be zero). |
| Dry-run/audit | Approved dry-run plus denied/no-action paths. | `executed=false`, verification `not_run`, no recovery claim, correlation ID continuity, expected audit event references present. |
| Verification | Executed action with consecutive healthy samples; unhealthy, unavailable, incomplete, or missing deployment evidence. | Verified/not-verified/unavailable counts; verified recovery latency only when actual execution and verification succeeded. |

### Result schema

Phase F.15 implements a backward-compatible envelope: unversioned and
integer-version-1 inputs remain accepted; version 2 requires a `run` object.
The implemented minimum is documented in
[EVALUATION.md](./EVALUATION.md). This implemented form supersedes the earlier
F.8 proposal; fields not present here remain future design decisions.

```json
{
  "schema_version": 2,
  "run": {
    "run_id": "operator-assigned-opaque-id",
    "evaluation_mode": "synthetic",
    "scenario_matrix_sha256": "sha256-of-exact-loaded-matrix-bytes",
    "started_at": "2026-10-22T00:00:00+00:00",
    "completed_at": "2026-10-22T00:20:00+00:00"
  },
  "cases": [],
  "controls": []
}
```

The matrix digest binds the calculation to the exact file bytes loaded, not a
normalized JSON serialization; it cannot prove that those bytes were used at
execution time. The mode is a caller declaration: `live_lab` does not prove a
live run. Result IDs and timestamps are checked against the matrix and run
bounds. Per-record provenance and policy fields remain caller-supplied claims,
not authenticated source evidence or proof of safety. Do not include secrets,
full prompts, or unnecessary personal data in result references.

### Metrics and reporting rules

Report raw counts and denominators with every rate:

- Detection: TP, FN, FP, recall, precision, and F1. Define the positive
  detection event before a run. False positives use explicitly timed healthy
  controls. If controls are absent, precision is not a false-positive-adjusted
  operational estimate.
- Classification: confusion matrix over the five categories plus `unknown`,
  per-class precision/recall/F1, macro-F1, accuracy, and number of unlabeled
  cases. Unknown/negative cases must be represented; do not silently score
  only injected positive cases.
- Targeting: exact namespace/resource/kind/UID correctness and unsupported
  target count. A telemetry-only candidate must not become target-correct by
  inference.
- Timing: MTTD only for valid, nonnegative injected-to-detected intervals.
  Report sample count and omitted/invalid timestamp count. MTTR includes only
  actually executed actions with a successful verification timestamp; dry-run
  is never a recovery sample.
- RCA: validity, availability, latency, and token metrics only over cases
  where the relevant observation exists. Report provider failures and missing
  usage metadata rather than treating them as zero.
- Safety: denied-action count, approval-before-execution violations,
  pre-execution-audit blocks, dry-run executed count, invalid evidence marked
  healthy, and unverified recoveries. Acceptance requires zero for the latter
  four unsafe outcomes; thresholds for quality metrics require review.

The current [benchmark_metrics.py](../app/services/benchmark_metrics.py)
requires exactly the 20 positive scenario IDs and five declared controls. It
counts an alert as detected even when the category is wrong; legacy category
scalars cover positive cases and additive classification metrics include the
controls. It supports exact target comparison, optional source-provenance
coverage, and caller-reported policy/approval state coverage, but cannot prove
target truth, evidence authenticity/freshness, or runtime policy safety. The
F.15 version-2 envelope reports declared run metadata separately and validates
the matrix digest and timestamp bounds; it does not attest actual execution.
The F.9 validator requires verified recovery to follow successful executed
remediation. See the
[Evaluation Readiness Audit](./EVALUATION_READINESS_AUDIT.md) for the current
metric and schema assessment. Keep historical results in their original
unversioned/version-1 shape; do not retrofit or invent run metadata.

### Acceptance criteria requiring review

The following safety criteria can be fixed without operational tuning:

- Missing, invalid, stale, or conflicting evidence is never classified as
  healthy solely because no anomaly was observed.
- Unsupported or telemetry-inferred targets are never actionable.
- Execution-before-approval and execution-after-failed-pre-audit counts are
  zero.
- Dry-run execution count and dry-run recovery claims are zero.
- Verification cannot report recovery from incomplete/unavailable evidence.
- Every response preserves one correlation ID across response output and
  audit events, and audit references point to recorded events.

The following quality/service-level thresholds remain open and must receive
explicit review: detection latency objective, poll cadence, stale-evidence
age for diagnosis, duplicate/dedupe retention window, minimum precision/
recall/F1, allowable false-positive rate, target-accuracy threshold, minimum
sample counts, and acceptable RCA latency/availability. Do not fabricate
thresholds or infer them from an unlabeled baseline.

## 5. Risks and open decisions

1. Which operator identity/team owns manual response initiation and audit
   review?
2. Is `GET /incidents` intended to remain an operator-polled detector, or
   should it eventually become a passive read of persisted records? Its
   current behavior queries and persists on every request.
3. What stable trigger ID, fingerprint fields, dedupe window, and recurrence
   rules should apply across manual requests, webhooks, and pollers?
4. What detection-latency objective and polling cadence, if any, are
   acceptable? What clock-skew assumptions apply?
5. Which evidence collector owns Prometheus snapshots if an automated trigger
   is added, and how should feature sample timestamps be associated with the
   diagnosis and canonical contract?
6. What authenticated network path and sender identity are required for a
   future alert webhook?
7. What labeled unknown/negative dataset and number of independent healthy
   controls are sufficient for meaningful classification and false-positive
   estimates?
8. What metric-quality thresholds and minimum sample counts are approved for
   promotion or operational acceptance?
9. How should partial/unavailable observations be represented in a future
   benchmark without confusing “not evaluated” with a missed detection?

## 6. Staged implementation plan

1. **Review decisions:** approve initial operator trigger, caller permissions,
   correlation/idempotency semantics, and any metric thresholds. No code until
   these choices are explicit.
2. **Version evaluation schema:** add validation for run metadata, unknown
   cases, controls, source provenance, target correctness, audit IDs, and
   denominators; preserve existing raw results and version old/new metrics.
3. **Harden manual trigger semantics:** add an idempotency key distinct from
   correlation ID, atomic dedupe/claim, and tests proving retries do not
   create duplicate pending actions. Keep existing policy and approval gates.
4. **Synthetic end-to-end acceptance:** exercise all categories, unknown,
   healthy controls, incomplete evidence, RCA failure/adversarial output,
   denied actions, approvals, dry-run, audit failures, and verification with
   temporary storage and mocks.
5. **Separately review webhook integration and polling operations:** specify
   owner, authentication, cadence/latency, source freshness, concurrency,
   backpressure, dedupe, and recovery behavior for any proposed changes or
   guarantees beyond the existing anomaly monitor. Do not run or change an
   automatic trigger in the lab or production until separately authorized.
6. **Authorized live-lab benchmark:** execute only under a dedicated,
   scenario-specific authorization and existing runbook safety gates. Record
   actual evidence, cleanup, and deviations. Do not claim live accuracy from
   synthetic tests.

## 7. References

- [Agent and model implementation plan](./AGENT_MODEL_IMPLEMENTATION_PLAN.md)
- [Evaluation protocol](./EVALUATION.md)
- [Scenario matrix](../benchmark/scenarios.json)
- [Prometheus incident detector](../app/services/incident_detector.py)
- [Prometheus feature collector](../app/services/prometheus_features.py)
- [Incident diagnosis](../app/services/incident_diagnoser.py)
- [Incident repository](../app/services/incident_repository.py)
- [Operations routes](../app/api/operations.py)
- [Canonical incident contract](../app/models/incident_contract.py)
- [Incident response graph](../app/services/incident_response_graph.py)
